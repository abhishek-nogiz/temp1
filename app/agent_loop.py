"""
LLM-Guided Autonomous Browser Agent
=====================================
Replaces the fixed type→click→extract pipeline with a self-deciding loop:

  1. Take a DOM snapshot of the current page.
  2. Build a compact element list (INPUT[i], BUTTON[i], LINK[i]).
  3. Ask Groq: "What is the single best next action toward the goal?"
  4. Execute that action via Playwright (resolving element by index, not heuristics).
  5. Run cf_guard after every navigation/click.
  6. Repeat until action=done|extract|fail or max_steps reached.

Key design decisions (based on review of the earlier architecture proposal):
  - Elements are referenced by INDEX in the snapshot – the LLM picks INPUT[3],
    not a raw selector or free-text – so the executor does a direct lookup,
    bypassing the scoring heuristic that caused the Naukri bug.
  - visited_url_count prevents infinite loops on the same page.
  - CF guard is called inside _execute_action after every click/navigate.
  - The visible cursor / highlight functions are passed in as callables so this
    module stays UI-agnostic (works headless too).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .dom import build_snapshot
from .schemas import PageItem, PageSnapshot
from .llm.groq_client import AllGroqKeysRateLimited


# ─────────────────────────────────────────────────────────────────────────────
# Agent state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    goal: str
    start_url: str
    current_url: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    max_steps: int = 14
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    _url_step_count: Dict[str, int] = field(default_factory=dict)

    # ── Pattern C: URL Queue fields ──────────────────────────────────────────
    # Phase is code-controlled ONLY — LLM never changes this.
    phase: str = "search"               # "search" | "detail" | "done"
    listing_url: str = ""               # saved when results page is detected
    url_queue: List[str] = field(default_factory=list)   # detail URLs to visit
    visited_urls: List[str] = field(default_factory=list)
    collected_jobs: List[Dict[str, Any]] = field(default_factory=list)
    target_count: int = 3               # how many JDs to collect

    def add_step(self, action: str, target: str = "", value: str = "",
                 result: str = "", url_before: str = "", url_after: str = ""):
        self.history.append({
            "step": len(self.history) + 1,
            "action": action,
            "target": target,
            "value": value,
            "result": result,
            "url_before": url_before,
            "url_after": url_after,
        })

    def record_url(self, url: str) -> int:
        """Returns how many times this URL has been seen so far (after recording)."""
        key = url.split("?")[0]
        self._url_step_count[key] = self._url_step_count.get(key, 0) + 1
        return self._url_step_count[key]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_SEARCH = """\
You are a browser automation agent. Your job is to reach the job SEARCH RESULTS
page for the given query. Stop as soon as a list of job postings is visible.

Rules:
1. Choose exactly ONE action per turn.
2. Reference elements by INDEX (INPUT[0], BUTTON[2], LINK[3]).
3. After typing into a search/keyword input, your VERY NEXT action MUST be
   {"action": "key", "key": "Enter"} — never click a nav link instead.
4. If no Search/Submit BUTTON is visible after typing, always use key Enter.
5. Once you can see a list of job cards/results, return {"action": "extract"}.
6. Never repeat an action that already failed.

Return ONLY a single JSON object. Valid actions:
  {"action": "click",    "target": "BUTTON[2]",  "reason": "..."}
  {"action": "type",     "target": "INPUT[0]",   "value": "...", "reason": "..."}
  {"action": "scroll",   "direction": "down",    "reason": "..."}
  {"action": "key",      "key": "Enter",         "reason": "..."}
  {"action": "extract",                           "reason": "results page reached"}
  {"action": "fail",                              "reason": "..."}
"""

SYSTEM_PROMPT_DETAIL = """\
You are a browser automation agent. Your job is to extract the full job
description from the current job detail page.

Rules:
1. Choose exactly ONE action per turn.
2. If the full job description text is already visible, return {"action": "extract"}.
3. If content is hidden behind a "Show more" / "View full description" button, click it first.
4. If the page shows a login wall or paywall, return {"action": "fail"}.
5. Scrolling to find more content is allowed.
6. Never navigate away from this page.

