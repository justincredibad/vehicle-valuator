-- Shared Supabase schema for the Streamlit Cloud deployment.
-- Run once via the Supabase SQL Editor. See README.md "Cloud deployment"
-- section for the full setup flow.

create table if not exists listings (
    id bigint generated always as identity primary key,
    query_key text not null,
    title text,
    price integer,
    reg_year integer,
    coe_years_left real,
    mileage_km integer,
    depreciation_per_year integer,
    url text,
    scraped_at timestamptz not null default now()
);
create index if not exists idx_listings_query_key on listings(query_key);

create table if not exists search_meta (
    query_key text primary key,
    last_scraped_at timestamptz not null,
    result_count integer not null
);

-- Queue the GitHub Actions scraper worker drains. Streamlit inserts a
-- pending row on a cache miss instead of scraping inline (Streamlit Cloud's
-- 1GB RAM tier can't run headless Chromium reliably).
create table if not exists search_requests (
    id bigint generated always as identity primary key,
    query_key text not null,
    make text not null,
    model text not null,
    year_min integer not null,
    year_max integer not null,
    status text not null default 'pending' check (status in ('pending', 'done', 'error')),
    requested_at timestamptz not null default now(),
    completed_at timestamptz,
    error_message text
);
create index if not exists idx_search_requests_status on search_requests(status);
-- At most one *pending* row per query_key — avoids piling up duplicate
-- enqueues if Streamlit reruns while a request is still pending.
create unique index if not exists idx_search_requests_pending_key
    on search_requests(query_key) where status = 'pending';

-- Global (not per-IP) request counter protecting the shared Gemini
-- free-tier rate limit. See streamlit_app.py's _check_global_rate_limit.
create table if not exists rate_limit_log (
    id bigint generated always as identity primary key,
    called_at timestamptz not null default now()
);

-- RLS enabled with ZERO policies on every table, on purpose: only the
-- service_role key (used server-side by both Streamlit and the GitHub
-- Actions worker, never exposed to a browser) can read/write. There is no
-- anon-key client access path in this app, so no policies are needed —
-- do NOT add permissive anon-key policies "for convenience" later, that
-- would let anyone with the public anon key bypass rate limiting entirely.
alter table listings enable row level security;
alter table search_meta enable row level security;
alter table search_requests enable row level security;
alter table rate_limit_log enable row level security;
