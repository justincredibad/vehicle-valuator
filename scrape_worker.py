"""
GitHub Actions scraper worker for the Streamlit Cloud deployment.

Drains pending rows from Supabase's search_requests queue and scrapes them
with the existing, completely unmodified scraper.search_listings() — this
runs on a GitHub-hosted runner (~7GB RAM) rather than Streamlit Cloud's 1GB
free tier, since headless Chromium needs real resources to run reliably.

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment (set
as GitHub Actions repo secrets — see .github/workflows/scrape.yml).
"""

import logging

import db_supabase as db
import scraper

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("scrape_worker")


def main():
    pending = db.claim_pending_requests(limit=5)
    if not pending:
        log.info("No pending requests.")
    else:
        for req in pending:
            key = req["query_key"]
            try:
                listings = scraper.search_listings(
                    req["make"], req["model"], req["year_min"], req["year_max"]
                )
                db.save_listings(key, listings)
                db.mark_request_done(req["id"])
                log.info(f"Scraped {len(listings)} listings for {key}")
            except Exception as e:
                log.exception(f"Failed to scrape {key}")
                db.mark_request_done(req["id"], error=str(e))

    # Any read is enough traffic to reset Supabase's 7-day free-tier
    # auto-pause clock, even when there was nothing to scrape this run.
    db.get_client().table("search_meta").select("query_key").limit(1).execute()


if __name__ == "__main__":
    main()
