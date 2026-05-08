from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .dom import build_snapshot
from .service import WebIntelService


BLOCKED_ACTION_WORDS = {
    "buy", "purchase", "checkout", "pay", "payment", "delete", "remove account",
    "close account", "change password", "transfer", "withdraw", "send money",
}


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


class BrowserAgent:
    """
    TinyFish/AgentQL-like browser task loop.

    It repeatedly snapshots the live page, asks the configured LLM for the next
    safe action, executes the action with Playwright, and returns the trace.
    """

    def __init__(self, service: WebIntelService):
        self.service = service

    def run(
        self,
        url: str,
        task: str,
        max_steps: int = 8,
        schema: Optional[Dict[str, Any]] = None,
        screenshot_on_finish: bool = False,
    ) -> Dict[str, Any]:
        self.service.open_page(url)
        steps: List[Dict[str, Any]] = []
        extracted: Any = None
        success = False
        error: Optional[str] = None

        for step_index in range(max(1, max_steps)):
            snapshot = build_snapshot(self.service.session.page)
            page_summary = self._page_summary(snapshot)

            if not self.service.llm:
                action = {"action": "extract", "target": "page", "value": "", "reason": "LLM disabled/unavailable"}
            else:
                action = self.service.llm.decide_action(task, page_summary, steps)

            action_name = str(action.get("action", "extract")).lower().strip()
            target = str(action.get("target", "") or "")
            value = str(action.get("value", "") or "")
            reason = str(action.get("reason", "") or "")

            record: Dict[str, Any] = {
                "step": step_index + 1,
                "action": action_name,
                "target": target,
                "value": value,
                "reason": reason,
                "url_before": snapshot.url,
            }

            try:
                if self._looks_destructive(action_name, target, value):
                    raise ValueError(f"Blocked potentially destructive action: {action_name} {target}")

                if action_name == "done":
                    record["result"] = {"done": True}
                    steps.append(record)
                    success = True
                    break

                if action_name == "click":
                    record["result"] = self.service.click_element(target)

                elif action_name == "type":
                    record["result"] = self.service.type_text(target, value)

                elif action_name == "scroll":
                    amount = self._safe_int(value, default=800)
                    self.service.session.human_like_scroll(amount)
                    record["result"] = {"scrolled": amount}

                elif action_name == "wait":
                    seconds = min(10.0, max(0.2, float(value or 2)))
                    self.service.session.page.wait_for_timeout(int(seconds * 1000))
                    record["result"] = {"waited_seconds": seconds}

                elif action_name == "extract":
                    extracted = self.service.extract_structured(schema=schema)
                    record["result"] = {"extracted": True}
                    steps.append(record)
                    success = True
                    break

                else:
                    extracted = self.service.extract_structured(schema=schema)
                    record["result"] = {"fallback": "extract", "extracted": True}
                    steps.append(record)
                    success = True
                    break

                record["url_after"] = self.service.session.page.url
                steps.append(record)

            except Exception as exc:
                record["error"] = str(exc)
                record["url_after"] = self.service.session.page.url
                steps.append(record)
                error = str(exc)
                break

        final_snapshot = build_snapshot(self.service.session.page)
        if extracted is None and success:
            extracted = self.service.extract_structured(schema=schema)

        screenshot_path = None
        if screenshot_on_finish:
            screenshot_path = f"agent_{int(time.time())}.png"
            self.service.screenshot(screenshot_path)

        return {
            "success": success,
            "error": error,
            "task": task,
            "start_url": url,
            "final_url": final_snapshot.url,
            "title": final_snapshot.title,
            "steps": steps,
            "data": _model_to_dict(extracted),
            "screenshot": screenshot_path,
            "llm_used": self.service.use_llm,
        }

    def _page_summary(self, snapshot) -> str:
        lines = [
            f"URL: {snapshot.url}",
            f"TITLE: {snapshot.title}",
            "",
            "INPUTS:",
        ]
        for i, item in enumerate(snapshot.inputs[:30]):
            lines.append(f"  input[{i}] {item.text[:160]!r}")
        lines.append("BUTTONS:")
        for i, item in enumerate(snapshot.buttons[:60]):
            lines.append(f"  button[{i}] {item.text[:160]!r}")
        lines.append("LINKS:")
        for i, item in enumerate(snapshot.links[:80]):
            href = f" -> {item.href[:120]}" if item.href else ""
            lines.append(f"  link[{i}] {item.text[:160]!r}{href}")
        lines.append("PAGE TEXT SNIPPET:")
        lines.append(snapshot.text[:2500])
        return "\n".join(lines)

    def _looks_destructive(self, action: str, target: str, value: str) -> bool:
        if action not in {"click", "type"}:
            return False
        text = f"{action} {target} {value}".lower()
        return any(word in text for word in BLOCKED_ACTION_WORDS)

    def _safe_int(self, raw: str, default: int) -> int:
        try:
            return int(float(raw))
        except Exception:
            return default
