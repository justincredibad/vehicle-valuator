"""
Central configuration for the sgcarmart valuer.

VERIFIED 2026-07-02 by rendering a live search
(https://www.sgcarmart.com/used-cars/listing?q=honda+vezel&avl=&) with
Selenium + headless Chrome. Key finding: sgcarmart is a Next.js app that
server-renders only loading-skeleton placeholders — real listing data
(prices, models, mileage, etc.) is injected client-side after hydration.
Plain `requests` only ever sees the skeleton, so USE_PLAYWRIGHT must stay
True. The SELECTORS below were read off the post-render DOM for a real
"honda vezel" search and confirmed against 20/20 cards on two separate
pages (page=1 and page=2).

CAVEAT: sgcarmart's classes are Next.js CSS-modules, e.g.
"styles_model_name__ZaHTI" — the "__ZaHTI" suffix is a build hash that can
change whenever they redeploy their frontend, even with no visible page
change. If scraping starts returning nothing, re-run the same Selenium
render-and-inspect approach used to build this file rather than guessing.

Before running this for real:
  1. Check https://www.sgcarmart.com/robots.txt and Terms of Use yourself
     for any scraping restrictions before running this at any real frequency.
  2. If selectors break, render the page with Selenium/Playwright (headless
     Chrome), save page_source, and re-inspect with BeautifulSoup — same
     process as before, since raw HTML alone won't show real data.

This file is the ONLY place selectors/URLs should live — scraper.py just
reads from here, so fixing a broken selector means editing one dict.
"""

import os

# ---------------------------------------------------------------------------
# Network behaviour
# ---------------------------------------------------------------------------

BASE_URL = "https://www.sgcarmart.com"
LISTING_PATH = "/used-cars/listing"   # confirmed against a real search URL

# Confirmed via a real search result (2026-07-02):
#   https://www.sgcarmart.com/used-cars/listing?q=honda+vezel&avl=&
# The search is a single free-text query param ("q") combining make+model —
# NOT separate make/model params. "avl" is left blank on a default search
# (likely an availability/status filter); left blank here until we know
# what non-default values it takes. "page" was tested directly (page=2
# returned a different set of 20 listings) — confirmed, not a guess.
SEARCH_QUERY_PARAM = "q"
AVAILABILITY_PARAM = "avl"
PAGE_PARAM = "page"

# sgcarmart's listing data is injected client-side after hydration (see
# module docstring) — plain `requests` only ever sees empty placeholders.
USE_PLAYWRIGHT = True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SECONDS = 15
DELAY_BETWEEN_REQUESTS_SECONDS = 2.0   # be polite — don't hammer the site
# stats.py's confidence already caps at "high" for >=15 raw listings, and
# each page returns ~20 — 2 pages (~40 listings) is generous headroom over
# that threshold while cutting worst-case fetch time well below the old
# 5-page cap.
MAX_PAGES_PER_SEARCH = 2

# ---------------------------------------------------------------------------
# CSS selectors — verified 2026-07-02 against the rendered DOM (see docstring)
# ---------------------------------------------------------------------------

SELECTORS = {
    # The repeating container for a single car listing on the results page.
    # Old generic guesses kept as fallbacks in case the real one breaks.
    "listing_card": "div.styles_listing_box__eDRd3, div.listing_item, "
                     "div.card_used_listing, article.listing",

    # Within a listing_card, where to find each field.
    # Multiple comma-separated fallbacks are tried in order — the verified
    # selector goes first, old guesses stay as a last-resort fallback.
    "price": ".styles_price_container__rI4oV .styles_price__PoUIK, "
             ".price, .listing_price, .car_price",
    "title": ".styles_model_name__ZaHTI, .car_title, .listing_title, h3 a, h2 a",
    "model_year": ".styles_reg_date_text__g7iO_, .reg_date, .year, .car_year",
    "mileage": ".listing_mileage_box__XvLqW .styles_detail_text__13VQe, "
               ".mileage, .car_mileage",
    "depreciation": ".styles_depreciation_text__I0yui, .depreciation, .dep_value",
    "link": "a.styles_text_link__wBaHL, a",
}

# ---------------------------------------------------------------------------
# Local LLM (Ollama) settings
# ---------------------------------------------------------------------------

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# ---------------------------------------------------------------------------
# Stats engine
# ---------------------------------------------------------------------------

PRICE_BUFFER_PCT = 0.10        # the +/-10% requirement
MIN_SAMPLE_SIZE = 5            # below this, agent should widen search / warn
IQR_OUTLIER_MULTIPLIER = 1.5   # standard Tukey fence multiplier
YEAR_WIDEN_STEP = 1            # how much to widen year range per retry
MAX_WIDEN_RETRIES = 2

# ---------------------------------------------------------------------------
# Local cache database
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "listings_cache.db")
)
CACHE_TTL_HOURS = 24  # re-scrape if cached data is older than this
