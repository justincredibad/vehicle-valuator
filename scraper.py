"""
Scraper for sgcarmart.com used car listings.

This module is deliberately defensive: sgcarmart's HTML WILL drift from
what's hardcoded in config.SELECTORS. Every parse function tries multiple
fallback selectors and multiple regex patterns for numbers, and logs a
warning (not a crash) when a field can't be found, so a partial scrape
still returns usable rows instead of dying on the first weird listing.

If USE_PLAYWRIGHT is True in config.py, install playwright first:
    pip install playwright --break-system-packages
    playwright install chromium
"""

import re
import time
import logging
from contextlib import contextmanager
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("scraper")


class ScraperError(Exception):
    pass


def _clean_int(text: str) -> int | None:
    """Extract an integer from messy text like '$45,800' or '88,000 km' or '12.3k/yr'."""
    if not text:
        return None
    text = text.replace(",", "")
    # "(k)?" must sit directly against the digits (no \s*) and not be
    # followed by "m" — otherwise "88,000 km" misparses as "88000 * 1000"
    # by treating the "k" in "km" as the thousands suffix from "12.3k".
    match = re.search(r"(\d+(?:\.\d+)?)(k)?(?!m)", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2):  # "k" suffix like 12.3k
        value *= 1000
    return int(value)


def _extract_year(text: str) -> int | None:
    """Pull a 4-digit year (19xx/20xx) out of a registration-date-style string."""
    if not text:
        return None
    match = re.search(r"(19|20)\d{2}", text)
    return int(match.group(0)) if match else None


_COE_BLOB_RE = re.compile(r"\(([^)]*?COE left[^)]*?)\)", re.IGNORECASE)
_COE_YEARS_TOKEN_RE = re.compile(r"(\d+)\s*y", re.IGNORECASE)
_COE_MONTHS_TOKEN_RE = re.compile(r"(\d+)\s*m", re.IGNORECASE)


def _parse_coe_years_left(text: str) -> float | None:
    """
    Parse the "(3y 4m COE left)" style parenthetical out of a reg-date blob
    into decimal years (years + months/12). Formats seen in the wild:
    "(3y 4m COE left)", "(5y  COE left)" (no months token), "(11m COE left)"
    (no years token, under 1 year remaining). Returns None if there's no
    "(...COE left...)" substring, or no digit could be found in it.
    """
    if not text:
        return None
    blob_match = _COE_BLOB_RE.search(text)
    if not blob_match:
        return None
    blob = blob_match.group(1)

    y_match = _COE_YEARS_TOKEN_RE.search(blob)
    m_match = _COE_MONTHS_TOKEN_RE.search(blob)
    if y_match is None and m_match is None:
        return None

    years = int(y_match.group(1)) if y_match else 0
    months = int(m_match.group(1)) if m_match else 0
    return round(years + months / 12, 2)


def _try_select(card, selector_key: str) -> str:
    """Try each comma-separated fallback selector in order, return first match's text."""
    selectors = [s.strip() for s in config.SELECTORS[selector_key].split(",")]
    for sel in selectors:
        el = card.select_one(sel)
        if el:
            return el.get_text(strip=True)
    return ""


def _try_select_href(card) -> str:
    selectors = [s.strip() for s in config.SELECTORS["link"].split(",")]
    for sel in selectors:
        el = card.select_one(sel)
        if el and el.get("href"):
            href = el["href"]
            return href if href.startswith("http") else config.BASE_URL + href
    return ""


