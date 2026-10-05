# Careers Finder

A job-search agent built on TinyFish. You set your preferences, it finds live openings across
company careers platforms and job portals, opens each posting to check it is real, removes
duplicates, and ranks what is left against what you asked for.

## Run it

```bash
pip install -r requirements.txt
export TINYFISH_API_KEY="your_key"      # from agent.tinyfish.ai/api-keys. Never commit or paste it anywhere.
python jobfinder.py serve               # web UI at http://127.0.0.1:8000
```

The UI has a **Basic** mode (role and location) and an **Advanced** mode: keywords, exclude words,
seniority, which platforms to search, posting age, visa sponsorship, remote on/off, and a minimum score.

Terminal version:

```bash
python jobfinder.py search --role "product designer" --location London --country GB \
  --levels mid,senior --keywords figma --exclude contract --json results.json
```

Tests (no network or API key needed, they replay real TinyFish responses): `python test_offline.py`

## How TinyFish is used (all three endpoints, each doing real work)

| Endpoint | What it does here |
|---|---|
| **Search** | Discovers live postings. One query per platform (Greenhouse, Lever, Ashby, Workable, public LinkedIn job pages, Reed) using `site:` operators, a freshness window (`recency_minutes`) and optional country targeting. This is what makes it work across many companies without a hardcoded list. |
| **Fetch** | Opens every shortlisted posting live (`ttl: 0`) in batches of 10. Used to confirm the link still works, read the real location, salary range and visa wording, and drop dead links. |
| **Agent** | Handles what Fetch cannot. (1) Ashby postings are JavaScript-only and Fetch returns `empty_content`, so the Agent browses them and returns structured JSON. (2) Optionally browses LinkedIn and Reed search result pages and extracts the listings. |

## What happens in a run

1. **Discover**: Search across the selected platforms, in parallel.
2. **Deduplicate**: tracking parameters are stripped from URLs, then near-duplicates (same company, near-identical title) are merged. A direct careers-page link is preferred over an aggregator link.
3. **Shortlist**: cheap title, seniority and freshness checks decide which postings are worth opening.
4. **Verify**: Fetch reads each posting. Expired postings (for example Greenhouse redirecting to "no current openings") are removed.
5. **Read hard pages**: the Agent covers pages Fetch cannot render.
6. **Rank**: each posting gets a 0 to 100 score from title match, keywords, location, seniority, freshness and visa wording, with the reasons shown on every result. Postings that state a different location, an excluded word, the wrong level or "no visa sponsorship" are dropped.
7. **Stay fresh**: results seen in earlier runs are remembered locally (`.seen_jobs.json`), so new ones are marked **new**.

## Things found while testing against the live API

- Search results do not always respect the location, so the app verifies location from the posting itself. A "London" result turned out to be San Mateo, CA.
- Expired Greenhouse postings return a normal page, not an error, so the app detects them from the redirect and page text.
- Fetch returns `empty_content` on Ashby, hence the Agent fallback.

## Limits and honest notes

- Public pages only. The app never logs in and never applies for you. LinkedIn and Indeed restrict automated access, so LinkedIn is read through public job pages and the Agent portal option is off by default.
- Free-tier Search and Fetch are rate limited. The app retries on HTTP 429. The Agent endpoint uses credits, so it is capped (`agent_fallback`, default 3 per run).
- The location, salary and visa checks are heuristics on page text. Always read the posting before applying.
