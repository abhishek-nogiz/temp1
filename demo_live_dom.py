from __future__ import annotations

import argparse
import json
import platform
import re
import time
from typing import Any, Dict, List, Tuple
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


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "show", "that",
    "the", "this", "to", "what", "where", "which", "with", "find", "get",
    "fetch", "give", "want", "need", "please", "about",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visible DOM demo for TinyFish2. Opens a live page, moves a demo cursor, clicks/types, and extracts text for a natural-language query."
    )
    parser.add_argument("--url", required=True, help="Local or live URL to open")
    parser.add_argument("--query", required=True, help="Natural-language text to extract from the current page")
    parser.add_argument("--click", default="", help="Optional natural-language target to visibly click before extraction")
    parser.add_argument("--type-target", default="", help="Optional natural-language input target")
    parser.add_argument("--type-value", default="", help="Optional text to type into the target input")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless instead of visible mode")
    parser.add_argument("--no-llm", action="store_true", help="Disable Groq/Ollama assistance")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the initial URL check and let Playwright open the page directly")
    parser.add_argument("--manual-verify", action="store_true", help="Pause for manual verification if a challenge page is detected")
    parser.add_argument("--manual-verify-timeout", type=int, default=180, help="Seconds to wait for manual verification before failing")
    parser.add_argument("--keep-open", action="store_true", help="Keep the browser open until Enter is pressed")
    parser.add_argument("--screenshot", default="demo_live_dom.png", help="Final screenshot path")
    return parser.parse_args()


def _is_local_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _looks_like_bot_protection(response: httpx.Response) -> bool:
    server = (response.headers.get("server") or "").lower()
    if any(name in server for name in {"cloudflare", "akamai", "imperva", "sucuri", "datadome"}):
        return True
    return any(header in response.headers for header in {"cf-ray", "x-sucuri-id", "x-akamai-request-id"})


def preflight_url(url: str) -> str | None:
    try:
        response = httpx.get(url, follow_redirects=True, timeout=10.0)
    except Exception as exc:
        raise RuntimeError(
            f"Could not reach {url}. Start your local website first, then retry with its exact URL."
        ) from exc

    content_type = (response.headers.get("content-type") or "").lower()
    server = response.headers.get("server") or "unknown"

    if response.status_code >= 400:
        if "airtunes" in server.lower():
            raise RuntimeError(
                f"{url} is responding with {response.status_code} from macOS AirTunes, not from your website. "
                "Use the port where your app is actually running."
            )
        if not _is_local_url(url) and _looks_like_bot_protection(response):
            return (
                f"Preflight warning: {url} returned HTTP {response.status_code} from {server}, which looks like bot protection. "
                "Continuing with Playwright anyway."
            )
        raise RuntimeError(
            f"{url} returned HTTP {response.status_code} from server {server}. "
            "Use a page URL that loads successfully in your browser."
        )

    if "text/html" not in content_type:
        raise RuntimeError(
            f"{url} returned content-type {content_type or 'unknown'}, not an HTML page. "
            "Point the demo to a browser page URL, not an API endpoint."
        )

    return None


def install_demo_cursor(page) -> None:
    page.evaluate(
        """
        () => {
            const existing = document.getElementById('tinyfish-demo-cursor');
            if (existing) {
                return;
            }

            const cursor = document.createElement('div');
            cursor.id = 'tinyfish-demo-cursor';
            cursor.style.position = 'fixed';
            cursor.style.left = '0px';
            cursor.style.top = '0px';
            cursor.style.width = '18px';
            cursor.style.height = '18px';
            cursor.style.borderRadius = '999px';
            cursor.style.background = 'rgba(255, 87, 34, 0.92)';
            cursor.style.border = '2px solid white';
            cursor.style.boxShadow = '0 0 0 6px rgba(255, 87, 34, 0.18)';
            cursor.style.transform = 'translate(24px, 24px)';
            cursor.style.zIndex = '2147483647';
            cursor.style.pointerEvents = 'none';
            cursor.style.transition = 'transform 0.04s linear, box-shadow 0.15s ease, background 0.15s ease';
            document.documentElement.appendChild(cursor);

            window.__tinyfishDemoCursor = { x: 24, y: 24 };
        }
        """
    )


