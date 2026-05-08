from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urljoin

from .browser import BrowserSession
from .dom import build_snapshot
from .detectors import detect_candidate_items
from .extractors import extract_job_detail
from .prompts import find_element as heuristic_find_element
from .prompts import find_element_multi
from .llm.semantic import LLMSemanticMatcher, llm_available
from .parser.aql_parser import parse_aql
from .robust.healing import SelectorHealer

# Cloudflare guard — called after every navigation and action
from .cloudflare_handler import handle_cloudflare

# Import each adapter directly; the adapters package does not expose them all via __init__.
from .adapters.generic import GenericAdapter
from .adapters.greenhouse import GreenhouseAdapter
from .adapters.lever import LeverAdapter
from .adapters.linkedin import LinkedInAdapter
from .adapters.indeed import IndeedAdapter


ADAPTERS = [
    LinkedInAdapter(),
    IndeedAdapter(),
    GreenhouseAdapter(),
    LeverAdapter(),
    GenericAdapter(),
]


def get_adapter(url: str):
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    return GenericAdapter()


def _model_to_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, list):
        return [_model_to_dict(v) for v in value]
    if isinstance(value, dict):
        return {k: _model_to_dict(v) for k, v in value.items()}
    return value


class WebIntelService:
    def __init__(self, session: BrowserSession, use_llm: bool = True):
        self.session = session
        self.adapter = None
        self.use_llm = bool(use_llm and llm_available())
        self.llm = LLMSemanticMatcher() if self.use_llm else None
        self.healer = SelectorHealer(self.llm)

    # --------------------------------------------------------------------- #
    # Page navigation / basic helpers
    # --------------------------------------------------------------------- #
    def open_page(self, url: str):
        self.session.goto(url)
        handle_cloudflare(self.session.page, max_wait=90, verbose=True)
        self.adapter = get_adapter(url)
        return build_snapshot(self.session.page)

    # --------------------------------------------------------------------- #
    # Item detection
    # --------------------------------------------------------------------- #
    def list_items(self, use_smart: bool = True):
        if self.adapter and hasattr(self.adapter, "detect_items"):
            raw = self.adapter.detect_items(self.session.page)
            from .schemas import PageItem

            items = []
            for i, r in enumerate(raw):
                if isinstance(r, PageItem):
                    item = r
                    item.index = i
                elif isinstance(r, dict):
                    item = PageItem(
                        index=i,
                        text=r.get("text", ""),
                        href=r.get("href"),
                        tag=r.get("tag", "unknown"),
                        selector=r.get("selector"),
                        score=r.get("score"),
                    )
                else:
                    item = PageItem(
                        index=i,
                        text=getattr(r, "text", ""),
                        href=getattr(r, "href", None),
                        tag=getattr(r, "tag", "repeated_block"),
                        selector=getattr(r, "selector", None),
                        score=getattr(r, "score", None),
                    )
                items.append(item)

            if self.llm and items:
                items = self.llm.rank_elements("main content item or listing", items)
            return items

        # fallback: generic detection based on repeated blocks / link list
        return detect_candidate_items(self.session.page, use_repeated_blocks=use_smart)

    # --------------------------------------------------------------------- #
    # Detail / structured extraction
    # --------------------------------------------------------------------- #
    def open_item(self, item_index: int):
        items = self.list_items()
        if item_index >= len(items):
            raise IndexError("item_index out of range")

        item = items[item_index]
        href = item.href
        if not href:
            raise ValueError("Selected item has no href")

        target_url = urljoin(self.session.page.url, href)
        self.session.goto(target_url)
        handle_cloudflare(self.session.page, max_wait=90, verbose=True)
        self.adapter = get_adapter(target_url)
        return {"url": self.session.page.url, "title": self.session.page.title()}

    def extract_detail(self):
        if self.adapter:
            detail = self.adapter.extract_detail(self.session.page)
            if self.llm:
                page_text = build_snapshot(self.session.page).text
                if not detail.company:
                    detail.company = self.llm.extract_field("company name", page_text)
                if not detail.location:
                    detail.location = self.llm.extract_field("job location", page_text)
                if not detail.salary:
                    detail.salary = self.llm.extract_field("salary or compensation", page_text)
            return detail

        return extract_job_detail(self.session.page)

    def extract_structured(self, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        snapshot = build_snapshot(self.session.page)
        if schema and self.llm:
            extracted = self.llm.extract_json(schema=schema, page_text=snapshot.text, url=snapshot.url)
            if extracted:
                return extracted
        return {
            "url": snapshot.url,
            "title": snapshot.title,
            "text": snapshot.text[:5000],
            "links": [_model_to_dict(link) for link in snapshot.links[:50]],
            "buttons": [_model_to_dict(button) for button in snapshot.buttons[:50]],
            "inputs": [_model_to_dict(inp) for inp in snapshot.inputs[:30]],
        }

    # --------------------------------------------------------------------- #
    # Smart element location
    # --------------------------------------------------------------------- #
    def find_element(self, prompt: str):
        snapshot = build_snapshot(self.session.page)
        result = find_element_multi(prompt, snapshot)

        # If heuristic score is low, ask the LLM to re-rank all interactive elements.
        if self.llm and result and (result.score is None or result.score < 20):
            all_items = snapshot.buttons + snapshot.links + snapshot.inputs
            reranked = self.llm.rank_elements(prompt, all_items)
            if reranked:
                result = reranked[0]
                result.llm_confidence = 0.8

        return result

    # --------------------------------------------------------------------- #
    # Action helpers
    # --------------------------------------------------------------------- #
    def click_element(self, prompt: str):
        if not prompt:
            raise ValueError("click target is required")
        element = self.find_element(prompt)
        if not element:
            raise ValueError(f"No element found for prompt: {prompt}")

        page = self.session.page
        errors = []

        if element.selector:
            try:
                locator = page.locator(element.selector).first
                locator.scroll_into_view_if_needed(timeout=5000)
                locator.click(timeout=10000)
                self._settle_after_action()
                return {
                    "clicked": element.text,
                    "selector": element.selector,
                    "url": page.url,
                    "score": element.score,
                    "llm_confidence": element.llm_confidence,
                }
            except Exception as exc:
                errors.append(f"selector click failed: {exc}")

        if element.tag == "a" and element.href:
            try:
                target_url = urljoin(page.url, element.href)
                self.session.goto(target_url)
                self.adapter = get_adapter(target_url)
                return {"clicked": element.text, "href": element.href, "url": page.url, "score": element.score}
            except Exception as exc:
                errors.append(f"href navigation failed: {exc}")

        try:
            if element.tag == "button":
                locator = page.get_by_role("button", name=element.text, exact=False)
                if locator.count() > 0:
                    locator.first.click(timeout=10000)
                    self._settle_after_action()
                    return {"clicked": element.text, "url": page.url, "score": element.score}
        except Exception as exc:
            errors.append(f"role click failed: {exc}")

        try:
            page.get_by_text(element.text, exact=False).first.click(timeout=10000)
            self._settle_after_action()
            return {"clicked": element.text, "url": page.url, "score": element.score}
        except Exception as exc:
            errors.append(f"text click failed: {exc}")

        raise RuntimeError({"prompt": prompt, "element": _model_to_dict(element), "errors": errors})

    def type_text(self, prompt: str, value: str):
        if not prompt:
            raise ValueError("input target is required")
        snapshot = build_snapshot(self.session.page)
        element = heuristic_find_element(prompt, snapshot.inputs)
        if not element:
            raise ValueError(f"No input found for prompt: {prompt}")

        page = self.session.page
        errors = []
        if element.selector:
            try:
                locator = page.locator(element.selector).first
                locator.scroll_into_view_if_needed(timeout=5000)
                locator.fill(value, timeout=10000)
                self._settle_after_action(short=True)
                return {"typed": value, "into": element.text, "selector": element.selector}
            except Exception as exc:
                errors.append(f"selector fill failed: {exc}")

        for fn_name, fn in [
            ("placeholder", lambda: page.get_by_placeholder(element.text).fill(value, timeout=10000)),
            ("label", lambda: page.get_by_label(element.text).fill(value, timeout=10000)),
        ]:
            try:
                fn()
                self._settle_after_action(short=True)
                return {"typed": value, "into": element.text, "strategy": fn_name}
            except Exception as exc:
                errors.append(f"{fn_name} fill failed: {exc}")

        raise RuntimeError({"prompt": prompt, "input": _model_to_dict(element), "errors": errors})

    def _settle_after_action(self, short: bool = False) -> None:
        try:
            self.session.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            self.session.page.wait_for_timeout(500 if short else 1000)
        except Exception:
            pass
        # Silent CF guard after every click/type — catches mid-session challenges
        handle_cloudflare(self.session.page, max_wait=60, verbose=False)

    # --------------------------------------------------------------------- #
    # Healing / LLM fall-backs
    # --------------------------------------------------------------------- #
    def heal_selector(self, error_msg: str) -> str:
        """Attempt to heal a broken selector using the healer."""
        healed = self.healer.heal(self.session.page, error_msg)
        if healed:
            self.healer.record_healing(self.session.page.url, error_msg, healed)
        return healed

    # --------------------------------------------------------------------- #
    # AQL handling
    # --------------------------------------------------------------------- #
    def query_aql(self, aql_str: str):
        plan = parse_aql(aql_str)
        items = self.list_items()
        results = []

        field_names = list(plan.get("fields", {}).keys())
        if plan["is_list"]:
            for item in items[: plan.get("max_items", 20)]:
                self.open_item(item.index)
                detail = self.extract_detail()
                row = {}
                for field in field_names:
                    row[field] = getattr(detail, field, None)
                results.append(row)
                self.go_back()
        else:
            detail = self.extract_detail()
            results = {field: getattr(detail, field, None) for field in field_names}

        return {"plan": plan, "results": results}

    # --------------------------------------------------------------------- #
    # Navigation helpers
    # --------------------------------------------------------------------- #
    def go_back(self):
        self.session.page.go_back()
        self.session.page.wait_for_timeout(1000)
        return {"url": self.session.page.url}

    def screenshot(self, path: str = "screenshot.png"):
        return self.session.screenshot(path)
