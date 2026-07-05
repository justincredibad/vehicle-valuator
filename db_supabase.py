"""
Supabase-backed cache access, used by both streamlit_app.py (read + enqueue)
and scrape_worker.py (write + drain queue) — the shared store that lets the
Streamlit Cloud frontend and the GitHub Actions scraper talk to each other.

This is pure DB plumbing with no behavioral divergence between callers
(unlike tools.py vs tools_cloud.py, which genuinely differ on a cache miss),
so it's a single shared module rather than one copy per caller.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment.
streamlit_app.py copies them from st.secrets into os.environ at startup so
this module (and the GitHub Actions worker, which sets them directly as
workflow env vars) can read them the same way regardless of caller.
"""

from datetime import datetime, timezone
import os

from supabase import create_client, Client

CACHE_TTL_HOURS = 24  # mirrors config.CACHE_TTL_HOURS

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return _client


def is_cache_fresh(query_key: str) -> bool:
    resp = (
        get_client()
        .table("search_meta")
        .select("last_scraped_at")
        .eq("query_key", query_key)
        .execute()
    )
    if not resp.data:
        return False
    last_dt = datetime.fromisoformat(resp.data[0]["last_scraped_at"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return age_hours < CACHE_TTL_HOURS


def load_listings(query_key: str) -> list[dict]:
    resp = get_client().table("listings").select("*").eq("query_key", query_key).execute()
    return resp.data or []


def save_listings(query_key: str, listings: list[dict]):
    """Worker-only. Delete-then-insert mirrors db.save_listings' "replace
    this query_key's rows atomically" semantics (not truly transactional
    across the two calls, same known limitation as db.py's SQLite version)."""
    client = get_client()
    client.table("listings").delete().eq("query_key", query_key).execute()

    rows = [
        {
            "query_key": query_key,
            "title": item.get("title"),
            "price": item.get("price"),
            "reg_year": item.get("reg_year"),
            "coe_years_left": item.get("coe_years_left"),
            "mileage_km": item.get("mileage_km"),
            "depreciation_per_year": item.get("depreciation_per_year"),
            "url": item.get("url"),
        }
        for item in listings
    ]
    if rows:
        client.table("listings").insert(rows).execute()

    client.table("search_meta").upsert(
        {
            "query_key": query_key,
            "last_scraped_at": datetime.now(timezone.utc).isoformat(),
            "result_count": len(listings),
        },
        on_conflict="query_key",
    ).execute()


def enqueue_search_request(query_key: str, make: str, model: str, year_min: int, year_max: int):
    """Called by tools_cloud.py on a cache miss. Swallows a conflict if a
    pending row for this query_key already exists (partial unique index) —
    harmless, the worker will pick up the existing one regardless."""
    try:
        get_client().table("search_requests").insert(
            {
                "query_key": query_key,
                "make": make,
                "model": model,
                "year_min": year_min,
                "year_max": year_max,
                "status": "pending",
            }
        ).execute()
    except Exception:
        pass


def claim_pending_requests(limit: int = 5) -> list[dict]:
    """Worker-only."""
    resp = (
        get_client()
        .table("search_requests")
        .select("*")
        .eq("status", "pending")
        .limit(limit)
        .execute()
    )
    return resp.data or []


def mark_request_done(request_id: int, error: str | None = None):
    """Worker-only."""
    get_client().table("search_requests").update(
        {
            "status": "error" if error else "done",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": error,
        }
    ).eq("id", request_id).execute()
