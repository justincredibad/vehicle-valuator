# sgcarmart Local Price Valuer

Fully local agentic price estimator: scrapes sgcarmart.com used car listings,
computes a price estimate with a ±10% buffer using pure statistics, and uses
a **local LLM via Ollama** (no API keys, no external tokens, no cloud LLM
calls) to orchestrate the search and explain results in plain language.

Understands free-text queries with an optional COE (Certificate of
Entitlement) requirement, e.g. `"2019 Honda Vezel with at least 3 years COE
left"` — the price estimate is filtered to listings meeting that constraint,
and the answer always mentions typical COE years remaining among matched
listings. Available both as a CLI (`main.py`) and a web app (`webapp.py`)
you can self-host.

```
Your query  ─►  Local LLM (Ollama)  ─►  decides search params
                       │
                       ▼
              search_and_estimate tool
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
       scraper.py            db.py (cache)
            │
            ▼
        stats.py  ──►  median + outlier filter + ±10% buffer
            │
            ▼
   Local LLM formats final answer
```

The LLM **never invents a price** — it only calls the tool and reports the
numbers stats.py computed. The actual valuation math is 100% deterministic
and auditable.

---

## ⚠️ Before you run this for real

This was built without live access to sgcarmart.com (sandboxed build
environment), so:

1. **Check `robots.txt` and Terms of Use yourself**: visit
   `https://www.sgcarmart.com/robots.txt` and sgcarmart's Terms of Use.
   Respect any disallowed paths and rate limits. This tool defaults to a
   2-second delay between requests and a 5-page cap per search — adjust in
   `config.py`, but don't remove the politeness delay.
2. **Verify the page structure**: open
   `https://www.sgcarmart.com/used-cars/listing` in a browser:
   - View Source (Ctrl+U) — if listing data is in the raw HTML, plain
     `requests` works fine (default). If the page is mostly empty until JS
     runs, set `USE_PLAYWRIGHT = True` in `config.py` and install Playwright.
   - Inspect a listing card and update `SELECTORS` in `config.py` to match
     the real CSS classes/structure.
   - Submit a real search and check the resulting URL's query parameters —
     update the `params` dict in `scraper.py`'s `search_listings()` to match
     (the field names there, e.g. `yr_from`/`yr_to`, are a best guess).

Everything selector/URL-related lives in **`config.py`** so fixes are
localized to one file.

---

## Setup

### 1. Install Ollama

**macOS:**
```bash
brew install ollama
```
Or download the installer from https://ollama.com/download

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

**Windows:** download the installer from https://ollama.com/download

### 2. Start Ollama and pull a model

In one terminal, start the Ollama server (if it isn't already running as a
background service after install):
```bash
ollama serve
```

In another terminal, pull a tool-calling-capable model. With 12GB+ VRAM:
```bash
ollama pull qwen2.5:14b
```
If you have more headroom (24GB+ VRAM), you can use a larger model instead:
```bash
ollama pull qwen2.5:32b
```
Then set `OLLAMA_MODEL` in `config.py` (or via env var, see below) to match.

### 3. Install Python dependencies

```bash
cd sgcarmart-valuer
pip install -r requirements.txt
```

If you set `USE_PLAYWRIGHT = True` in `config.py`:
```bash
pip install playwright
playwright install chromium
```

### 4. (Optional) environment overrides

```bash
export OLLAMA_HOST=http://localhost:11434   # default, change if remote
export OLLAMA_MODEL=qwen2.5:14b              # match whatever you pulled
```

---

## Usage

```bash
python main.py "2019 Honda Vezel"
python main.py "Honda Vezel with at least 3 years COE left"
```

Or interactive mode:
```bash
python main.py
```

Example output:
```
--- 2019 Honda Vezel ---
Based on 14 sgcarmart listings for the 2019 Honda Vezel, the estimated
market price is SGD 49,750, with a 10% buffer range of SGD 44,775 –
54,725. Confidence: medium (one outlier listing was excluded). Typical
COE remaining among these listings: ~4.2 years (range 2.1–6.8).
```

