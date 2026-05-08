from __future__ import annotations

import argparse
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from .agent import BrowserAgent
from .browser import BrowserSession
from .service import WebIntelService


def run_cli() -> None:
    parser = argparse.ArgumentParser(description="Open TinyFish / AgentQL-like browser agent")
    parser.add_argument("--url", default="https://example.com", help="URL to open")
    parser.add_argument("--task", default="extract the main page title and visible links", help="Browser task")
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum browser actions")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless")
    parser.add_argument("--no-llm", action="store_true", help="Disable Groq/Ollama")
    parser.add_argument("--schema", default="", help="Optional JSON schema/object describing fields to extract")
    args = parser.parse_args()

    schema = None
    if args.schema:
        schema = json.loads(args.schema)

    session = BrowserSession(headless=args.headless).start()
    service = WebIntelService(session, use_llm=not args.no_llm)
    agent = BrowserAgent(service)
    try:
        result = agent.run(
            url=args.url,
            task=args.task,
            max_steps=args.max_steps,
            schema=schema,
            screenshot_on_finish=True,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        session.close()


if __name__ == "__main__":
    run_cli()
