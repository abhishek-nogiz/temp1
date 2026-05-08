"""
ADVANCED Cloudflare Handler v3.0 - LLM Self-Evolving Edition
For TinyFish2 / Playwright

Flow:
  solve()              - fast heuristic attempts (click turnstile, wait for redirect)
  LLMCloudflareResolver - if heuristics fail, asks Groq to read the page and suggest
                          the next action. Learns from each attempt and avoids repeating
                          failed strategies.
"""

from __future__ import annotations
import base64
import json
import random
import re
import time
from typing import List, Optional
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

class AdvancedCloudflareHandler:
    def __init__(self, page: Page, max_wait: int = 90, verbose: bool = True, human_like: bool = True, extra_stealth: bool = True):
        self.page = page
        self.max_wait = max_wait
        self.verbose = verbose
        self.human_like = human_like
        self.extra_stealth = extra_stealth
        self._challenges_solved = 0

    def _is_cloudflare_challenge(self, _debug: bool = False) -> bool:
        try:
            title = (self.page.title() or "").lower()
            url   = self.page.url
            if _debug:
                print(f"[CF-Debug] _is_cloudflare_challenge: url={url}  title='{title}'")

            if any(x in title for x in ["just a moment", "cloudflare", "attention required", "verify"]):
                if _debug:
                    print(f"[CF-Debug]   ↳ triggered by TITLE: '{title}'")
                return True

            body_text = self.page.locator("body").inner_text(timeout=2500) or ""
            body_lower = body_text.lower()[:4000]
            # NOTE: Do NOT add generic words like "cloudflare", "ray id", "turnstile" here.
            # They appear in footers/scripts on millions of normal pages and cause false positives.
            cf_phrases = ["verify you are human", "checking your browser", "one more step",
                          "ddos protection by cloudflare", "please wait while we verify",
                          "challenge running"]
            for phrase in cf_phrases:
                if phrase in body_lower:
                    if _debug:
                        idx = body_lower.index(phrase)
                        snippet = body_lower[max(0, idx-30):idx+60].replace("\n", " ")
                        print(f"[CF-Debug]   ↳ triggered by BODY phrase '{phrase}': ...{snippet}...")
                    return True

            selectors = ['div[class*="cf-"]', '#cf-challenge', 'form[action*="__cf_chl"]',
                         '[data-ray]', '.cf-turnstile', '#challenge-form', '#cf-please-wait',
                         'iframe[src*="challenges.cloudflare.com"]']
            for sel in selectors:
                if self.page.locator(sel).count() > 0:
                    if _debug:
                        print(f"[CF-Debug]   ↳ triggered by SELECTOR: '{sel}'")
                    return True

            try:
                cf_vars = self.page.evaluate("() => !!(window.__cf_chl_opt || window._cf_chl_opt || document.querySelector('script[src*=\"challenges.cloudflare.com\"]'))")
                if cf_vars:
                    if _debug:
                        print(f"[CF-Debug]   ↳ triggered by JS variable / script tag")
                    return True
            except:
                pass

            if _debug:
                print(f"[CF-Debug]   ↳ NO challenge detected")
            return False
        except Exception as e:
            if _debug:
                print(f"[CF-Debug]   ↳ exception in _is_cloudflare_challenge: {e}")
            return False

    def _human_mouse_move(self, x: int, y: int):
        if not self.human_like:
            return
        try:
            current = self.page.evaluate("() => ({x: window.mouseX || 100, y: window.mouseY || 100})")
            steps = random.randint(8, 18)
            for i in range(1, steps + 1):
                progress = i / steps
                nx = current["x"] + (x - current["x"]) * progress
                ny = current["y"] + (y - current["y"]) * progress
                self.page.mouse.move(nx, ny)
                time.sleep(random.uniform(0.01, 0.04))
            self.page.evaluate(f"window.mouseX = {x}; window.mouseY = {y}")
        except:
            pass

    def _random_delay(self, min_ms: int = 80, max_ms: int = 450):
        if self.human_like:
            time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

    def _human_activity(self):
        """Simulate real user behaviour during silent JS challenge.
        Multi-step mouse movement (v6.0) is more realistic than a single jump."""
        if not self.human_like:
            return
        try:
            for _ in range(random.randint(2, 5)):
                self.page.mouse.move(
                    random.randint(150, 1600),
                    random.randint(150, 800),
                    steps=random.randint(8, 15),  # gradual movement, not a jump
                )
                time.sleep(random.uniform(0.15, 0.40))
            self.page.evaluate("window.scrollBy(0, 180)")
            time.sleep(random.uniform(0.3, 0.7))
            self.page.evaluate("window.scrollBy(0, -80)")
            if random.random() < 0.3:
                self.page.keyboard.press("ArrowDown")
                time.sleep(0.1)
                self.page.keyboard.press("ArrowUp")
        except:
            pass

    def _is_hard_block(self) -> bool:
        """Detect CF 'Additional Verification Required' — a hard bot block.
        Unlike the normal JS challenge which eventually redirects, this one
        will NEVER resolve on its own. Bail immediately to save time."""
        try:
            body = (self.page.locator("body").inner_text(timeout=1500) or "").lower()[:2000]
            return (
                "additional verification required" in body
                or "your ray id for this request is" in body
            )
        except:
            return False

    def _click_turnstile_or_checkbox(self) -> bool:
        strategy_names = [
            'input[type="checkbox"]',
            'iframe[src*="challenges.cloudflare.com"] body',
            'button[name~=verify|human|continue]',
            'div[class*="cf-"] / #cf-challenge',
        ]
        strategies = [
            lambda: self.page.locator('input[type="checkbox"]').first,
            lambda: self.page.frame_locator('iframe[src*="challenges.cloudflare.com"]').locator('body').first,
            lambda: self.page.get_by_role("button", name=re.compile(r"verify|human|continue|not a robot", re.I)).first,
            lambda: self.page.locator('div[class*="cf-"], #cf-challenge').first,
        ]
        if self.verbose:
            print(f"[CF-Advanced] Trying {len(strategies)} click strategies...")
        for i, get_locator in enumerate(strategies, 1):
            try:
                loc = get_locator()
                count = loc.count()
                visible = loc.is_visible() if count > 0 else False
                if self.verbose:
                    print(f"[CF-Advanced]   strategy {i} ({strategy_names[i-1]}): count={count}, visible={visible}")
                if count > 0 and visible:
                    box = loc.bounding_box()
                    if self.verbose:
                        print(f"[CF-Advanced]   → bounding_box={box}")
                    if box:
                        cx = box["x"] + box["width"] / 2 + random.randint(-15, 15)
                        cy = box["y"] + box["height"] / 2 + random.randint(-10, 10)
                        self._human_mouse_move(int(cx), int(cy))
                        self._random_delay(120, 380)
                        self.page.mouse.click(cx, cy, delay=random.randint(40, 120))
                        if self.verbose:
                            print(f"[CF-Advanced]   ✓ Clicked strategy {i} at ({cx:.0f},{cy:.0f})")
                        return True
            except Exception as e:
                if self.verbose:
                    print(f"[CF-Advanced]   strategy {i} error: {e}")
                continue
        if self.verbose:
            print(f"[CF-Advanced]   ✗ No clickable CF element found")
        return False

    def _inject_stealth_cookies(self):
        """Only reinforce webdriver hiding — never inject fake CF cookies (they fail server-side validation)."""
        if not self.extra_stealth:
            return
        try:
            self.page.evaluate("""
                () => {
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    if (!window.chrome) window.chrome = { runtime: {} };
                }
            """)
        except:
            pass

    def _is_passing_js_challenge(self) -> bool:
        """Return True if CF already passed the JS leg and is just waiting to redirect."""
        try:
            title = (self.page.title() or "").lower()
            body  = (self.page.locator("body").inner_text(timeout=2000) or "").lower()[:2000]
            passing = (
                "verification successful" in body
                or ("waiting for" in body and "just a moment" in title)
            )
            if self.verbose and passing:
                print(f"[CF-Advanced] _is_passing_js_challenge=True  title='{title}'  body_snippet='{body[:120]}'")
            return passing
        except:
            return False

    def _wait_for_redirect_after_js_challenge(self, remaining: float) -> bool:
        """After CF passes the JS check, wait up to `remaining` seconds for the real page to load."""
        deadline = time.time() + min(remaining, 30)
        if self.verbose:
            print("[CF-Advanced] JS challenge passed — waiting for redirect...")
        while time.time() < deadline:
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=4000)
            except:
                pass
            if not self._is_cloudflare_challenge():
                return True
            time.sleep(1.0)
        return not self._is_cloudflare_challenge()

    def solve(self) -> bool:
        if not self._is_cloudflare_challenge():
            return False

        if self.verbose:
            print("[CF-Advanced] 🔥 Strong Cloudflare challenge detected. Starting advanced resolution...")

        # Hard block = CF already decided this is a bot. Nothing we can do — bail fast.
        if self._is_hard_block():
            if self.verbose:
                print("[CF-Advanced] 🚫 Hard block detected ('Additional Verification Required')."
                      " CF flagged the TLS fingerprint. Need a proxy or stealth binary to bypass.")
            return False

        start_time = time.time()
        attempt = 0

        while time.time() - start_time < self.max_wait:
            attempt += 1
            elapsed = time.time() - start_time
            if self.verbose:
                try:
                    title = self.page.title()
                    url   = self.page.url
                except Exception:
                    title, url = "?", "?"
                print(f"[CF-Advanced] Attempt {attempt}  elapsed={elapsed:.1f}s  url={url}  title='{title}'")

            # If CF already passed JS leg, just wait for the redirect — don't retry
            if self._is_passing_js_challenge():
                remaining = self.max_wait - (time.time() - start_time)
                if self._wait_for_redirect_after_js_challenge(remaining):
                    if self.verbose:
                        print(f"[CF-Advanced] ✅ JS challenge redirect completed!")
                    self._challenges_solved += 1
                    return True
                break  # gave it all remaining time, still blocked

            self._inject_stealth_cookies()
            self._click_turnstile_or_checkbox()

            # Wait condition: only check challenge-specific phrases, NOT the word 'cloudflare'
            # (Cloudflare's own pages always contain that word in footers/links)
            try:
                self.page.wait_for_function(
                    """() => {
                        const title = document.title.toLowerCase();
                        const body = document.body ? document.body.innerText.toLowerCase() : '';
                        return !title.includes('just a moment') &&
                               !title.includes('attention required') &&
                               !body.includes('verify you are human') &&
                               !body.includes('checking your browser') &&
                               !body.includes('challenge running') &&
                               !body.includes('please wait while we verify');
                    }""",
                    timeout=10000
                )
            except:
                pass

            still_challenged = self._is_cloudflare_challenge(_debug=self.verbose)
            if not still_challenged:
                if self.verbose:
                    print(f"[CF-Advanced] ✅ Challenge solved in {attempt} attempt(s)!")
                self._challenges_solved += 1
                self.page.wait_for_timeout(random.randint(800, 1600))
                return True

            if self.verbose:
                print(f"[CF-Advanced]   ↳ still challenged after attempt {attempt}")

            # Human-like activity every attempt — helps with behavioural fingerprinting
            self._human_activity()
            self._random_delay(600, 1400)

        if self.verbose:
            try:
                final_title = self.page.title()
                final_url   = self.page.url
                final_body  = (self.page.locator("body").inner_text(timeout=2000) or "")[:400]
            except Exception:
                final_title, final_url, final_body = "?", "?", "?"
            print(f"[CF-Advanced] ⚠️ Timeout after {self.max_wait}s — challenge not resolved")
            print(f"[CF-Advanced]   final url  : {final_url}")
            print(f"[CF-Advanced]   final title: {final_title}")
            print(f"[CF-Advanced]   final body  (first 400 chars):\n{final_body}")
        return False

    def get_stats(self) -> dict:
        return {"challenges_solved": self._challenges_solved}