---

## Web app

A minimal Flask front-end (`webapp.py`) wraps the same pipeline behind a
single search box, for hosting online.

**Local dev:**
```bash
pip install -r requirements.txt
python webapp.py
```
Then open `http://localhost:8000`. Requires `ollama serve` running locally,
same as the CLI.

**Deploying online:** this needs a persistent server (not serverless) since
scraping requires headless Chrome via Playwright, and it runs its own
Ollama instance rather than a hosted LLM API. A `Dockerfile` +
`docker-compose.yml` are included, bundling the web app and an
`ollama/ollama` container together:

1. Provision a VPS with enough RAM for the model — a `qwen2.5:14b` model at
   typical quantization needs ~9 GB resident, so 16 GB+ RAM total is a
   reasonable floor (e.g. Hetzner CPX41: 8 vCPU / 16 GB RAM, Singapore
   region, for low latency to sgcarmart.com and to users in SG).
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Copy this repo to the server.
4. `docker compose up -d --build`
5. Pull the model once (persists in the `ollama_data` volume):
   `docker compose exec ollama ollama pull qwen2.5:14b`
6. Visit `http://<server-ip>/` — served over plain HTTP on the bare IP by
   default (no domain/TLS configured). To add a domain + automatic HTTPS
   later, put a `caddy` service in front reverse-proxying to `web:8000` —
   no application code changes needed.

Notes on the deployed setup:
- Runs under gunicorn with **1 worker, 1 thread** on purpose — a query can
  take tens of seconds (CPU-only LLM inference + a fresh headless-Chrome
  scrape), and more workers would just contend for the same CPU/RAM rather
  than adding real concurrency at this scale.
- `/api/query` is rate-limited (6/min, 60/hour per IP) since the app has no
  login — a minimal safety net against a public URL getting hammered and
  running up scraping/LLM load.
- Ollama's port (11434) is not published outside the Docker network — only
  the web app's port 80 is exposed.

---

## Cloud deployment (free — Streamlit Community Cloud)