Return ONLY a single JSON object. Valid actions:
  {"action": "click",    "target": "BUTTON[2]",  "reason": "..."}
  {"action": "scroll",   "direction": "down",    "reason": "..."}
  {"action": "extract",                           "reason": "..."}
  {"action": "fail",                              "reason": "..."}
"""


def _build_element_list(snapshot: PageSnapshot) -> tuple[
    str, List[PageItem], List[PageItem], List[PageItem]
]:
    """Return (compact_text, inputs, buttons, links) — lists ordered by snapshot index."""
    inputs  = snapshot.inputs[:15]
    buttons = snapshot.buttons[:20]
    links   = snapshot.links[:25]

    lines: List[str] = []
    for i, el in enumerate(inputs):
        lines.append(f"INPUT[{i}]   {el.text[:90]}")
    for i, el in enumerate(buttons):
        lines.append(f"BUTTON[{i}]  {el.text[:90]}")
    for i, el in enumerate(links):
        href = (el.href or "")[:55]
        lines.append(f"LINK[{i}]    {el.text[:70]}  →  {href}")

    return "\n".join(lines), inputs, buttons, links


def build_prompt(state: AgentState, snapshot: PageSnapshot) -> str:
    element_text, _, _, _ = _build_element_list(snapshot)

    history_lines = "\n".join(
        f"  step {h['step']}: {h['action']} {h.get('target','')} "
        f"{'→ typed: ' + repr(h['value']) if h.get('value') else ''}"
        f"  result={h.get('result','?')[:80]}"
        for h in state.history[-8:]
    ) or "  (none yet)"

    failed_actions = [
        f"{h['action']} {h.get('target','')}".strip()
        for h in state.history if h.get("result", "").startswith("failed")
    ]

    phase_context = ""
    if state.phase == "detail":
        phase_context = (
            f"\nCOLLECTED SO FAR: {len(state.collected_jobs)} / {state.target_count} job descriptions\n"
            f"REMAINING IN QUEUE: {len(state.url_queue)} URLs\n"
        )

    return f"""GOAL: {state.goal}
PHASE: {state.phase.upper()}
{phase_context}
CURRENT PAGE:
  title : {snapshot.title}
  url   : {snapshot.url}

VISIBLE ELEMENTS:
{element_text}

ACTION HISTORY (last 8 steps):
{history_lines}

FAILED ACTIONS (do NOT repeat these):
{json.dumps(failed_actions) if failed_actions else "[]"}

