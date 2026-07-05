"""
Flask web front-end for the sgcarmart valuer.

Thin wrapper around agent.run() — same pipeline the CLI (main.py) uses,
just reachable over HTTP. See README.md for local dev and Docker deployment.

Concurrency note: run this under gunicorn with exactly 1 worker / 1 thread
(see Dockerfile). agent.run() is CPU/IO-heavy (local LLM inference plus a
headless-Chromium scrape per uncached query) and doesn't benefit from more
workers on a small VPS — they'd just contend for the same resources. A
single worker + a "this can take a minute" UX is the right fit for a
personal-scale tool, not a limitation to engineer around.
"""

import logging

from flask import Flask, request, jsonify, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
import agent

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("webapp")

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=[])

db.init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/api/query", methods=["POST"])
@limiter.limit("6 per minute;60 per hour")
def api_query():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"error": "Please enter a vehicle to search for."}), 400
    if len(query) > 300:
        return jsonify({"error": "Query is too long."}), 400

    log.info(f"Query from {request.remote_addr}: {query!r}")

    try:
        answer = agent.run(query)
    except agent.OllamaAgentError as e:
        log.error(f"Ollama error: {e}")
        return jsonify({"error": str(e)}), 502
    except Exception:
        log.exception("Unexpected error in agent.run")
        return jsonify({"error": "Something went wrong processing that query."}), 500

    log.info(f"Answer: {answer!r}")
    return jsonify({"answer": answer})


if __name__ == "__main__":
    # Dev-only entrypoint (Flask's built-in server). Production runs via
    # gunicorn inside Docker — see Dockerfile.
    app.run(host="0.0.0.0", port=8000)
