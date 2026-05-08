"""
demo_agent.py — LLM-guided autonomous browser agent (CLI)
==========================================================
Replaces the fixed type→click→extract pipeline in demo_live_dom.py with a
self-deciding loop driven by Groq.

Usage example:
    python demo_agent.py \
        --url "https://www.naukri.com" \
        --goal "Search for data scientist jobs and show me the top 3 listings" \
        --max-steps 14

The agent figures out the correct action sequence by itself — no need to spell
out --type-target / --click. It uses the visible orange demo cursor for every
click and type action so you can watch it work.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

from app.powerfull_stealth_browser import PowerfulStealthBrowser
from app.cloudflare_handler import AdvancedCloudflareHandler
from app.dom import build_snapshot
from app.service import WebIntelService
from app.agent_loop import LLMBrowserAgent
from app.llm.groq_client import get_default_groq_client, GroqConfigurationError


# ── Re-use all cursor/animation helpers from demo_live_dom ──────────────────
# We import them directly rather than copy them so there's a single source of truth.
from demo_live_dom import (
    install_demo_cursor,
    set_demo_cursor,
    animate_cursor_move,
    highlight_selector,
    settle_page,
    cf_guard,
    preflight_url,
    _is_local_url,
)


# ─────────────────────────────────────────────────────────────────────────────
# Animated action callbacks (passed to the agent so it drives the demo cursor)
# ─────────────────────────────────────────────────────────────────────────────

def animated_click(page, selector: str) -> None:
    """Click `selector` with the visible orange demo cursor."""
    print(f"[animated_click] sel='{selector}'")
    install_demo_cursor(page)
    loc = page.locator(selector).first
    count = loc.count()
    visible = loc.is_visible() if count > 0 else False
    print(f"[animated_click] locator count={count}  visible={visible}")
    if count == 0:
        print(f"[animated_click] ❌ selector matched NOTHING on page — skipping click")
        return
    loc.scroll_into_view_if_needed(timeout=5000)
    highlight_selector(page, selector)

    box = loc.bounding_box()
    print(f"[animated_click] bounding_box={box}")
    if not box:
        print(f"[animated_click] ⚠ no bounding box — fallback direct click")
        loc.click(timeout=8000)
        settle_page(page)
        return

    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    print(f"[animated_click] clicking at ({cx:.0f}, {cy:.0f})")
    animate_cursor_move(page, cx, cy)
    set_demo_cursor(page, cx, cy, clicking=True)
    page.mouse.down()
    page.wait_for_timeout(120)
    page.mouse.up()
    page.wait_for_timeout(250)
    set_demo_cursor(page, cx, cy, clicking=False)
    settle_page(page)
    install_demo_cursor(page)
    print(f"[animated_click] ✅ done  url={page.url}")


def animated_type(page, selector: str, value: str) -> None:
    """Type `value` into `selector` with cursor animation."""
    print(f"[animated_type] sel='{selector}'  value='{value}'")
    install_demo_cursor(page)
    loc = page.locator(selector).first
    count = loc.count()
    visible = loc.is_visible() if count > 0 else False
    print(f"[animated_type] locator count={count}  visible={visible}")
    if count == 0:
        print(f"[animated_type] ❌ selector matched NOTHING — cannot type")
        return
    loc.scroll_into_view_if_needed(timeout=5000)
    highlight_selector(page, selector)

    box = loc.bounding_box()
    print(f"[animated_type] bounding_box={box}")
    if box:
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        print(f"[animated_type] clicking at ({cx:.0f}, {cy:.0f}) to focus")
        animate_cursor_move(page, cx, cy)
        set_demo_cursor(page, cx, cy, clicking=True)
        page.mouse.click(cx, cy)
        set_demo_cursor(page, cx, cy, clicking=False)
    else:
        print(f"[animated_type] ⚠ no bounding box — fallback direct click to focus")
        loc.click(timeout=5000)

    page.wait_for_timeout(200)
    select_all = "Meta+A" if platform.system() == "Darwin" else "Control+A"
    page.keyboard.press(select_all)
    page.keyboard.press("Backspace")
    print(f"[animated_type] typing '{value}' ({len(value)} chars) ...")
    page.keyboard.type(value, delay=48)
    settle_page(page, short=True)
    print(f"[animated_type] ✅ done")


# ─────────────────────────────────────────────────────────────────────────────
# LLM extraction with fallback keyword snippets
# ─────────────────────────────────────────────────────────────────────────────

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "show", "that",
    "the", "this", "to", "what", "where", "which", "with", "find", "get",
    "fetch", "give", "want", "need", "please", "about",
}


def keyword_snippets(query: str, text: str, limit: int = 4) -> list[str]:
    tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower())
              if t not in STOPWORDS and len(t) > 2]
    if not tokens:
        return []
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    scored = []
    for chunk in chunks:
        cleaned = " ".join(chunk.split()).strip()
        if len(cleaned) < 40:
            continue
        lowered = cleaned.lower()
        score = sum(3 for tok in tokens if tok in lowered)
        if score:
            scored.append((score, cleaned))
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    seen, out = set(), []
    for _, s in scored:
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_final_answer(service: WebIntelService, goal: str, agent_data: Dict) -> Dict:
    """Run one more LLM call to pull a crisp answer from the extracted page text."""
    page_text = agent_data.get("text", "")
    answer = ""
    if service.llm:
        try:
            answer = service.llm.extract_field(goal, page_text)
        except Exception:
            pass

    snippets = keyword_snippets(goal, page_text)
    return {
        "goal":  goal,
        "answer": answer or "\n\n".join(snippets) or page_text[:1200],
        "fallback_snippets": snippets,
        "url":   agent_data.get("url", ""),
        "title": agent_data.get("title", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LLM-guided autonomous browser agent. "
                    "Just give it a URL and a goal in plain English."
    )
    p.add_argument("--url",        required=True,  help="Starting URL")
    p.add_argument("--goal",       required=True,  help="Natural-language task description")
    p.add_argument("--max-steps",  type=int, default=14, help="Max LLM iterations (default 14)")
    p.add_argument("--target-count", type=int, default=3, help="Number of job descriptions to collect (default 3)")
    p.add_argument("--headless",   action="store_true", help="Run headless (no visible browser)")
    p.add_argument("--no-llm",     action="store_true", help="Disable Groq LLM extraction step")
    p.add_argument("--skip-preflight", action="store_true")
    p.add_argument("--keep-open",  action="store_true", help="Pause before closing the browser")
    p.add_argument("--screenshot", default="agent_screenshot.png")
    return p.parse_args()


def run() -> None:
    args = parse_args()

    # ── Preflight ──────────────────────────────────────────────────────────
    if not args.skip_preflight and not _is_local_url(args.url):
        try:
            warning = preflight_url(args.url)
            if warning:
                print(warning)
        except RuntimeError as e:
            print(f"Preflight warning (continuing anyway): {e}")

    # ── Groq client ────────────────────────────────────────────────────────
    try:
        groq = get_default_groq_client()
    except GroqConfigurationError as e:
        print(f"[Agent] No Groq keys found: {e}\nSet GROQ_API_KEYS in .env")
        sys.exit(1)

    # ── Browser + service ──────────────────────────────────────────────────
    session = PowerfulStealthBrowser(headless=args.headless, extra_stealth=True).start()
    service = WebIntelService(session, use_llm=not args.no_llm)

    result: Dict[str, Any] = {
        "url":  args.url,
        "goal": args.goal,
    }

    try:
        print(f"[Agent] Opening {args.url}")
        service.open_page(args.url)
        cf_guard(session.page, expected_url=args.url)
        install_demo_cursor(session.page)
        settle_page(session.page)

        # ── Run the agent loop ─────────────────────────────────────────────
        agent = LLMBrowserAgent(
            page=session.page,
            groq=groq,
            cf_guard_fn=lambda page, verbose=True: cf_guard(page, verbose=verbose),
            on_click=animated_click,
            on_type=animated_type,
            verbose=True,
            max_steps=args.max_steps,
        )

        agent_result = agent.run(url=args.url, goal=args.goal, target_count=args.target_count)
        result["agent"] = {
            "success":     agent_result["success"],
            "steps_taken": len(agent_result.get("steps", [])),
            "final_url":   agent_result.get("final_url"),
            "listing_url": agent_result.get("listing_url"),
            "error":       agent_result.get("error"),
        }

        # ── Collected JDs (Pattern C result) ──────────────────────────────
        collected = agent_result.get("collected_jobs", [])
        if collected:
            result["collected_jobs"] = collected
            print(f"\n[Agent] === RESULTS: {len(collected)} job descriptions ===")
            for i, job in enumerate(collected, 1):
                print(f"  [{i}] {job.get('title','(no title)')}")
                print(f"       {job.get('url','')}")
                print(f"       {job.get('jd_text','')[:200]}...\n")
        elif agent_result.get("data"):
            # Fallback: single-page extract (search didn't complete queue)
            result["extraction"] = extract_final_answer(
                service, args.goal, agent_result["data"]
            )
        else:
            result["extraction"] = {"goal": args.goal, "answer": "", "error": "no data"}

        # ── Screenshot ────────────────────────────────────────────────────
        if args.screenshot:
            session.screenshot(args.screenshot)
            result["screenshot"] = args.screenshot

        # ── Step trace (optional but useful for debugging) ─────────────────
        result["steps"] = [
            {k: v for k, v in h.items() if not k.startswith("_")}
            for h in agent_result.get("steps", [])
        ]

        print(json.dumps(result, ensure_ascii=False, indent=2))

        if args.keep_open:
            input("\nBrowser is open — press Enter to close... ")

    finally:
        session.close()


if __name__ == "__main__":
    run()
