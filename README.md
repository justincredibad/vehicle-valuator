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

## File guide

| File | Purpose | Uses LLM? |
|---|---|---|
| `config.py` | All selectors, URLs, model settings — edit here first | No |
| `scraper.py` | Fetches & parses sgcarmart listing pages | No |
| `db.py` | SQLite cache of scraped listings (24h TTL by default) | No |
| `stats.py` | Outlier filtering, median, ±10% buffer — the actual pricing math | No |
| `tools.py` | Wraps scraper+stats as a tool schema the LLM can call | No |
| `agent.py` | Ollama chat loop: parses query, calls tools, writes final answer | **Yes (local only)** |
| `main.py` | CLI entry point | — |
| `webapp.py` | Flask web front-end (same pipeline as `main.py`, over HTTP) | — |

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