An alternative, fully free deployment that needs **no server or desktop kept
running**: [Streamlit Community Cloud](https://streamlit.io/cloud) hosts the
UI, [Google Gemini's free tier](https://ai.google.dev/gemini-api/docs) replaces
local Ollama, and [Supabase](https://supabase.com) (Postgres) replaces local
SQLite as a cache shared between Streamlit and a scheduled
[GitHub Actions](https://github.com/features/actions) job that does the actual
scraping — Streamlit Cloud's free tier (1GB RAM, no persistent processes)
can't run a self-hosted LLM or headless Chrome reliably, so those stay
outside it.

```
User  ─►  streamlit_app.py  ─►  agent_gemini.py  ─►  Gemini API (free tier)
                                       │
                                       ▼
                              tools_cloud.py
                                       │
                          ┌────────────┴────────────┐
                          ▼                          ▼
                 db_supabase.py               (cache miss →
                 (Supabase Postgres)         enqueue in search_requests)
                                                       │
                                                       ▼
                                  GitHub Actions (every ~15 min)
                                  runs scrape_worker.py ─► scraper.py (unmodified)
                                  ─► writes back to Supabase
```

A cache miss doesn't scrape inline here — it queues the search and tells the
user to check back shortly once the next scheduled GitHub Actions run has
scraped it. This is the one real behavioral difference from the local path.

### One-time setup

1. **Supabase**: create a free project at supabase.com, then run
   `supabase/migrations/001_init.sql` in its SQL Editor. Copy the project's
   URL and `service_role` key (Project Settings → API).
2. **Gemini**: create a free API key at
   [aistudio.google.com](https://aistudio.google.com/apikey), on a Google
   Cloud project that has **never had billing enabled** (enabling billing
   later removes the free tier for that project permanently). Note: on the
   free tier, Google's terms permit using your queries to improve their
   models — worth knowing before pointing real users at it.
3. **Streamlit Cloud**: go to [share.streamlit.io](https://share.streamlit.io),
   sign in with GitHub, "New app" → this repo → branch `main` → main file
   `streamlit_app.py`. In that app's Settings → Secrets, paste:
   ```toml
   SUPABASE_URL = "https://xxxx.supabase.co"
   SUPABASE_SERVICE_ROLE_KEY = "eyJ..."
   GEMINI_API_KEY = "AIza..."
   ```
4. **GitHub Actions**: in this repo's Settings → Secrets and variables →
   Actions, add `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (same values
   as step 1 — the worker never calls Gemini, so no Gemini key needed here).

### Notes

- Rate limiting here is **global, not per-visitor** (Streamlit Cloud doesn't
  expose caller IP as cleanly as Flask) — sized to stay under Gemini's shared
  free-tier ceiling (15 requests/min, 1,500/day). One heavy user can briefly
  throttle everyone else; acceptable at personal/demo scale, not for real
  concurrent traffic.
- Free Supabase projects auto-pause after 7 days with no DB traffic — the
  scheduled GitHub Actions run doubles as a keep-alive ping.
- The scrape schedule (`.github/workflows/scrape.yml`, every 15 min by
  default) can be triggered manually via its `workflow_dispatch` trigger to
  pre-warm a specific search before a demo, instead of waiting for the next
  scheduled run.
- This cloud path and the local path (below) are independent — `config.py`,
  `scraper.py`, and `stats.py` are shared unmodified between them, but
  `tools.py`/`agent.py`/`db.py`/`webapp.py` (local) and
  `tools_cloud.py`/`agent_gemini.py`/`db_supabase.py`/`streamlit_app.py`
  (cloud) are separate, parallel implementations — see the file guide below.

---

## File guide

| File | Purpose | Uses LLM? |
|---|---|---|
| `config.py` | All selectors, URLs, model settings — edit here first | No |
| `scraper.py` | Fetches & parses sgcarmart listing pages | No |
| `stats.py` | Outlier filtering, median, ±10% buffer — the actual pricing math | No |
| **Local path** | | |
| `db.py` | SQLite cache of scraped listings (24h TTL by default) | No |
| `tools.py` | Wraps scraper+stats as a tool schema the LLM can call | No |
| `agent.py` | Ollama chat loop: parses query, calls tools, writes final answer | **Yes (local only)** |
| `main.py` | CLI entry point | — |
| `webapp.py` | Flask web front-end (same pipeline as `main.py`, over HTTP) | — |
| **Cloud path** (Streamlit) | | |
| `db_supabase.py` | Supabase-backed cache + scrape-request queue, shared with the GitHub Actions worker | No |
| `tools_cloud.py` | Cloud variant of `tools.py` — same logic, but enqueues a scrape instead of running one inline | No |
| `agent_gemini.py` | Same role as `agent.py`, calls Gemini's free API instead of local Ollama | **Yes (cloud)** |
| `streamlit_app.py` | Streamlit Cloud entry point (chat UI) | — |
| `scrape_worker.py` | Runs in GitHub Actions: drains the queue, scrapes with the unmodified `scraper.py`, writes back to Supabase | No |

## Testing the non-LLM parts without Ollama running

`stats.py` and `db.py` can be sanity-checked standalone:
```bash
python stats.py   # runs a built-in example with fake listings
python db.py       # just initializes the SQLite schema
```

`scraper.py` has a `__main__` block for a manual smoke test once selectors
are verified:
```bash
python scraper.py
```

## Troubleshooting

- **"Couldn't reach Ollama"** — make sure `ollama serve` is running and
  `OLLAMA_HOST` matches.
- **"No listing cards found"** — sgcarmart's HTML structure doesn't match
  `config.SELECTORS`. Re-inspect the live page and update the selectors.
- **Few/no results for a real model** — check the `params` dict in
  `scraper.search_listings()` matches sgcarmart's actual query string format.
- **Model won't stop calling tools** — `agent.py`'s `max_tool_rounds` (default
  4) is a hard safety cap independent of the model's own behavior.