def set_demo_cursor(page, x: float, y: float, clicking: bool = False) -> None:
    page.evaluate(
        """
        ({ x, y, clicking }) => {
            const cursor = document.getElementById('tinyfish-demo-cursor');
            if (!cursor) {
                return;
            }
            cursor.style.transform = `translate(${x}px, ${y}px)`;
            cursor.style.background = clicking ? 'rgba(220, 38, 38, 0.96)' : 'rgba(255, 87, 34, 0.92)';
            cursor.style.boxShadow = clicking
                ? '0 0 0 12px rgba(220, 38, 38, 0.2)'
                : '0 0 0 6px rgba(255, 87, 34, 0.18)';
            window.__tinyfishDemoCursor = { x, y };
        }
        """,
        {"x": x, "y": y, "clicking": clicking},
    )


def get_demo_cursor_position(page) -> Tuple[float, float]:
    position = page.evaluate(
        """
        () => {
            const current = window.__tinyfishDemoCursor || { x: 24, y: 24 };
            return { x: current.x, y: current.y };
        }
        """
    )
    return float(position["x"]), float(position["y"])


def animate_cursor_move(page, x: float, y: float, steps: int = 18, delay_ms: int = 30) -> None:
    start_x, start_y = get_demo_cursor_position(page)
    for index in range(1, steps + 1):
        progress = index / steps
        next_x = start_x + (x - start_x) * progress
        next_y = start_y + (y - start_y) * progress
        set_demo_cursor(page, next_x, next_y)
        page.mouse.move(next_x, next_y)
        page.wait_for_timeout(delay_ms)


def highlight_selector(page, selector: str) -> None:
    page.evaluate(
        """
        (selector) => {
            const element = document.querySelector(selector);
            if (!element) {
                return;
            }
            element.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
            const previousOutline = element.style.outline;
            const previousOffset = element.style.outlineOffset;
            const previousTransition = element.style.transition;
            element.style.transition = 'outline 0.2s ease, outline-offset 0.2s ease';
            element.style.outline = '3px solid rgba(255, 87, 34, 0.95)';
            element.style.outlineOffset = '3px';
            setTimeout(() => {
                element.style.outline = previousOutline;
                element.style.outlineOffset = previousOffset;
                element.style.transition = previousTransition;
            }, 2500);
        }
        """,
        selector,
    )
    page.wait_for_timeout(250)


def cf_guard(page, max_wait: int = 75, verbose: bool = True, expected_url: str | None = None) -> None:
    """
    Check for and resolve Cloudflare challenges after every navigation.

    Pipeline:
      1. Fast heuristic solver (AdvancedCloudflareHandler)
      2. If heuristics fail → LLM self-evolving resolver (LLMCloudflareResolver)
      3. False-positive check: if CF 'solved' by redirecting to homepage/wrong page,
         and we know the expected URL, navigate back there.
    """
    if verbose:
        try:
            print(f"[CF-Guard] checking  url={page.url}  title='{page.title()}'")
        except Exception:
            print("[CF-Guard] checking (page not reachable yet)")

    pre_url = page.url

    # ── Step 1: fast heuristic solver ──────────────────────────────────────
    handler = AdvancedCloudflareHandler(page, max_wait=max_wait, verbose=verbose)
    solved  = handler.solve()

    # ── Step 2: LLM resolver if heuristics couldn't clear it ───────────────
    if not solved:
        # Re-check: maybe it just needed a moment
        handler2 = AdvancedCloudflareHandler(page, max_wait=1, verbose=False)
        if handler2._is_cloudflare_challenge():
            from app.cloudflare_handler import LLMCloudflareResolver
            llm_solver = LLMCloudflareResolver(page, verbose=verbose)
            solved = llm_solver.resolve()

    # ── Step 3: false-positive detection ───────────────────────────────────
    # CF sometimes "clears" by silently redirecting to the homepage.
    # If the URL changed away from where we wanted to go, navigate back.
    if solved and expected_url:
        current = page.url
        from urllib.parse import urlparse
        pre_path    = urlparse(pre_url).path.rstrip("/")
        current_path = urlparse(current).path.rstrip("/")
        expected_path = urlparse(expected_url).path.rstrip("/")

        # Drifted back to root — CF redirected us rather than passing us
        if current_path in ("", "/") and expected_path not in ("", "/"):
            if verbose:
                print(f"[CF-Guard] ⚠ CF redirected to homepage instead of {expected_url} — navigating back")
            try:
                page.goto(expected_url, wait_until="domcontentloaded", timeout=30000)
                settle_page(page)
                # One more heuristic pass on the re-navigation
                handler3 = AdvancedCloudflareHandler(page, max_wait=max_wait, verbose=verbose)
                handler3.solve()
            except Exception as e:
                if verbose:
                    print(f"[CF-Guard] re-navigate failed: {e}")

    if solved:
        if verbose:
            print(f"[CF-Guard] ✅ Challenge cleared — now at {page.url}")
        settle_page(page)
    else:
        if verbose:
            print("[CF-Guard] ℹ no CF challenge (or unresolvable).")


