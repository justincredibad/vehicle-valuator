"""
Agent loop — the LLM orchestrator.

This is the only place an LLM is involved at all, and it's a 100% local
model via Ollama (no API keys, no external tokens, no internet calls except
to your own localhost Ollama server and to sgcarmart for scraping).

The LLM's job is narrow and supervised:
  1. Parse the user's free-text query into make/model/year params
  2. Call search_and_estimate (tools.py)
  3. If confidence is low / sample size is small, decide whether to retry
     with a wider year range (bounded by config.MAX_WIDEN_RETRIES)
  4. Write a short, clear final answer using the numbers tools.py returned

The LLM does NOT compute prices itself for a search it has real data for —
it calls the tool and reports the tool's numbers back, keeping normal
valuations deterministic. The one deliberate exception: if the tool
genuinely finds zero comparable listings after a retry (not an error — a
real "nothing matched" result, e.g. a rare/exotic car), the model may give
a clearly-labeled, unverified rough estimate from general knowledge rather
than just refusing. See SYSTEM_PROMPT step 5 for the exact rule.
"""

import json
import logging
from datetime import date

import requests

import config
import tools

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("agent")


SYSTEM_PROMPT = """You are a local assistant that estimates used car prices \
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
   - If success is false, or confidence is "low" / sample_size is small \
(under {min_sample}), you may call search_and_estimate ONCE more with a \
wider year_min/year_max (widen by {widen_step} year(s) on each side). Keep \
the same min_coe_years_left if one was set — do not loosen a COE \
requirement the user gave you, unless you suspect you mis-converted an \
absolute expiry year the first time (see above) — in that case, recompute \
it correctly and retry once with the corrected number.
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
"tool_error" or similar) rather than genuinely finding zero matches — an \
error means the search didn't really happen, so say so plainly and suggest \
the user try again, rather than guessing.
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


class OllamaAgentError(Exception):
    pass


def _call_ollama(messages: list[dict]) -> dict:
    """Send a chat request to the local Ollama server with tool definitions."""
    url = f"{config.OLLAMA_HOST}/api/chat"
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "tools": tools.TOOL_SCHEMAS,
        "stream": False,
        # Keep the model resident in memory well past Ollama's 5-minute
        # default so gaps between queries (e.g. during a live demo) don't
        # pay a reload cost on the next request.
        "keep_alive": "30m",
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise OllamaAgentError(
            f"Couldn't reach Ollama at {config.OLLAMA_HOST}. "
            f"Is it running? Try `ollama serve` in another terminal. ({e})"
        )
    return resp.json()


def run(user_query: str, max_tool_rounds: int = 4) -> str:
    """
    Run the agent loop for one user query. Returns the final text answer.

    max_tool_rounds caps total LLM<->tool exchanges as a safety net, independent
    of the model's own self-discipline about config.MAX_WIDEN_RETRIES.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_query},
    ]

    for round_num in range(max_tool_rounds):
        response = _call_ollama(messages)
        message = response.get("message", {})
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            # No more tool calls — the model is giving its final answer
            final_text = message.get("content", "").strip()
            if not final_text:
                raise OllamaAgentError("Model returned an empty response.")
            return final_text

        # Append the assistant's tool-call message to history
        messages.append(message)

        # Execute each requested tool call and feed results back
        for call in tool_calls:
            fn_name = call["function"]["name"]
            fn_args = call["function"]["arguments"]
            if isinstance(fn_args, str):
                fn_args = json.loads(fn_args)

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

            messages.append({
                "role": "tool",
                "content": json.dumps(result),
            })

    # Safety net: if we hit max_tool_rounds without a final text answer,
    # ask the model one more time with tools disabled to force a wrap-up.
    log.warning("Hit max_tool_rounds — forcing a final answer without further tool calls.")
    final_payload = {
        "model": config.OLLAMA_MODEL,
        "messages": messages + [{
            "role": "user",
            "content": (
                "Please give your final answer now, without calling any more tools. "
                "Follow your system prompt's rules exactly: a rough fallback estimate "
                "is only allowed if the tool genuinely ran and found zero comparable "
                "listings (reason no_listings_found / no_listings_matching_coe_constraint), "
                "clearly labeled as not based on real data. If instead every tool call "
                "errored (reason tool_error) or you're unsure why it failed, do NOT "
                "invent a price — say plainly that the search failed and why."
            ),
        }],
        "stream": False,
        "keep_alive": "30m",
    }
    resp = requests.post(f"{config.OLLAMA_HOST}/api/chat", json=final_payload, timeout=120)
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "").strip()


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "2019 Honda Vezel"
    print(f"Query: {query}\n")
    answer = run(query)
    print(answer)