# ─────────────────────────────────────────────────────────────────────────────
# LLM SELF-EVOLVING RESOLVER
# Asks Groq to read the live page DOM + screenshot and decide what to do next.
# Remembers every strategy it tried and avoids repeating failures.
# ─────────────────────────────────────────────────────────────────────────────

class LLMCloudflareResolver:
    """
    Self-evolving CF resolver.  Each attempt:
      1. Takes a screenshot + grabs page source + title + body text
      2. Sends everything to Groq with the history of what already failed
      3. Groq returns a JSON action plan
      4. We execute the action in Playwright
      5. Check result → feed back to next prompt as history

    Groq always returns ONE of these actions:
        {"action": "wait",    "seconds": <int>,   "reason": "..."}
        {"action": "click",   "selector": "...",  "reason": "..."}
        {"action": "js",      "code": "...",      "reason": "..."}
        {"action": "reload",                      "reason": "..."}
        {"action": "navigate","url": "...",       "reason": "..."}
        {"action": "give_up",                     "reason": "..."}
    """

    SYSTEM_PROMPT = """You are an expert browser automation engineer specialising in bypassing Cloudflare bot challenges.
You will be given:
  - current page title, URL, and body text (first 800 chars)
  - a base64 screenshot of the page
  - a history of actions already tried and whether they worked

Your job: decide the SINGLE best next action to clear the challenge and reach the real page.

Rules:
- Never repeat an action that is already in the failed_actions list.
- If you see "Just a moment..." with no visible UI, CF is running a silent JS check — your best option is "wait" (10-20s) or "reload".
- If you see a Turnstile checkbox or iframe, use "click" with the exact CSS selector.
- If you see a "Verify you are human" button, use "click".
- If all else fails after 3+ attempts, use "give_up".
- NEVER suggest injecting fake cf_clearance cookies — they don't pass server-side validation.

Respond ONLY with a single JSON object, no markdown, no explanation outside of the "reason" field.
Example: {"action": "wait", "seconds": 15, "reason": "Silent JS fingerprint check — give browser time to pass."}"""

    def __init__(self, page: Page, verbose: bool = True, max_attempts: int = 6):
        self.page = page
        self.verbose = verbose
        self.max_attempts = max_attempts
        self._history: List[dict] = []   # {"action": ..., "result": "success"|"failed"}
        self._groq = None
        self._load_groq()

    def _load_groq(self):
        try:
            from .llm.groq_client import get_default_groq_client
            self.groq = get_default_groq_client()
        except Exception as e:
            if self.verbose:
                print(f"[LLM-CF] Groq not available: {e}")
            self.groq = None

    def _screenshot_b64(self) -> str:
        try:
            data = self.page.screenshot(type="jpeg", quality=60, full_page=False)
            return base64.b64encode(data).decode()
        except Exception:
            return ""

    def _page_context(self) -> dict:
        try:
            title = self.page.title()
            url   = self.page.url
            body  = (self.page.locator("body").inner_text(timeout=2000) or "")[:800]
            # Grab all visible interactive elements for the LLM to reason about
            elements = self.page.evaluate("""() => {
                const sel = 'button, input, iframe, [role=button], a[href]';
                return Array.from(document.querySelectorAll(sel))
                    .filter(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    })
                    .slice(0, 20)
                    .map(e => ({
                        tag: e.tagName.toLowerCase(),
                        id: e.id || '',
                        cls: (e.className || '').toString().slice(0, 60),
                        text: (e.innerText || e.value || e.placeholder || '').slice(0, 60),
                        src: e.src || e.href || ''
                    }));
            }""")
        except Exception:
            title, url, body, elements = "?", "?", "", []
        return {"title": title, "url": url, "body": body, "elements": elements}

    def _build_prompt(self, ctx: dict) -> str:
        history_text = "\n".join(
            f"  attempt {i+1}: {h['action_desc']} -> {h['result']}"
            for i, h in enumerate(self._history)
        ) or "  (none yet)"
        failed = [h["action_desc"] for h in self._history if h["result"] == "failed"]

        return f"""PAGE STATE:
title: {ctx['title']}
url:   {ctx['url']}
body (first 800 chars):
{ctx['body']}

VISIBLE INTERACTIVE ELEMENTS:
{json.dumps(ctx['elements'], indent=2)}

ATTEMPT HISTORY:
{history_text}

FAILED ACTIONS (do not repeat):
{json.dumps(failed)}

Decide the single best next action to clear the Cloudflare challenge."""

    def _ask_llm(self, ctx: dict) -> Optional[dict]:
        if not self.groq:
            return None
        prompt = self._build_prompt(ctx)
        # Retry up to 2 times in case of transient Groq errors
        for llm_try in range(1, 3):
            try:
                resp = self.groq.chat(
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    model="llama-3.3-70b-versatile",
                    max_tokens=300,
                    temperature=0.2,
                )
                raw = resp.content.strip()
                # Strip markdown fences if Groq wrapped it anyway
                raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
                action = json.loads(raw)
                if self.verbose:
                    print(f"[LLM-CF] Groq suggested: {action}")
                return action
            except Exception as e:
                if self.verbose:
                    print(f"[LLM-CF] Groq error (try {llm_try}): {e}")
                if llm_try < 2:
                    time.sleep(2)
        return None

    def _execute(self, action: dict) -> bool:
        """Execute the LLM-suggested action. Returns True if page cleared CF after it."""
        act = action.get("action", "")
        try:
            if act == "wait":
                secs = min(int(action.get("seconds", 10)), 30)
                if self.verbose:
                    print(f"[LLM-CF] Waiting {secs}s...")
                time.sleep(secs)

            elif act == "click":
                sel = action.get("selector", "")
                if self.verbose:
                    print(f"[LLM-CF] Clicking '{sel}'...")
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=5000)
                    time.sleep(1.5)
                else:
                    if self.verbose:
                        print(f"[LLM-CF]   selector not found/visible")
                    return False

            elif act == "js":
                code = action.get("code", "")
                if self.verbose:
                    print(f"[LLM-CF] Running JS: {code[:80]}...")
                self.page.evaluate(code)
                time.sleep(2)

            elif act == "reload":
                if self.verbose:
                    print("[LLM-CF] Reloading page...")
                self.page.reload(wait_until="domcontentloaded", timeout=20000)
                time.sleep(3)

            elif act == "navigate":
                url = action.get("url", "")
                if self.verbose:
                    print(f"[LLM-CF] Navigating to {url}...")
                self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(2)

            elif act == "give_up":
                if self.verbose:
                    print(f"[LLM-CF] LLM gave up: {action.get('reason', '')}")
                return False

        except Exception as e:
            if self.verbose:
                print(f"[LLM-CF] Action execution error: {e}")
            return False

        # Wait a moment for the page to react, then check
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass

        handler = AdvancedCloudflareHandler(self.page, max_wait=1, verbose=False)
        return not handler._is_cloudflare_challenge()

    def resolve(self) -> bool:
        """Main entry point. Returns True if CF cleared."""
        if self.groq is None:
            if self.verbose:
                print("[LLM-CF] No Groq client — skipping LLM resolver")
            return False

        if self.verbose:
            print("[LLM-CF] 🤖 Starting LLM self-evolving Cloudflare resolver...")

        for attempt in range(1, self.max_attempts + 1):
            ctx = self._page_context()
            if self.verbose:
                print(f"[LLM-CF] Attempt {attempt}/{self.max_attempts}  title='{ctx['title']}'  url={ctx['url']}")

            action = self._ask_llm(ctx)
            if not action:
                continue

            action_desc = f"{action.get('action')} ({action.get('selector') or action.get('url') or action.get('seconds') or action.get('reason','')[:40]})"

            # Code-level dedup: don't let the LLM repeat actions its already failed at,
            # regardless of whether it follows the prompt instruction or not.
            action_key = f"{action.get('action')}:{action.get('selector','')}:{action.get('url','')}:{action.get('code','')}"
            already_failed = any(
                h.get("action_key") == action_key and h["result"] == "failed"
                for h in self._history
            )
            if already_failed:
                if self.verbose:
                    print(f"[LLM-CF] ↩ Skipping repeated failed action: {action_desc}")
                self._history.append({"action_desc": action_desc, "action_key": action_key, "result": "skipped"})
                continue

            success = self._execute(action)
            self._history.append({"action_desc": action_desc, "action_key": action_key, "result": "success" if success else "failed"})

            if success:
                if self.verbose:
                    print(f"[LLM-CF] ✅ Resolved after {attempt} LLM attempt(s)!")
                return True

            if action.get("action") == "give_up":
                break

        if self.verbose:
            print("[LLM-CF] ❌ LLM resolver exhausted all attempts")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Backwards compatible functions (used by service.py and demo_live_dom.py)
# ─────────────────────────────────────────────────────────────────────────────

def handle_cloudflare(page: Page, max_wait: int = 60, click_first: bool = True, verbose: bool = True) -> bool:
    handler = AdvancedCloudflareHandler(page, max_wait=max_wait, verbose=verbose)
    return handler.solve()


def auto_handle_cloudflare(page: Page):
    handler = AdvancedCloudflareHandler(page, max_wait=75, verbose=False)
    handler.solve()