def settle_page(page, short: bool = False) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(500 if short else 1200)


def challenge_page_detected(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""

    try:
        body_text = (page.locator("body").inner_text(timeout=3000) or "")[:4000].lower()
    except Exception:
        body_text = ""

    markers = [
        "verify you are human",
        "additional verification required",
        "checking if the site connection is secure",
        "cloudflare",
        "captcha",
        "ray id",
    ]
    combined = f"{title}\n{body_text}"
    return any(marker in combined for marker in markers)


def wait_for_manual_verification(page, timeout_seconds: int) -> None:
    if not challenge_page_detected(page):
        return

    print(
        "Manual verification required. Complete the challenge in the visible browser window. "
        "The script will continue automatically after the challenge page disappears."
    )

    deadline = time.time() + max(10, timeout_seconds)
    while time.time() < deadline:
        if not challenge_page_detected(page):
            settle_page(page)
            return
        page.wait_for_timeout(1000)

    raise RuntimeError(
        "Timed out waiting for manual verification to complete. Solve the challenge in the browser, "
        "or rerun with a longer --manual-verify-timeout."
    )


def visible_click(
    service: WebIntelService,
    prompt: str,
    exclude_selector: str | None = None,
) -> Dict[str, Any]:
    element = service.find_element(prompt)
    if not element:
        raise ValueError(f"No element found for prompt: {prompt}")

    page = service.session.page

    # If the best match is the element we just typed into, the submit button was
    # not found in the snapshot (icon-only, missing text, etc.).  Pressing Enter
    # on the focused input is the natural way to submit any search form.
    if exclude_selector and element.selector == exclude_selector:
        print(f"[click→Enter] Best match is the typed element — pressing Enter to submit form")
        page.keyboard.press("Enter")
        settle_page(page)
        return {
            "clicked": "Enter (form submit)",
            "selector": exclude_selector,
            "url": page.url,
            "score": element.score,
            "llm_confidence": None,
        }

    if not element.selector:
        return service.click_element(prompt)

    install_demo_cursor(page)
    locator = page.locator(element.selector).first
    locator.scroll_into_view_if_needed(timeout=5000)
    highlight_selector(page, element.selector)

    box = locator.bounding_box()
    if not box:
        raise RuntimeError(f"Could not resolve bounding box for selector: {element.selector}")

    center_x = box["x"] + (box["width"] / 2)
    center_y = box["y"] + (box["height"] / 2)
    animate_cursor_move(page, center_x, center_y)
    set_demo_cursor(page, center_x, center_y, clicking=True)
    page.mouse.down()
    page.wait_for_timeout(120)
    page.mouse.up()
    page.wait_for_timeout(250)
    set_demo_cursor(page, center_x, center_y, clicking=False)

    settle_page(page)
    install_demo_cursor(page)
    return {
        "clicked": element.text,
        "selector": element.selector,
        "url": page.url,
        "score": element.score,
        "llm_confidence": element.llm_confidence,
    }


def visible_type(service: WebIntelService, prompt: str, value: str) -> Dict[str, Any]:
    if not value:
        raise ValueError("type value is required when using --type-target")

    page = service.session.page

    # ── Debug: page state before element search ───────────────────────────
    try:
        _dbg_url   = page.url
        _dbg_title = page.title()
    except Exception as _e:
        _dbg_url, _dbg_title = "?", f"(error: {_e})"
    print(f"[type-debug] page  url='{_dbg_url}'  title='{_dbg_title}'")

    # ── Debug: what inputs are on the page right now ─────────────────────
    try:
        from app.dom import build_snapshot
        _snap = build_snapshot(page)
        print(f"[type-debug] snapshot: {len(_snap.inputs)} inputs, {len(_snap.buttons)} buttons, {len(_snap.links)} links")
        for _inp in _snap.inputs[:8]:
            print(f"[type-debug]   input  tag={_inp.tag}  text='{_inp.text[:60]}'  sel='{_inp.selector}'")
        if not _snap.inputs:
            print("[type-debug]   ⚠ NO inputs found in snapshot — page may be blank, CF-blocked, or still loading")
    except Exception as _e:
        print(f"[type-debug]   snapshot error: {_e}")

    # ── Find the element — INPUTS ONLY first ─────────────────────────────
    # visible_type must never match a link or button; search inputs-only and
    # only widen the search if no input scores well enough.
    print(f"[type-debug] searching inputs only for '{prompt}' ...")
    from app.prompts import find_element as heuristic_find_element
    try:
        _snap2 = build_snapshot(page)
        element = heuristic_find_element(prompt, _snap2.inputs)
        if element:
            print(f"[type-debug] inputs-search hit  tag={element.tag}  text='{element.text[:60]}'  sel='{element.selector}'  score={element.score}")
        else:
            print(f"[type-debug] inputs-search returned None")
    except Exception as _fe:
        print(f"[type-debug] inputs-search error: {_fe}")
        element = None

    # If inputs-only search failed or scored very low, widen to all elements
    # but reject any non-input result (a link/button is never right for typing).
    if not element or (element.score is not None and element.score < 10):
        print(f"[type-debug] score too low or no input — trying full find_element ...")
        _wide = service.find_element(prompt)
        if _wide and _wide.tag in ("input", "textarea", "select", "[contenteditable]"):
            element = _wide
            print(f"[type-debug] wide-search used  tag={element.tag}  text='{element.text[:60]}'  sel='{element.selector}'  score={element.score}")
        elif _wide:
            print(f"[type-debug] ⚠ wide-search returned a non-input (tag={_wide.tag}, text='{_wide.text[:40]}') — ignoring it, sticking with inputs-only result")

    if not element:
        print(f"[type-debug] ❌ no input found for prompt='{prompt}'")
        raise ValueError(f"No input found for prompt: {prompt}")

    if element.tag not in ("input", "textarea", "select"):
        print(f"[type-debug] ⚠ best match is tag={element.tag} (not an input!) — text='{element.text[:60]}'")
        # Try to pick the most text-like input from the snapshot as a safe fallback
        _inputs = _snap2.inputs if '_snap2' in dir() else build_snapshot(page).inputs
        if _inputs:
            element = _inputs[0]
            print(f"[type-debug] → falling back to first input: text='{element.text[:60]}'  sel='{element.selector}'")
        else:
            raise ValueError(f"No input elements on page — cannot type for prompt: {prompt}")

    print(f"[type-debug] final element  tag={element.tag}  text='{element.text[:60]}'  sel='{element.selector}'  score={element.score}")

    if not element.selector:
        print("[type-debug] no selector — falling back to service.type_text()")
        return service.type_text(prompt, value)

    # ── Locate + highlight ───────────────────────────────────────────────
    install_demo_cursor(page)
    locator = page.locator(element.selector).first
    print(f"[type-debug] scroll_into_view  sel='{element.selector}' ...")
    locator.scroll_into_view_if_needed(timeout=5000)
    highlight_selector(page, element.selector)

    print("[type-debug] bounding_box ...")
    box = locator.bounding_box()
    if not box:
        raise RuntimeError(f"Could not resolve bounding box for selector: {element.selector}")

    print(f"[type-debug] box={box}")
    center_x = box["x"] + (box["width"] / 2)
    center_y = box["y"] + (box["height"] / 2)

    print(f"[type-debug] move cursor → ({center_x:.0f}, {center_y:.0f})")
    animate_cursor_move(page, center_x, center_y)
    set_demo_cursor(page, center_x, center_y, clicking=True)
    page.mouse.click(center_x, center_y)
    set_demo_cursor(page, center_x, center_y, clicking=False)
    page.wait_for_timeout(200)

    select_all_key = "Meta+A" if platform.system() == "Darwin" else "Control+A"
    print("[type-debug] clearing field ...")
    page.keyboard.press(select_all_key)
    page.keyboard.press("Backspace")

    print(f"[type-debug] typing '{value}' ({len(value)} chars) ...")
    page.keyboard.type(value, delay=50)

    settle_page(page, short=True)
    print(f"[type-debug] ✅ done typing")
    return {
        "typed": value,
        "into": element.text,
        "selector": element.selector,
    }


def keyword_snippets(query: str, text: str, limit: int = 3) -> List[str]:
    tokens = [token for token in re.findall(r"[a-z0-9]+", query.lower()) if token not in STOPWORDS and len(token) > 2]
    if not tokens:
        return []

    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    scored: List[Tuple[int, str]] = []
    for chunk in chunks:
        cleaned = " ".join(chunk.split()).strip()
        if len(cleaned) < 40:
            continue
        lowered = cleaned.lower()
        score = sum(3 for token in tokens if token in lowered)
        if score:
            scored.append((score, cleaned))

    scored.sort(key=lambda item: (-item[0], len(item[1])))
    snippets: List[str] = []
    for _, snippet in scored:
        if snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= limit:
            break
    return snippets


def extract_answer(service: WebIntelService, query: str) -> Dict[str, Any]:
    snapshot = build_snapshot(service.session.page)
    answer = ""
    if service.llm:
        answer = service.llm.extract_field(query, snapshot.text)

    snippets = keyword_snippets(query, snapshot.text)
    fallback_text = "\n\n".join(snippets)

    return {
        "query": query,
        "answer": answer or fallback_text or snapshot.text[:1200],
        "fallback_snippets": snippets,
        "url": snapshot.url,
        "title": snapshot.title,
    }


def run() -> None:
    args = parse_args()

    session = PowerfulStealthBrowser(headless=args.headless, extra_stealth=True).start()
    service = WebIntelService(session, use_llm=not args.no_llm)
    result: Dict[str, Any] = {
        "url": args.url,
        "query": args.query,
        "llm_used": service.use_llm,
    }

    try:
        print(f"[1/4] Opening {args.url}")
        if not args.skip_preflight:
            warning = preflight_url(args.url)
            if warning:
                print(warning)
        service.open_page(args.url)

        # Initial CF guard on the landing page
        cf_guard(session.page, expected_url=args.url)
        if args.manual_verify:
            wait_for_manual_verification(session.page, args.manual_verify_timeout)

        install_demo_cursor(session.page)
        settle_page(session.page)

        last_typed_selector: str | None = None

        if args.type_target:
            print(f"[2/4] Typing into: {args.type_target}")
            result["type"] = visible_type(service, args.type_target, args.type_value)
            last_typed_selector = result["type"].get("selector")
            # After typing, page URL not yet changed — no CF guard needed here

        if args.click:
            step_label = "[3/4]" if args.type_target else "[2/4]"
            print(f"{step_label} Clicking: {args.click}")
            result["click"] = visible_click(service, args.click, exclude_selector=last_typed_selector)
            # After click/Enter the browser navigates to a new page — this is where CF hits
            # expected_url is the destination URL from the click result (post-navigation)
            post_click_url = result["click"].get("url") or session.page.url
            cf_guard(session.page, expected_url=post_click_url)
            install_demo_cursor(session.page)

        print("[4/4] Extracting answer from the live DOM")
        cf_guard(session.page, verbose=False)  # silent final guard before extraction
        result["extraction"] = extract_answer(service, args.query)

        if args.screenshot:
            session.screenshot(args.screenshot)
            result["screenshot"] = args.screenshot

        print(json.dumps(result, ensure_ascii=False, indent=2))

        if args.keep_open:
            input("Browser is still open. Press Enter to close it... ")
    finally:
        session.close()


if __name__ == "__main__":
    run()