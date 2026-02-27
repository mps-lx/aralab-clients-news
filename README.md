# Aralab Client Intelligence

Automated weekly news monitoring system for Aralab's client portfolio. Scans business press across Portugal, Spain, Germany, France, and international English-language outlets, filters articles for strategic relevance using an LLM, translates non-English content to Portuguese, and delivers a digest via email and a static web dashboard.

## How It Works

1. **Every Monday at 7:00 UTC**, a GitHub Actions workflow runs `scripts/fetch_news.py`
2. The script queries [NewsAPI](https://newsapi.org) for each active client across 16 configured news sources
3. Articles are deduplicated and sent to **Claude Haiku** for relevance filtering and translation
4. Relevant articles are saved as `data/news/YYYY-MM-DD.json`
5. An **HTML email digest** is sent to the configured recipient
6. The **GitHub Pages dashboard** (`index.html`) automatically picks up new JSON files

## Managing Clients

Edit `data/clients.json`. Each client entry:

```json
{
  "name": "Client Name",
  "segment": "Pharma",
  "country": "PT",
  "aliases": ["Client Name", "Alternative Name"],
  "active": true
}
```

- **name**: Display name
- **segment**: One of: Automotive, Pharma, Aerospace & Defence, Research, Transport, Industrial, Cannabis, Own Brand, Own Orbit
- **country**: ISO 2-letter code
- **aliases**: Search terms used to query NewsAPI (first alias is the primary query)
- **active**: Set to `false` to skip a client without removing it

## Managing Sources

Edit `data/sources.json`. Each source:

```json
{
  "name": "Source Name",
  "domain": "example.com",
  "language": "en",
  "market": "EU"
}
```

The `domain` field is used to restrict NewsAPI searches to these outlets only.

## Manual Trigger

1. Go to the repository on GitHub
2. Click **Actions** tab
3. Select **Weekly Client News** workflow
4. Click **Run workflow**
5. Select the branch and click the green **Run workflow** button

## Required GitHub Secrets

Configure these in **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `NEWSAPI_KEY` | API key from [newsapi.org](https://newsapi.org) (free tier: 100 requests/day) |
| `ANTHROPIC_API_KEY` | API key from [Anthropic](https://console.anthropic.com) for Claude Haiku |
| `GMAIL_USER` | Gmail address used to send the digest email |
| `GMAIL_APP_PASSWORD` | Gmail App Password ([how to create](https://support.google.com/accounts/answer/185833)) — not your regular password |
| `RECIPIENT_EMAIL` | Email address that receives the weekly digest |

## GitHub Pages Dashboard

The `index.html` file is a self-contained dashboard (no build step required).

To enable:

1. Go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Choose `main` branch, `/ (root)` folder
4. Save

The dashboard loads JSON files from `data/news/` and provides:

- Dark/light mode toggle (persisted in localStorage)
- Date range filtering (7/14/30 days or custom)
- Client and segment filters
- Responsive card grid layout

## Project Structure

```
aralab-clients-news/
├── .github/workflows/
│   └── weekly_news.yml      # GitHub Actions workflow (weekly cron + manual)
├── data/
│   ├── clients.json          # Client list with aliases and segments
│   ├── sources.json          # News source domains
│   └── news/                 # Weekly JSON output files
│       └── .gitkeep
├── scripts/
│   └── fetch_news.py         # Main fetch, filter, translate, email script
├── index.html                # Static dashboard (GitHub Pages)
└── README.md
```

## Local Development

To run the script locally:

```bash
pip install requests anthropic

export NEWSAPI_KEY="your-key"
export ANTHROPIC_API_KEY="your-key"
export GMAIL_USER="your@gmail.com"
export GMAIL_APP_PASSWORD="your-app-password"
export RECIPIENT_EMAIL="recipient@example.com"

python scripts/fetch_news.py
```

To preview the dashboard, serve the repo root with any static server:

```bash
python -m http.server 8000
# Open http://localhost:8000
```
