"""
Cloud agent loop — same role and rules as agent.py, but calls Google's
Gemini API (gemini-2.5-flash, free tier) instead of a local Ollama server,
since Streamlit Community Cloud can't run a self-hosted model. Uses
tools_cloud's schemas/dispatch instead of tools.py's.

⚠️ VERIFY BEFORE TRUSTING: this was written against Gemini's documented
generateContent function-calling shape (systemInstruction, functionCall/
functionResponse parts, functionDeclarations) as of the research done when
this file was written, but Google revises these between API versions. The
first real run against a live API key should be a single trivial call (no
tools) to sanity-check the response shape before relying on the full
tool-calling loop below.

Same non-negotiable rule as agent.py: the model does NOT compute prices for
a search it has real data for — it calls the tool and reports the tool's
numbers back. The same narrow exception applies (a clearly-labeled rough
estimate only when the tool genuinely finds zero comparables after a
retry), plus one new state this cloud version has to handle that agent.py
doesn't: a cache miss here means "queued for scraping", not "no data" —
see SYSTEM_PROMPT step 5a.
"""

import logging
import os
from datetime import date

import requests

import config
import tools_cloud as tools

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("agent_gemini")


SYSTEM_PROMPT = """You are an assistant that estimates used car prices \
from sgcarmart.com data for the Singapore market.

Today's date is {today}, so the current year is {current_year} — use this \
as the reference point for any date/year math below.

You have ONE tool available: search_and_estimate. You must call it to get \
any real price information — never invent or guess a price on a search you \
haven't tried yet, or in place of trying.

Workflow:
1. Parse the user's request into make, model, and year.
   - If they give a single year (e.g. "2019 Honda Vezel"), set target_year \
to that year, and search a small range around it, e.g. year_min=2018, \
year_max=2020, to get enough sample listings.
   - Normalize common shorthand (e.g. "Civic" alone implies make=Honda).
   - If the user mentions a COE (Certificate of Entitlement) constraint as \
a number of years, e.g. "at least 3 years COE left" or "3+ years COE \
remaining", pass that number directly as min_coe_years_left.
   - If the user instead mentions an absolute COE expiry, e.g. "COE ending \
2030" or "COE expires in 2030", convert it to years remaining by \
subtracting the CURRENT year ({current_year}) from that expiry year — e.g. \
"COE ending 2030" in {current_year} means min_coe_years_left = 2030 - \
{current_year}. Do NOT subtract the vehicle's manufacturing/registration \
year for this — COE years-left is always measured from today, not from \
when the car was made.
   - If the user doesn't mention COE at all, omit min_coe_years_left \
entirely — do not assume a constraint.
2. Call search_and_estimate with those parameters.
3. Look at the result:
   - If reason is "queued_for_scraping", the data simply isn't cached yet — \
this is NOT an error and NOT a zero-comparables case. Tell the user their \
search was queued and to check back in a few minutes. Do not retry \
immediately (retrying just re-queues the same request) and do NOT apply \
the rough-fallback-estimate rule in step 5 for this reason.
   - If success is false for another reason, or confidence is "low" / \
sample_size is small (under {min_sample}), you may call search_and_estimate \
ONCE more with a wider year_min/year_max (widen by {widen_step} year(s) on \
each side). Keep the same min_coe_years_left if one was set — do not loosen \
a COE requirement the user gave you, unless you suspect you mis-converted \
an absolute expiry year the first time (see above) — in that case, \
recompute it correctly and retry once with the corrected number.
   - Do not call the tool more than {max_retries} extra times total.
4. Once you have a usable result (reason "success"), write a short final \
answer for the user:
   - State the estimated price and the low-high buffer range clearly in SGD.
   - Mention sample size and confidence level briefly.
   - If the result includes a coe_summary, mention the typical COE years \
remaining (e.g. average, and range) among the matched listings — useful \
context even if the user didn't ask for a COE filter.
   - If a COE constraint was applied (min_coe_years_left is set), say so \
explicitly so the user knows the price reflects only listings meeting that \
requirement.
   - Pass along any notes/caveats from the tool result.
5. If, after retrying, the tool still finds genuinely no comparable \
listings (reason "no_listings_found" or "no_listings_matching_coe_constraint" \
— meaning the tool ran fine but nothing matched, e.g. a rare/exotic car \
sgcarmart doesn't list), you MAY give a rough fallback estimate from your \
own general knowledge instead of refusing outright. If you do:
   - Start the answer with a clear warning that this is NOT based on real \
sgcarmart listings and could be significantly off — e.g. "⚠️ No real \
listings were found for this car, so this is a rough general estimate, \
not derived from actual sgcarmart data:".
   - Give a single approximate SGD figure or a wide range, reasoning from \
the vehicle's typical original price, age, and how Singapore's COE/ARF/PARF \
system generally affects used values — briefly explain your reasoning.
   - Do NOT invent a fake sample size, confidence level, or COE summary — \
those numbers only exist when the tool actually returns real data.
   - Do NOT use this fallback if the tool call itself errored (reason \
"tool_error" or similar), or returned "queued_for_scraping" — neither of \
those means the search genuinely found zero matches, so say so plainly and \
suggest the user try again, rather than guessing.
   - If you never got usable data and this fallback doesn't apply either, \
say so plainly and suggest what the user could try (different spelling, \
broader year range, a looser COE requirement, etc.) — do not make up a \
number.

Be concise. Do not show your reasoning steps to the user, just the final \
answer.""".format(
    today=date.today().isoformat(),
    current_year=date.today().year,
    min_sample=config.MIN_SAMPLE_SIZE,
    widen_step=config.YEAR_WIDEN_STEP,
    max_retries=config.MAX_WIDEN_RETRIES,
)


GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)


class GeminiAgentError(Exception):
    pass


def _call_gemini(contents: list[dict]) -> dict:
    api_key = os.environ["GEMINI_API_KEY"]
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "tools": [{"functionDeclarations": tools.TOOL_SCHEMAS}],
    }
    try:
        resp = requests.post(f"{GEMINI_URL}?key={api_key}", json=payload, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise GeminiAgentError(f"Couldn't reach Gemini API: {e}")
    return resp.json()


def run(user_query: str, max_tool_rounds: int = 4) -> str:
    """
    Run the agent loop for one user query. Returns the final text answer.
    Same contract as agent.py's run() — stateless per call, caller (e.g.
    streamlit_app.py) owns any chat-history display.
    """
    contents = [{"role": "user", "parts": [{"text": user_query}]}]

    for round_num in range(max_tool_rounds):
        response = _call_gemini(contents)
        candidates = response.get("candidates") or []
        if not candidates:
            raise GeminiAgentError(f"Gemini returned no candidates: {response}")

        model_content = candidates[0].get("content", {})
        parts = model_content.get("parts", [])
        fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not fn_calls:
            final_text = "".join(p.get("text", "") for p in parts).strip()
            if not final_text:
                raise GeminiAgentError("Model returned an empty response.")
            return final_text

        # Append the model's own turn (including functionCall parts) to history
        contents.append({"role": "model", "parts": parts})

        # Execute each requested tool call and feed results back in one
        # "user" turn (Gemini's convention for function results, not "tool"
        # or "function" — verified against Gemini's docs when this was written)
        response_parts = []
        for call in fn_calls:
            fn_name = call.get("name")
            fn_args = call.get("args", {})

            log.info(f"Round {round_num+1}: calling {fn_name}({fn_args})")

            if fn_name not in tools.TOOL_DISPATCH:
                result = {"success": False, "reason": f"Unknown tool: {fn_name}"}
            else:
                try:
                    result = tools.TOOL_DISPATCH[fn_name](**fn_args)
                except Exception as e:
                    log.warning(f"Tool {fn_name} raised an error: {e}")
                    result = {
                        "success": False,
                        "reason": "tool_error",
                        "message": (
                            f"{e} — no listing data was retrieved. Do not invent or "
                            "estimate a price; tell the user the search failed."
                        ),
                    }

            response_parts.append(
                {"functionResponse": {"name": fn_name, "response": result}}
            )

        contents.append({"role": "user", "parts": response_parts})

    # Safety net: if we hit max_tool_rounds without a final text answer,
    # ask the model one more time, restating the fallback rules explicitly.
    log.warning("Hit max_tool_rounds — forcing a final answer without further tool calls.")
    contents.append({
        "role": "user",
        "parts": [{"text": (
            "Please give your final answer now, without calling any more tools. "
            "Follow your system prompt's rules exactly: a rough fallback estimate "
            "is only allowed if the tool genuinely ran and found zero comparable "
            "listings (reason no_listings_found / no_listings_matching_coe_constraint), "
            "clearly labeled as not based on real data. If instead every tool call "
            "errored (reason tool_error) or returned queued_for_scraping, do NOT "
            "invent a price — say plainly what happened instead."
        )}],
    })
    response = _call_gemini(contents)
    candidates = response.get("candidates") or []
    if not candidates:
        raise GeminiAgentError(f"Gemini returned no candidates: {response}")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "2019 Honda Vezel"
    print(f"Query: {query}\n")
    answer = run(query)
    print(answer)
