#!/usr/bin/env python3
"""
Aralab Client Intelligence — Weekly News Fetcher

Monitors business press for Aralab clients via GNews API,
filters with Claude Haiku, translates non-PT/EN content,
and delivers a digest via email.
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlencode

import anthropic
import requests

# ── Configuration ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
CLIENTS_PATH = BASE_DIR / "data" / "clients.json"
SOURCES_PATH = BASE_DIR / "data" / "sources.json"
NEWS_DIR = BASE_DIR / "data" / "news"

GNEWS_KEY = os.environ.get("GNEWS_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")

GNEWS_URL = "https://gnews.io/api/v4/search"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 20

SYSTEM_PROMPT = (
    "You are a news relevance filter and translator for Aralab, a Portuguese "
    "manufacturer of environmental testing chambers. You will receive a list of "
    "news articles about a specific company. For each article, you must:\n"
    "1. Decide if it is RELEVANT or IRRELEVANT. Relevant means it directly covers "
    "the company from a business/strategic angle: financial results, investments, "
    "partnerships, expansions, contracts, restructurings, M&A, leadership changes, "
    "layoffs, funded projects, new products. Irrelevant means: generic press releases "
    "about minor events, passing mentions in unrelated articles, job listings, "
    "generic industry lists.\n"
    "2. If RELEVANT: translate the title and description to Portuguese if they are "
    "in German, Spanish, or French. Keep English as-is. Write a clean 2-sentence "
    "summary in Portuguese.\n"
    "3. Return a JSON array. Each item: {\"url\": \"...\", \"relevant\": true/false, "
    "\"title_pt\": \"...\", \"summary_pt\": \"...\", \"source_name\": \"...\", "
    "\"published_at\": \"...\", \"client_name\": \"...\"}\n"
    "Return ONLY the JSON array, no other text."
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[DEBUG] {msg}", flush=True)


def mask_token(token: str) -> str:
    if len(token) <= 4:
        return "****"
    return "****" + token[-4:]


def load_json(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── GNews Fetch ──────────────────────────────────────────────────────────────

def fetch_articles_for_client(client: dict, from_date: str) -> list:
    """Fetch articles from GNews API for a single client."""
    query = client["aliases"][0]
    params = {
        "q": query,
        "token": GNEWS_KEY,
        "max": 10,
        "sortby": "publishedAt",
        "from": from_date + "T00:00:00Z",
    }

    # Build debug URL with masked token
    debug_params = {**params, "token": mask_token(GNEWS_KEY)}
    debug_url = f"{GNEWS_URL}?{urlencode(debug_params)}"
    log(f"GET {debug_url}")

    resp = requests.get(GNEWS_URL, params=params, timeout=30)
    log(f"Response: HTTP {resp.status_code}")

    if resp.status_code != 200:
        log(f"Response body: {resp.text[:500]}")
        resp.raise_for_status()

    data = resp.json()
    raw_count = len(data.get("articles", []))
    log(f"GNews returned {raw_count} raw articles for \"{query}\"")

    if raw_count == 0:
        log(f"No articles found. Full response: {json.dumps(data, ensure_ascii=False)[:300]}")

    articles = []
    for art in data.get("articles", []):
        articles.append({
            "url": art.get("url", ""),
            "title": art.get("title", ""),
            "description": art.get("description", ""),
            "source_name": art.get("source", {}).get("name", ""),
            "published_at": art.get("publishedAt", ""),
            "client_name": client["name"],
        })
    return articles


# ── Dedup & Source Matching ──────────────────────────────────────────────────

def deduplicate_articles(articles: list) -> list:
    """Remove duplicate articles by URL."""
    seen = set()
    unique = []
    for art in articles:
        url = art.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(art)
    return unique


def filter_by_sources(articles: list, sources: list) -> list:
    """Prioritise articles from known sources. All articles are kept."""
    if not sources:
        return articles
    domains = [s["domain"].lower() for s in sources]
    matched = []
    unmatched = []
    for art in articles:
        url = art.get("url", "").lower()
        source_name = art.get("source_name", "").lower()
        if any(d in url or d.replace(".", " ") in source_name for d in domains):
            matched.append(art)
        else:
            unmatched.append(art)
    log(f"Source filter: {len(matched)} from known sources, {len(unmatched)} other — all kept")
    return matched + unmatched


# ── Claude Filtering ─────────────────────────────────────────────────────────

def filter_with_claude(articles: list) -> list:
    """Send articles to Claude for relevance filtering and translation."""
    if not articles:
        log("No articles to filter, skipping Claude call")
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    relevant = []
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        log(f"Claude batch {batch_num}/{total_batches}: sending {len(batch)} articles")

        user_message = json.dumps(batch, ensure_ascii=False, indent=2)

        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )

            text = ""
            for block in response.content:
                if block.type == "text":
                    text += block.text

            log(f"Claude batch {batch_num} response length: {len(text)} chars")

            text = text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines)

            parsed = json.loads(text)

            batch_relevant = 0
            batch_irrelevant = 0
            for item in parsed:
                is_relevant = item.get("relevant", False)
                title = item.get("title_pt", item.get("title", "?"))[:80]
                client_name = item.get("client_name", "?")
                if is_relevant:
                    batch_relevant += 1
                    log(f"  RELEVANT: [{client_name}] {title}")
                    relevant.append(item)
                else:
                    batch_irrelevant += 1
                    log(f"  IRRELEVANT: [{client_name}] {title}")

            log(f"Claude batch {batch_num} result: {batch_relevant} relevant, {batch_irrelevant} irrelevant")

        except json.JSONDecodeError as e:
            log(f"WARN: Failed to parse Claude JSON for batch {batch_num}: {e}")
            log(f"Raw response (first 500 chars): {text[:500]}")
        except anthropic.APIError as e:
            log(f"WARN: Claude API error for batch {batch_num}: {e}")

    return relevant


# ── Email ────────────────────────────────────────────────────────────────────

def build_email_html(articles: list, date_str: str) -> str:
    """Build the HTML email digest."""
    if not articles:
        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:40px 20px;background:#0f0f0f;color:#e8e8e8;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:680px;margin:0 auto;">
    <h1 style="color:#00c896;font-size:22px;margin-bottom:8px;">Aralab Client Intelligence</h1>
    <p style="color:#888;font-size:14px;margin-bottom:30px;">Semana de {date_str}</p>
    <p style="font-size:16px;line-height:1.6;">Sem notícias relevantes esta semana para os clientes monitorizados.</p>
    <hr style="border:none;border-top:1px solid #222;margin:30px 0;">
    <p style="color:#555;font-size:12px;">Gerado automaticamente · aralab-clients-news</p>
  </div>
</body>
</html>"""

    by_client = {}
    for art in articles:
        cn = art.get("client_name", "Unknown")
        by_client.setdefault(cn, []).append(art)

    n_articles = len(articles)
    n_clients = len(by_client)

    cards_html = ""
    for client_name in sorted(by_client.keys()):
        client_articles = by_client[client_name]
        cards_html += f'<h2 style="color:#00c896;font-size:18px;margin:30px 0 15px 0;border-bottom:1px solid #222;padding-bottom:8px;">{client_name}</h2>\n'

        for art in client_articles:
            title = art.get("title_pt", art.get("title", "Sem título"))
            url = art.get("url", "#")
            source = art.get("source_name", "")
            pub_date = art.get("published_at", "")[:10]
            summary = art.get("summary_pt", "")

            cards_html += f"""<div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:8px;padding:16px;margin-bottom:12px;">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <span style="color:#00c896;font-size:12px;font-weight:600;">{client_name}</span>
    <span style="color:#888;font-size:11px;background:#252525;padding:2px 8px;border-radius:4px;">{source}</span>
  </div>
  <a href="{url}" style="color:#e8e8e8;text-decoration:none;font-size:15px;font-weight:600;line-height:1.4;">{title}</a>
  <p style="color:#888;font-size:12px;margin:6px 0 8px 0;">{pub_date}</p>
  <p style="color:#bbb;font-size:13px;line-height:1.5;margin:0;">{summary}</p>
</div>\n"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:40px 20px;background:#0f0f0f;color:#e8e8e8;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:680px;margin:0 auto;">
    <h1 style="color:#00c896;font-size:22px;margin-bottom:8px;">Aralab Client Intelligence</h1>
    <p style="color:#888;font-size:14px;margin-bottom:30px;">Semana de {date_str}</p>
    {cards_html}
    <hr style="border:none;border-top:1px solid #222;margin:30px 0;">
    <p style="color:#555;font-size:12px;">Gerado automaticamente &middot; {n_articles} artigos de {n_clients} clientes &middot; aralab-clients-news</p>
  </div>
</body>
</html>"""