def _fetch_html(url: str, page=None) -> str:
    if config.USE_PLAYWRIGHT:
        return _fetch_html_playwright(url, page)
    headers = {"User-Agent": config.USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def _fetch_html_playwright(url: str, page) -> str:
    """Fetch a URL's rendered HTML using an already-open Playwright page.
    The caller (search_listings) owns the browser/page lifecycle — reusing
    one page across a multi-page search avoids re-launching a whole browser
    per page, which was the single biggest source of latency here (browser
    startup alone can cost 1-2+ seconds, on top of every page load)."""
    page.goto(url, timeout=config.REQUEST_TIMEOUT_SECONDS * 1000)
    # "networkidle" is unreliable on ad/analytics-heavy sites like this one
    # — background beacons keep firing forever, so the page never goes
    # fully idle and the wait times out even though the content we care
    # about (listing cards) has long since rendered. Wait for that
    # content directly instead; if it never shows up (selectors broken,
    # or a genuine no-results page), fall through and return whatever
    # HTML we have — parse_listing_page already handles "no cards found"
    # gracefully rather than crashing.
    try:
        page.wait_for_selector(
            config.SELECTORS["listing_card"].split(",")[0].strip(),
            timeout=config.REQUEST_TIMEOUT_SECONDS * 1000,
        )
    except Exception as e:
        log.warning(f"Timed out waiting for listing cards to render: {e}")
    return page.content()


@contextmanager
def _playwright_page():
    """Launch one browser + page for the duration of a whole search_listings()
    call (all pages of one query share it), and block image/media/font
    requests — we only read text out of the HTML, so skipping those
    resources cuts real page-load time on this ad-heavy site without
    affecting what we scrape."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ScraperError(
            "USE_PLAYWRIGHT is True but playwright isn't installed. "
            "Run: pip install playwright --break-system-packages && playwright install chromium"
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=config.USER_AGENT)
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        try:
            yield page
        finally:
            browser.close()


def parse_listing_page(html: str) -> list[dict]:
    """Parse one results page into a list of listing dicts. Skips unparseable cards."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(config.SELECTORS["listing_card"])

    if not cards:
        log.warning(
            "No listing cards found with current selectors. "
            "The site structure has likely changed — update config.SELECTORS."
        )
        return []

    results = []
    for card in cards:
        try:
            title = _try_select(card, "title")
            price = _clean_int(_try_select(card, "price"))
            reg_date_text = _try_select(card, "model_year")
            reg_year = _extract_year(reg_date_text)
            coe_years_left = _parse_coe_years_left(reg_date_text)
            mileage = _clean_int(_try_select(card, "mileage"))
            depreciation = _clean_int(_try_select(card, "depreciation"))
            url = _try_select_href(card)

            if price is None or not title:
                # Can't use a listing with no price or no title — skip silently
                continue

            results.append({
                "title": title,
                "price": price,
                "reg_year": reg_year,
                "coe_years_left": coe_years_left,
                "mileage_km": mileage,
                "depreciation_per_year": depreciation,
                "url": url,
            })
        except Exception as e:
            log.warning(f"Skipped a listing card due to parse error: {e}")
            continue

    return results


def search_listings(make: str, model: str, year_min: int, year_max: int,
                     max_pages: int | None = None) -> list[dict]:
    """
    Search sgcarmart for used listings matching make/model within a year range.

    The site takes a single free-text query (e.g. "honda vezel") rather than
    separate make/model params — see config.SEARCH_QUERY_PARAM. There's no
    confirmed year-range param, so year filtering is done client-side on the
    parsed reg_year here instead of via the query string.
    """
    max_pages = max_pages or config.MAX_PAGES_PER_SEARCH
    all_listings = []

    def _run(page):
        for page_num in range(1, max_pages + 1):
            params = {
                config.SEARCH_QUERY_PARAM: f"{make} {model}",
                config.AVAILABILITY_PARAM: "",
                config.PAGE_PARAM: page_num,
            }
            url = f"{config.BASE_URL}{config.LISTING_PATH}?{urlencode(params)}"
            log.info(f"Fetching: {url}")

            try:
                html = _fetch_html(url, page)
            except requests.RequestException as e:
                log.warning(f"Request failed on page {page_num}: {e}")
                break

            page_listings = parse_listing_page(html)
            if not page_listings:
                break  # no more results, or selectors are broken

            page_listings = [
                r for r in page_listings
                if r["reg_year"] is None or year_min <= r["reg_year"] <= year_max
            ]
            all_listings.extend(page_listings)

            if page_num < max_pages:
                time.sleep(config.DELAY_BETWEEN_REQUESTS_SECONDS)

    if config.USE_PLAYWRIGHT:
        with _playwright_page() as page:
            _run(page)
    else:
        _run(None)

    return all_listings


if __name__ == "__main__":
    # Quick manual smoke test — run `python scraper.py` after fixing selectors
    results = search_listings("Honda", "Vezel", 2018, 2020, max_pages=1)
    print(f"Found {len(results)} listings")
    for r in results[:5]:
        print(r)
