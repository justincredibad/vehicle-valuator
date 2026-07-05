"""
Streamlit Cloud entrypoint — the free-hosted version of this app. Same
pipeline as webapp.py/agent.py locally, but calls Gemini instead of a local
Ollama server, and reads/writes a shared Supabase cache instead of local
SQLite, since Streamlit Cloud can't run either of those locally. See
README.md "Cloud deployment" section for the full setup flow.
"""

import os
from datetime import datetime, timedelta, timezone

import streamlit as st

# Copy secrets into env vars before importing anything that reads them via
# os.environ (db_supabase.py, agent_gemini.py) — keeps those modules usable
# unchanged outside Streamlit too (e.g. scrape_worker.py sets the same env
# vars directly from GitHub Actions secrets).
for _key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GEMINI_API_KEY"):
    if _key in st.secrets:
        os.environ[_key] = st.secrets[_key]

import agent_gemini
import db_supabase


def _check_global_rate_limit(max_per_minute: int = 10, max_per_day: int = 1200) -> bool:
    """Global (not per-IP) limit — Streamlit Cloud doesn't expose caller IP
    as cleanly as Flask's request.remote_addr, and this is a personal-scale
    app, so a single shared counter is an explicit, acceptable
    simplification. Sized to stay comfortably under Gemini's shared free-tier
    ceiling (15 RPM / 1,500 RPD) even with occasional 2-call retries per query."""
    client = db_supabase.get_client()
    now = datetime.now(timezone.utc)
    minute_cutoff = (now - timedelta(minutes=1)).isoformat()
    day_cutoff = (now - timedelta(days=1)).isoformat()

    per_min = (
        client.table("rate_limit_log")
        .select("id", count="exact")
        .gte("called_at", minute_cutoff)
        .execute()
    )
    per_day = (
        client.table("rate_limit_log")
        .select("id", count="exact")
        .gte("called_at", day_cutoff)
        .execute()
    )
    if (per_min.count or 0) >= max_per_minute or (per_day.count or 0) >= max_per_day:
        return False

    client.table("rate_limit_log").insert({}).execute()
    return True


st.set_page_config(page_title="SG Car Valuator", page_icon="🚗")
st.title("SG Used Car Price Estimator")
st.caption(
    'Ask about a car, e.g. "2019 Honda Vezel" or '
    '"Toyota Corolla Altis 2017, at least 3 years COE left"'
)

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

user_query = st.chat_input("Ask about a car...")
if user_query:
    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.write(user_query)

    with st.chat_message("assistant"):
        if not _check_global_rate_limit():
            answer = "This app is getting a lot of use right now — please try again in a minute."
            st.warning(answer)
        else:
            with st.spinner("Searching sgcarmart data and asking Gemini..."):
                try:
                    answer = agent_gemini.run(user_query)
                except Exception as e:
                    answer = f"Sorry, something went wrong: {e}"
            st.write(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