def send_email(subject: str, html_body: str) -> bool:
    """Send an HTML email via Gmail SMTP."""
    if not all([GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL]):
        log("Email credentials not configured — skipping send")
        return False

    log(f"Sending email to {RECIPIENT_EMAIL}")
    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
        log("Email sent successfully")
        return True
    except smtplib.SMTPException as e:
        log(f"ERROR: Failed to send email: {e}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Aralab Client Intelligence — Weekly News Fetch")
    print("=" * 60)

    # Validate env vars
    log(f"GNEWS_KEY present: {'yes (' + mask_token(GNEWS_KEY) + ')' if GNEWS_KEY else 'NO'}")
    log(f"ANTHROPIC_API_KEY present: {'yes' if ANTHROPIC_API_KEY else 'NO'}")
    log(f"GMAIL_USER present: {'yes' if GMAIL_USER else 'NO'}")
    log(f"GMAIL_APP_PASSWORD present: {'yes' if GMAIL_APP_PASSWORD else 'NO'}")
    log(f"RECIPIENT_EMAIL present: {'yes (' + RECIPIENT_EMAIL + ')' if RECIPIENT_EMAIL else 'NO'}")

    missing = []
    if not GNEWS_KEY:
        missing.append("GNEWS_KEY")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        print(f"[FATAL] Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Load data
    clients = load_json(CLIENTS_PATH)
    sources = load_json(SOURCES_PATH)
    active_clients = [c for c in clients if c.get("active", False)]

    today = datetime.utcnow()
    from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    date_str = today.strftime("%Y-%m-%d")

    log(f"Date: {date_str}")
    log(f"From date: {from_date}")
    log(f"Active clients: {len(active_clients)}")
    log(f"Sources loaded: {len(sources)}")
    print()

    # Fetch articles for each client
    all_articles = []
    errors = 0

    for idx, client in enumerate(active_clients, 1):
        print(f"[{idx}/{len(active_clients)}] {client['name']}")
        try:
            articles = fetch_articles_for_client(client, from_date)
            log(f"Kept {len(articles)} articles for {client['name']}")
            all_articles.extend(articles)
        except requests.RequestException as e:
            log(f"ERROR: HTTP request failed for {client['name']}: {e}")
            if hasattr(e, "response") and e.response is not None:
                log(f"Response status: {e.response.status_code}")
                log(f"Response body: {e.response.text[:500]}")
            errors += 1
        except Exception as e:
            log(f"ERROR: Unexpected failure for {client['name']}: {type(e).__name__}: {e}")
            errors += 1
        print()

    # Deduplicate
    total_raw = len(all_articles)
    all_articles = deduplicate_articles(all_articles)
    total_deduped = len(all_articles)
    log(f"Dedup: {total_raw} raw → {total_deduped} unique")

    # Source matching (all articles kept, known sources first)
    all_articles = filter_by_sources(all_articles, sources)

    # Claude filtering
    log(f"Sending {total_deduped} articles to Claude ({CLAUDE_MODEL}) in batches of {BATCH_SIZE}")
    relevant_articles = filter_with_claude(all_articles)

    # Sort by date
    relevant_articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    log(f"Final relevant articles: {len(relevant_articles)}")

    # Save JSON
    output_path = NEWS_DIR / f"{date_str}.json"
    save_json(output_path, relevant_articles)
    log(f"Saved to: {output_path}")

    # Email
    subject = f"Aralab Client Intelligence — Semana de {date_str}"
    html_body = build_email_html(relevant_articles, date_str)
    email_sent = send_email(subject, html_body)

    # Summary
    client_names_with_news = set(a.get("client_name") for a in relevant_articles)
    print()
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"  Clients checked:     {len(active_clients)}")
    print(f"  Fetch errors:        {errors}")
    print(f"  Articles fetched:    {total_raw}")
    print(f"  After dedup:         {total_deduped}")
    print(f"  Relevant (kept):     {len(relevant_articles)}")
    print(f"  Clients with news:   {len(client_names_with_news)}")
    print(f"  Email sent:          {'Yes' if email_sent else 'No'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
