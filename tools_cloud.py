"""
Cloud variant of tools.py's search_and_estimate, for use behind Streamlit
Cloud. Same signature, same COE-filtering/coe_summary/notes logic as
tools.py for the success path — the actual price math (stats.py) is shared
unmodified. The one real difference: on a cache miss, this enqueues a scrape
request for the GitHub Actions worker instead of scraping inline, since
Streamlit Cloud's 1GB RAM free tier can't run headless Chromium reliably
(see scraper.py / config.py).

Kept as a parallel file rather than a branch inside tools.py so the
already-verified local/self-hosted path has zero regression risk from this
cloud deployment — see the implementation plan for the full reasoning.
"""

import logging

import db_supabase as db
import stats

log = logging.getLogger("tools_cloud")


def _query_key(make: str, model: str, year_min: int, year_max: int) -> str:
    return f"{make.strip().lower()}|{model.strip().lower()}|{year_min}-{year_max}"


def _coe_summary(listings: list[dict]) -> dict | None:
    """Aggregate COE-years-left across listings that had parseable COE data."""
    values = [l["coe_years_left"] for l in listings if l.get("coe_years_left") is not None]
    if not values:
        return None
    return {
        "avg_years_left": round(sum(values) / len(values), 1),
        "min_years_left": round(min(values), 1),
        "max_years_left": round(max(values), 1),
        "listings_with_coe_data": len(values),
    }


def search_and_estimate(make: str, model: str, year_min: int, year_max: int,
                         target_year: int | None = None,
                         min_coe_years_left: float | None = None) -> dict:
    """
    Same contract as tools.search_and_estimate, but backed by Supabase
    instead of local SQLite, and never scrapes inline. If the cache is
    missing or stale, this enqueues a scrape request for the GitHub Actions
    worker and returns reason="queued_for_scraping" instead of blocking.
    """
    key = _query_key(make, model, year_min, year_max)

    if db.is_cache_fresh(key):
        log.info(f"Using cached listings for {key}")
        listings = db.load_listings(key)
    else:
        db.enqueue_search_request(key, make, model, year_min, year_max)
        return {
            "success": False,
            "reason": "queued_for_scraping",
            "message": (
                f"No fresh cached data for {make} {model} ({year_min}-{year_max}) yet. "
                "This search has been queued and will be scraped shortly (usually "
                "within the next scheduled run, up to ~15 minutes). Please try again "
                "in a few minutes."
            ),
        }

    if not listings:
        return {
            "success": False,
            "reason": "no_listings_found",
            "message": (
                f"No listings found for {make} {model} ({year_min}-{year_max}). "
                "Try a different model name spelling, or widen the year range."
            ),
        }

    listings_for_stats = listings
    coe_filter_note = None
    if min_coe_years_left is not None:
        filtered = [
            l for l in listings
            if l.get("coe_years_left") is not None and l["coe_years_left"] >= min_coe_years_left
        ]
        if not filtered:
            return {
                "success": False,
                "reason": "no_listings_matching_coe_constraint",
                "message": (
                    f"Found {len(listings)} listing(s) for {make} {model} "
                    f"({year_min}-{year_max}), but none had at least "
                    f"{min_coe_years_left} year(s) of COE remaining (or COE info "
                    "wasn't available for them). Try loosening the COE "
                    "requirement or widening the year range."
                ),
            }
        listings_for_stats = filtered
        coe_filter_note = (
            f"Filtered to listings with at least {min_coe_years_left} year(s) of "
            f"COE remaining ({len(filtered)} of {len(listings)} total listings qualified)."
        )

    estimate = stats.estimate_price(listings_for_stats, target_year=target_year)
    if estimate is None:
        return {
            "success": False,
            "reason": "no_usable_prices",
            "message": "Listings were found but none had a parseable price.",
        }

    notes = list(estimate.notes)
    if coe_filter_note:
        notes.append(coe_filter_note)

    return {
        "success": True,
        "make": make,
        "model": model,
        "year_range_searched": [year_min, year_max],
        "target_year": target_year,
        "min_coe_years_left_applied": min_coe_years_left,
        "point_estimate_sgd": estimate.point_estimate,
        "low_sgd": estimate.low,
        "high_sgd": estimate.high,
        "buffer_pct": 10,
        "sample_size": estimate.sample_size,
        "outliers_removed": estimate.outliers_removed,
        "confidence": estimate.confidence,
        "coe_summary": _coe_summary(listings_for_stats),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Tool schemas (Gemini's functionDeclarations format — same shape as
# tools.py's TOOL_SCHEMAS content, just without the OpenAI-style
# {"type": "function", "function": {...}} wrapper Gemini doesn't use)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "search_and_estimate",
        "description": (
            "Search sgcarmart.com for used car listings matching a make, "
            "model, and year range, and return a statistically-derived "
            "price estimate with a 10% buffer (low/high band). Call this "
            "whenever you need a price for a specific vehicle. If the "
            "result has low confidence or few samples, you can call this "
            "again with a wider year_min/year_max range. The result always "
            "includes a coe_summary (average/min/max years of COE "
            "remaining across the listings used), regardless of whether a "
            "COE filter was applied. If the data isn't cached yet, this "
            "queues a scrape and returns reason='queued_for_scraping' "
            "instead of a price — that is not an error and not a "
            "no-comparables case, just not ready yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "make": {
                    "type": "string",
                    "description": "Vehicle make, e.g. 'Honda', 'Toyota'. Normalize common abbreviations.",
                },
                "model": {
                    "type": "string",
                    "description": "Vehicle model, e.g. 'Vezel', 'Civic'. Strip trim/variant unless essential.",
                },
                "year_min": {
                    "type": "integer",
                    "description": "Earliest registration year to include in the search.",
                },
                "year_max": {
                    "type": "integer",
                    "description": "Latest registration year to include in the search.",
                },
                "target_year": {
                    "type": "integer",
                    "description": (
                        "The exact year the user asked about, if a single year "
                        "(not a range) was given. Used to weight same-year listings "
                        "more heavily in the estimate."
                    ),
                },
                "min_coe_years_left": {
                    "type": "number",
                    "description": (
                        "Minimum years of COE (Certificate of Entitlement) "
                        "remaining that matching listings must have, if the user "
                        "specified a COE constraint (e.g. 'at least 3 years COE "
                        "left'). Omit this entirely if the user didn't mention "
                        "COE — do not assume a constraint. When set, listings "
                        "with less COE remaining, or with unknown COE data, are "
                        "excluded before computing the price estimate."
                    ),
                },
            },
            "required": ["make", "model", "year_min", "year_max"],
        },
    }
]

# Maps tool name -> actual Python function, used by the agent loop to dispatch calls
TOOL_DISPATCH = {
    "search_and_estimate": search_and_estimate,
}
