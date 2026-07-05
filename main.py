"""
CLI entry point.

Usage:
    python main.py "2019 Honda Vezel"
    python main.py "Toyota Corolla Altis 2017"
    python main.py            # interactive mode, asks for input
"""

import sys

import db
import agent


def main():
    db.init_db()

    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        _run_once(query)
    else:
        print("sgcarmart local price estimator (Ctrl+C to quit)")
        while True:
            try:
                query = input("\nVehicle (e.g. '2019 Honda Vezel'): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nBye.")
                break
            if not query:
                continue
            _run_once(query)


def _run_once(query: str):
    print(f"\n--- {query} ---")
    try:
        answer = agent.run(query)
        print(answer)
    except agent.OllamaAgentError as e:
        print(f"[Error] {e}")


if __name__ == "__main__":
    main()