What is the single best next action?"""


# ─────────────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────────────

class LLMBrowserAgent:
    """
    Parameters
    ----------
    page        : Playwright sync Page object
    groq        : GroqRouterClient from app.llm.groq_client
    cf_guard_fn : callable(page) – your existing cf_guard() function
    on_click    : optional callable(page, selector) – runs visible_click animation
    on_type     : optional callable(page, selector, value) – runs visible_type animation
    verbose     : print step-by-step progress
    max_steps   : hard cap on LLM iterations
    """

    def __init__(
        self,
        page,
        groq,
        cf_guard_fn: Optional[Callable] = None,
        on_click: Optional[Callable] = None,
        on_type: Optional[Callable] = None,
        verbose: bool = True,
        max_steps: int = 14,
    ):
        self.page = page
        self.groq = groq
        self.cf_guard_fn = cf_guard_fn
        self.on_click = on_click   # callable(page, selector) for animated click
        self.on_type  = on_type    # callable(page, selector, value) for animated type
        self.verbose  = verbose
        self.max_steps = max_steps

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _ask(self, state: AgentState, snapshot: PageSnapshot) -> Optional[Dict]:
        prompt = build_prompt(state, snapshot)
        system = SYSTEM_PROMPT_DETAIL if state.phase == "detail" else SYSTEM_PROMPT_SEARCH
        try:
            resp = self.groq.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                model="llama-3.3-70b-versatile",
                max_tokens=250,
                temperature=0.1,
            )
            raw = resp.content.strip()
            if not raw:
                # Groq returned 200 but empty content — log the full response for diagnosis
                print(f"[Agent] ⚠ Groq returned EMPTY content.")
                print(f"[Agent]   model={resp.model}  key={resp.key_name}")
                print(f"[Agent]   usage={resp.usage}")
                finish = ""
                try:
                    finish = resp.raw["choices"][0].get("finish_reason", "?")
                except Exception:
                    pass
                print(f"[Agent]   finish_reason={finish}")
                return None
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
            action = json.loads(raw)
            if self.verbose:
                print(f"[Agent] LLM → {action}")
            return action
        except AllGroqKeysRateLimited:
            # Re-raise so the caller (run / _extract_jd) can exit immediately
            # instead of burning all remaining steps doing nothing.
            raise
        except Exception as e:
            print(f"[Agent] LLM call failed: {e}")
            return None

    # ── Element resolver: INDEX string → PageItem ─────────────────────────────

    @staticmethod
    def _is_results_page(current_url: str, start_url: str) -> bool:
        """
        Deterministic check — no LLM.
        Returns True if the current URL looks like a job results/listing page
        and is different from where we started.
        """
        from urllib.parse import urlparse, parse_qs
        if current_url.rstrip("/") == start_url.rstrip("/"):
            return False
        path_qs = (urlparse(current_url).path + "?" + urlparse(current_url).query).lower()
        RESULTS_SIGNALS = (
            "-jobs", "/jobs", "/job-", "/search", "?k=", "?q=",
            "?query=", "?keyword", "/find", "/results", "jobs?",
        )
        return any(sig in path_qs for sig in RESULTS_SIGNALS)

    @staticmethod
    def _resolve_element(
        target: str,
        inputs: List[PageItem],
        buttons: List[PageItem],
        links: List[PageItem],
    ) -> Optional[PageItem]:
        """
        Parse "INPUT[3]" / "BUTTON[0]" / "LINK[7]" → the PageItem.
        Falls back to text-search across all three lists if indexing fails.
        """
        print(f"[resolve] target='{target}'  pools: {len(inputs)} inputs, {len(buttons)} buttons, {len(links)} links")
        m = re.match(r"^(INPUT|BUTTON|LINK)\s*\[(\d+)\]", target.strip().upper())
        if m:
            kind, idx = m.group(1), int(m.group(2))
            pool = {"INPUT": inputs, "BUTTON": buttons, "LINK": links}[kind]
            if idx < len(pool):
                el = pool[idx]
                print(f"[resolve] ✓ index hit  → tag={el.tag}  text='{el.text[:60]}'  sel='{el.selector}'")
                return el
            print(f"[resolve] ⚠ {kind}[{idx}] out of range (pool size={len(pool)}) — falling back to text search")
        else:
            print(f"[resolve] no index pattern — text search for '{target}'")
        # Text fallback: search all pools
        t_lower = target.lower()
        for el in inputs + buttons + links:
            if t_lower in (el.text or "").lower():
                print(f"[resolve] ✓ text fallback hit → tag={el.tag}  text='{el.text[:60]}'  sel='{el.selector}'")
                return el
        print(f"[resolve] ❌ could not resolve '{target}'")
        return None

    # ── Individual action executors ──────────────────────────────────────────

    def _do_click(self, element: PageItem, url_before: str) -> str:
        sel = element.selector
        print(f"[do_click] tag={element.tag}  text='{element.text[:60]}'  sel='{sel}'")
        if not sel:
            return "failed: element has no selector"
        try:
            if self.on_click:
                print(f"[do_click] calling animated_click(sel='{sel}')")
                self.on_click(self.page, sel)
            else:
                loc = self.page.locator(sel).first
                loc.scroll_into_view_if_needed(timeout=5000)
                print(f"[do_click] direct Playwright click")
                loc.click(timeout=8000)
                time.sleep(0.5)

            if self.cf_guard_fn:
                self.cf_guard_fn(self.page, verbose=False)

            url_after = self.page.url
            print(f"[do_click] url_before={url_before}  url_after={url_after}")
            return f"ok — navigated to {url_after}" if url_after != url_before else "ok — clicked (no navigation)"
        except Exception as e:
            print(f"[do_click] ❌ exception: {e}")
            return f"failed: {e}"

    def _do_type(self, element: PageItem, value: str) -> str:
        sel = element.selector
        print(f"[do_type] tag={element.tag}  text='{element.text[:60]}'  sel='{sel}'  value='{value}'")
        if not sel:
            return "failed: element has no selector"
        try:
            if self.on_type:
                print(f"[do_type] calling animated_type(sel='{sel}', value='{value}')")
                self.on_type(self.page, sel, value)
            else:
                loc = self.page.locator(sel).first
                loc.scroll_into_view_if_needed(timeout=5000)
                loc.click(timeout=5000)
                self.page.keyboard.press("Control+A")
                self.page.keyboard.press("Backspace")
                self.page.keyboard.type(value, delay=45)
                time.sleep(0.3)
            print(f"[do_type] ✅ typed successfully")
            return f"ok — typed {repr(value)}"
        except Exception as e:
            print(f"[do_type] ❌ exception: {e}")
            return f"failed: {e}"

    def _do_navigate(self, url: str) -> str:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.0)
            if self.cf_guard_fn:
                self.cf_guard_fn(self.page, verbose=False)
            return f"ok — at {self.page.url}"
        except Exception as e:
            return f"failed: {e}"

    def _do_scroll(self, direction: str) -> str:
        try:
            amount = 700 if direction == "down" else -700
            self.page.evaluate(f"window.scrollBy(0, {amount})")
            time.sleep(0.4)
            return "ok — scrolled"
        except Exception as e:
            return f"failed: {e}"

    def _do_key(self, key: str) -> str:
        try:
            self.page.keyboard.press(key)
            time.sleep(0.8)
            if self.cf_guard_fn:
                self.cf_guard_fn(self.page, verbose=False)
            return f"ok — pressed {key}"
        except Exception as e:
            return f"failed: {e}"

    def _do_extract(self) -> Dict[str, Any]:
        """Pull text from the current page and return a structured result."""
        snapshot = build_snapshot(self.page)
        return {
            "url":   snapshot.url,
            "title": snapshot.title,
            "text":  snapshot.text[:6000],
        }

    def _extract_result_urls(self, n: int = 3, start_url: str = "") -> List[str]:
        """
        Deterministically pull the first `n` job-card links from the listing page.
        Strategy:
          1. Wait (via Playwright) for job-card anchors to appear in the DOM.
          2. Scroll once to trigger lazy-load.
          3. Use JS to collect from content areas, exclude nav/header/footer.
          4. Filter by URL signals that indicate a job detail page.
          5. Falls back to any content-area same-domain link if signals don't match.
        """
        from urllib.parse import urlparse
        base_domain = urlparse(self.page.url).netloc
        listing_base = self.page.url.split("?")[0]
        root_url = f"https://{base_domain}/"

        # Wait for job-card links to appear (covers lazy-rendering)
        JOB_CARD_SELECTORS = [
            'a[href*="job-listings"]',
            '[class*="jobTuple"] a',
            '[class*="job-card"] a',
            '[class*="srp"] a',
            'article a',
        ]
        for sel in JOB_CARD_SELECTORS:
            try:
                self.page.wait_for_selector(sel, timeout=4000)
                print(f"[extract_urls] job-card selector ready: '{sel}'")
                break
            except Exception:
                continue

        # Scroll once to reveal any below-fold lazy cards
        self.page.evaluate("window.scrollBy(0, 600)")
        time.sleep(0.6)
        self.page.evaluate("window.scrollBy(0, -600)")
        time.sleep(0.3)

        try:
            raw_links: List[str] = self.page.evaluate("""
                () => {
                    // Build exclusion set — links inside nav / header / footer
                    const excluded = new Set();
                    ['nav', 'header', 'footer',
                     '[class*="footer"]', '[class*="header"]',
                     '[class*="navbar"]', '[class*="nav-bar"]'
                    ].forEach(sel => {
                        try {
                            document.querySelectorAll(sel).forEach(el =>
                                el.querySelectorAll('a[href]').forEach(a => excluded.add(a))
                            );
                        } catch(e) {}
                    });

                    // Collect from content / job-card containers first
                    const priority = new Set();
                    ['main', 'article', '[class*="jobTuple"]', '[class*="job-card"]',
                     '[class*="srp"]', '[class*="result"]', '[class*="listing"]',
                     '[class*="card"]', '[class*="job"]', 'li'
                    ].forEach(sel => {
                        try {
                            document.querySelectorAll(sel + ' a[href]').forEach(a => {
                                if (!excluded.has(a) && a.href.startsWith('http'))
                                    priority.add(a.href);
                            });
                        } catch(e) {}
                    });

                    // Fallback: all non-excluded anchors
                    const all = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        if (!excluded.has(a) && a.href.startsWith('http'))
                            all.push(a.href);
                    });

                    return [...priority, ...all];
                }
            """)
        except Exception as e:
            print(f"[extract_urls] JS eval failed: {e}")
            return []

        seen: set = set()
        results: List[str] = []

        # Priority 1: href contains job-detail path signals
        DETAIL_SIGNALS = (
            "job-listings", "/job-listings", "-jid-",
            "/job-", "/job/", "/jobs/",
            "jobId=", "job_id=", "-job-",
            "jobdetail", "viewjob", "jd/",
        )
        for href in raw_links:
            parsed = urlparse(href)
            if parsed.netloc != base_domain:
                continue
            clean = href.split("?")[0].rstrip("/")
            if clean in (listing_base.rstrip("/"), root_url.rstrip("/"),
                         start_url.rstrip("/")):
                continue
            path_qs = (parsed.path + "?" + parsed.query).lower()
            if any(sig in path_qs for sig in DETAIL_SIGNALS):
                if href not in seen:
                    seen.add(href)
                    results.append(href)
            if len(results) >= n:
                break

        # Priority 2: any same-domain content-area link that isn't listing/homepage
        if len(results) < n:
            SKIP_PATTERNS = ("/faq", "/imposter", "/report", "/help",
                             "/about", "/contact", "/privacy", "/terms",
                             "/login", "/register", "/signup", "/sitemap")
            for href in raw_links:
                if len(results) >= n:
                    break
                parsed = urlparse(href)
                if parsed.netloc != base_domain:
                    continue
                clean = href.split("?")[0].rstrip("/")
                if clean in (listing_base.rstrip("/"), root_url.rstrip("/"),
                             start_url.rstrip("/")):
                    continue
                if any(skip in parsed.path.lower() for skip in SKIP_PATTERNS):
                    continue
                if href not in seen:
                    seen.add(href)
                    results.append(href)

        print(f"[extract_urls] found {len(results)} result URLs:")
        for u in results:
            print(f"  {u}")
        return results[:n]

    def _extract_jd(self, url: str, state: AgentState) -> Dict[str, Any]:
        """
        Navigate to a single detail URL and run a short LLM loop to extract
        the job description. Returns a job dict.
        """
        print(f"\n[JD] Extracting from: {url}")
        # Reset per-URL step count so the loop guard doesn't trigger
        step_backup = state._url_step_count.copy()
        history_backup = state.history[:]
        state._url_step_count = {}
        state.history = []
        state.phase = "detail"

        self._do_navigate(url)
        time.sleep(1.0)

        jd_data: Dict[str, Any] = {"url": url, "title": "", "jd_text": ""}

        for step in range(1, 8):   # cap at 7 steps per detail page
            snapshot = build_snapshot(self.page)
            url_before = snapshot.url
            _, inputs, buttons, links = _build_element_list(snapshot)

            try:
                action = self._ask(state, snapshot)
            except AllGroqKeysRateLimited as e:
                print(f"[JD] ⛔ Rate limited ({e.retry_after:.0f}s) — using DOM fallback directly.")
                action = None
            if not action:
                break

            act = action.get("action", "")

            if act in ("extract", "done"):
                jd_data["title"]   = snapshot.title
                jd_data["jd_text"] = snapshot.text[:6000]
                print(f"[JD] ✅ extracted  title='{snapshot.title[:60]}'  chars={len(jd_data['jd_text'])}")
                break

            if act == "fail":
                print(f"[JD] ⚠ LLM flagged failure: {action.get('reason')}")
                break

            # Execute the step
            result_str, _ = self._execute(action, inputs, buttons, links, url_before)
            state.add_step(act, target=action.get("target",""),
                           result=result_str, url_before=url_before, url_after=self.page.url)
            if self.verbose:
                print(f"[JD]   step {step}: {act}  → {result_str[:80]}")

        # Restore shared history + url counts
        state._url_step_count = step_backup
        state.history = history_backup

        # Fallback: if LLM loop produced nothing, extract page text directly.
        # We are already on the detail page — no need for LLM confirmation.
        if not jd_data.get("jd_text"):
            snapshot = build_snapshot(self.page)
            jd_data["title"]   = snapshot.title
            jd_data["jd_text"] = snapshot.text[:6000]
            print(f"[JD] 📥 Fallback direct extract: title='{snapshot.title[:60]}'  "
                  f"chars={len(jd_data['jd_text'])}")

        return jd_data



    def _execute(
        self,
        action: Dict,
        inputs: List[PageItem],
        buttons: List[PageItem],
        links: List[PageItem],
        url_before: str,
    ) -> tuple[str, Optional[Dict]]:
        """
        Returns (result_string, extracted_data_or_None).
        extracted_data is only set when action == 'extract'.
        """
        act    = action.get("action", "")
        target = action.get("target", "")

        if act == "click":
            el = self._resolve_element(target, inputs, buttons, links)
            if not el:
                return f"failed: could not resolve target '{target}'", None
            result = self._do_click(el, url_before)
            return result, None

        elif act == "type":
            el = self._resolve_element(target, inputs, buttons, links)
            if not el:
                return f"failed: could not resolve target '{target}'", None
            result = self._do_type(el, action.get("value", ""))
            return result, None

        elif act == "scroll":
            result = self._do_scroll(action.get("direction", "down"))
            return result, None

        elif act == "navigate":
            result = self._do_navigate(action.get("url", ""))
            return result, None

        elif act == "key":
            result = self._do_key(action.get("key", "Enter"))
            return result, None

        elif act in ("extract", "done"):
            data = self._do_extract()
            return "ok — extracted", data

        elif act == "fail":
            return f"agent_fail: {action.get('reason', '')}", None

        else:
            return f"unknown action: {act}", None

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, url: str, goal: str, target_count: int = 3) -> Dict[str, Any]:
        """
        Pattern C — URL Queue + phase state machine.

        Phase "search":
          LLM drives the browser until it reaches a results/listing page,
          then returns action "extract". Code takes over, scrapes result URLs
          deterministically, and pushes them to the queue.

        Phase "detail":
          For each URL in the queue, run _extract_jd() (its own mini LLM loop).
          Append result to collected_jobs. When queue is empty or target reached, done.

        The LLM never decides phase transitions — only the code does.
        """
        state = AgentState(
            goal=goal,
            start_url=url,
            max_steps=self.max_steps,
            target_count=target_count,
            phase="search",
        )

        if self.verbose:
            print(f"\n[Agent] 🚀 Starting  goal='{goal}'  url={url}  target={target_count} JDs")

        if self.page.url.rstrip("/") != url.rstrip("/"):
            self._do_navigate(url)
        state.current_url = self.page.url

        # ── PHASE: search ────────────────────────────────────────────────────
        for step in range(1, state.max_steps + 1):
            if self.verbose:
                print(f"\n[Agent] ── Search step {step}/{state.max_steps}  url={self.page.url} ──")

            snapshot = build_snapshot(self.page)
            state.current_url = snapshot.url
            url_before = snapshot.url

            _, inputs, buttons, links = _build_element_list(snapshot)
            print(f"[Agent] snapshot: {len(inputs)} inputs, {len(buttons)} buttons, {len(links)} links")
            for i, el in enumerate(inputs):
                print(f"[Agent]   INPUT[{i}]  tag={el.tag}  text='{el.text[:70]}'  sel='{el.selector}'")
            for i, el in enumerate(buttons[:10]):
                print(f"[Agent]   BUTTON[{i}] tag={el.tag}  text='{el.text[:70]}'  sel='{el.selector}'")

            # ── Deterministic results-page detector (no LLM needed) ─────────
            # Require inputs or buttons — nav links alone don't mean DOM is ready
            dom_ready = len(inputs) > 0 or len(buttons) > 0
            if dom_ready and self._is_results_page(snapshot.url, state.start_url):
                print(f"[Agent] 📋 Auto-detected results page (URL pattern match): {snapshot.url}")
                state.listing_url = self.page.url
                # Small settle wait so lazy-loaded job cards are in the DOM
                time.sleep(1.5)
                state.url_queue = self._extract_result_urls(n=target_count, start_url=state.start_url)
                if not state.url_queue:
                    print(f"[Agent] ⚠ On results page but no detail URLs extracted — continuing via LLM")
                else:
                    state.phase = "detail"
                    break
            elif self._is_results_page(snapshot.url, state.start_url):
                print(f"[Agent] ⏳ Results URL detected but DOM not ready yet — waiting...")

            try:
                action = self._ask(state, snapshot)
            except AllGroqKeysRateLimited as e:
                wait = e.retry_after
                if wait <= 120:
                    print(f"[Agent] ⏳ Rate limited. Waiting {wait:.0f}s then retrying once...")
                    time.sleep(wait + 1.0)
                    try:
                        action = self._ask(state, snapshot)
                    except Exception:
                        action = None
                else:
                    print(f"[Agent] ⛔ Groq rate limited for {wait:.0f}s ({wait/60:.1f} min). Aborting run — try again later.")
                    return {
                        "success": False,
                        "error": f"groq_rate_limited:{wait:.0f}s",
                        "retry_after_seconds": wait,
                        "collected_jobs": state.collected_jobs,
                        "steps": state.history,
                        "final_url": self.page.url,
                    }
            if not action:
                state.add_step("llm_error", result="failed: no response", url_before=url_before)
                continue

            act = action.get("action", "")

            # ── dedup guard ─────────────────────────────────────────────────
            action_key = f"{act}:{action.get('target','')}:{action.get('url','')}:{action.get('value','')}"
            already_failed = any(
                h.get("_action_key") == action_key and h.get("result", "").startswith("failed")
                for h in state.history
            )
            if already_failed:
                print(f"[Agent] ↩ Skipping repeated failed action: {action_key}")
                state.add_step(act, target=action.get("target",""),
                               result="skipped (repeated failure)", url_before=url_before)
                state.history[-1]["_action_key"] = action_key
                continue

            # ── LLM says results are ready → transition to detail phase ─────
            if act in ("extract", "done"):
                state.listing_url = self.page.url
                print(f"[Agent] 📋 Results page reached: {state.listing_url}")

                # Deterministic URL extraction — NO LLM
                state.url_queue = self._extract_result_urls(n=target_count, start_url=state.start_url)
                if not state.url_queue:
                    return {
                        "success": False,
                        "error": "Could not extract any result URLs from listing page",
                        "steps": state.history,
                        "final_url": self.page.url,
                    }
                state.phase = "detail"
                break   # exit search loop → enter detail loop below

            if act == "fail":
                reason = action.get("reason", "no reason given")
                return {
                    "success": False,
                    "error": f"Search phase failed: {reason}",
                    "steps": state.history,
                    "final_url": self.page.url,
                }

            # Execute search-phase action
            result_str, _ = self._execute(action, inputs, buttons, links, url_before)
            url_after = self.page.url
            state.add_step(act, target=action.get("target",""),
                           value=action.get("value",""), result=result_str,
                           url_before=url_before, url_after=url_after)
            state.history[-1]["_action_key"] = action_key

            if self.verbose:
                print(f"[Agent]   result: {result_str}")

            # Infinite-loop guard
            visit_count = state.record_url(url_after)
            if visit_count >= 4:
                print(f"[Agent] ⚠ Same URL seen {visit_count}x in search phase — aborting search")
                break

        # ── PHASE: detail ────────────────────────────────────────────────────
        if state.phase == "detail" and state.url_queue:
            print(f"\n[Agent] 🔍 Detail phase — {len(state.url_queue)} URLs in queue")
            for detail_url in state.url_queue:
                if len(state.collected_jobs) >= target_count:
                    break
                if detail_url in state.visited_urls:
                    continue
                state.visited_urls.append(detail_url)

                jd = self._extract_jd(detail_url, state)
                if jd.get("jd_text"):
                    state.collected_jobs.append(jd)
                    print(f"[Agent] ✅ Collected {len(state.collected_jobs)}/{target_count}: {jd['title'][:60]}")

                # Navigate back to listing between detail pages (most reliable)
                if len(state.collected_jobs) < target_count and state.url_queue:
                    self._do_navigate(state.listing_url)
                    time.sleep(0.8)

        state.phase = "done"
        print(f"\n[Agent] 🏁 Done — collected {len(state.collected_jobs)} job descriptions")

        return {
            "success": True,
            "collected_jobs": state.collected_jobs,
            "listing_url": state.listing_url,
            "steps": state.history,
            "final_url": self.page.url,
        }
