"""
LLM-powered semantic matching.

Provider selection:
    LLM_PROVIDER=auto   -> Groq when keys exist, otherwise Ollama if running.
    LLM_PROVIDER=groq   -> Groq only.
    LLM_PROVIDER=ollama -> Ollama only.
    LLM_PROVIDER=none   -> disable LLM usage.

Groq is accessed through app.llm.groq_client.GroqRouterClient, which supports
multiple keys and conservative local RPM/TPM/RPD/TPD accounting.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from ..schemas import LLMElementResult, PageItem
from .groq_client import (
    AllGroqKeysRateLimited,
    GroqClientError,
    get_default_groq_client,
    load_groq_api_keys_from_env,
)

OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").strip().lower()


def ollama_available() -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def groq_available() -> bool:
    return bool(load_groq_api_keys_from_env())


def llm_available() -> bool:
    if LLM_PROVIDER in {"none", "off", "false", "0"}:
        return False
    if LLM_PROVIDER == "groq":
        return groq_available()
    if LLM_PROVIDER == "ollama":
        return ollama_available()
    return groq_available() or ollama_available()


def _extract_json_array(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, list) else None
        except Exception:
            return None
    return None


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except Exception:
            pass
    # Handle common fenced JSON output.
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        try:
            value = json.loads(match.group(1))
            return value if isinstance(value, dict) else None
        except Exception:
            return None
    return None


class LLMSemanticMatcher:
    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, provider: Optional[str] = None):
        self.provider = (provider or LLM_PROVIDER or "auto").strip().lower()
        self.ollama_model = model
        self._ollama_is_available = ollama_available() if self.provider in {"auto", "ollama"} else False
        self._groq_is_available = groq_available() if self.provider in {"auto", "groq"} else False
        self.available = self.provider not in {"none", "off", "false", "0"} and (
            self._groq_is_available or self._ollama_is_available
        )

    @property
    def active_provider(self) -> str:
        if self.provider == "groq" and self._groq_is_available:
            return "groq"
        if self.provider == "ollama" and self._ollama_is_available:
            return "ollama"
        if self.provider == "auto":
            if self._groq_is_available:
                return "groq"
            if self._ollama_is_available:
                return "ollama"
        return "none"

    def _call_ollama(self, prompt: str, system: str = "", temperature: float = 0.1) -> str:
        if not self._ollama_is_available:
            return ""
        try:
            payload = {
                "model": self.ollama_model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {"temperature": temperature},
            }
            r = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=60.0)
            if r.status_code == 200:
                return r.json().get("response", "")
        except Exception:
            pass
        return ""

    def _call_groq(self, prompt: str, system: str = "", temperature: float = 0.1, max_tokens: int = 800) -> str:
        if not self._groq_is_available:
            return ""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            response = get_default_groq_client().chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.content
        except (AllGroqKeysRateLimited, GroqClientError):
            # In auto mode, gracefully fall back to local Ollama if Groq is exhausted.
            if self.provider == "auto" and self._ollama_is_available:
                return self._call_ollama(prompt, system=system, temperature=temperature)
            return ""

    def _call_llm(self, prompt: str, system: str = "", temperature: float = 0.1, max_tokens: int = 800) -> str:
        provider = self.active_provider
        if provider == "groq":
            return self._call_groq(prompt, system=system, temperature=temperature, max_tokens=max_tokens)
        if provider == "ollama":
            return self._call_ollama(prompt, system=system, temperature=temperature)
        return ""

    def rank_elements(self, prompt: str, items: List[PageItem]) -> List[PageItem]:
        if not self.available or not items:
            return items

        context_lines = []
        for i, item in enumerate(items[:40]):
            href = f" href={item.href[:80]}" if item.href else ""
            context_lines.append(f"[{i}] {item.tag or 'element'}: {item.text[:140]!r}{href}")

        system = (
            "You are a web element ranker. Given a user prompt and a list of page elements, "
            "return only a JSON array of indices sorted by relevance, best first. "
            "No prose. Example: [3, 0, 1]."
        )
        user_prompt = (
            f"User is looking for: {prompt!r}\n\n"
            f"Page elements:\n" + "\n".join(context_lines) + "\n\n"
            "Return only a JSON array of indices."
        )

        response = self._call_llm(user_prompt, system=system, max_tokens=300)
        indices = _extract_json_array(response)
        if indices is None:
            return items

        reordered: List[PageItem] = []
        for idx in indices:
            if isinstance(idx, int) and 0 <= idx < len(items):
                reordered.append(items[idx])
        seen = {id(x) for x in reordered}
        for item in items:
            if id(item) not in seen:
                reordered.append(item)
        return reordered

    def classify_element(self, text: str, tag: str) -> LLMElementResult:
        if not self.available:
            return LLMElementResult(
                element_type="unknown",
                text=text,
                confidence=0.0,
                reason="LLM not available",
            )

        system = (
            "Classify a web UI element. Return only JSON with keys: "
            "element_type (button|link|input|unknown), confidence (0-1), reason (short string)."
        )
        prompt = f"Element tag: {tag}\nElement text: {text!r}\n\nClassify this element."
        data = _extract_json_object(self._call_llm(prompt, system=system, max_tokens=250))
        if data:
            try:
                return LLMElementResult(
                    element_type=data.get("element_type", "unknown"),
                    text=text,
                    confidence=float(data.get("confidence", 0.5)),
                    reason=data.get("reason", ""),
                    selector_hint=data.get("selector_hint"),
                )
            except Exception:
                pass

        return LLMElementResult(
            element_type="unknown",
            text=text,
            confidence=0.0,
            reason="Parse failed",
        )

    def extract_field(self, field_name: str, page_text: str) -> str:
        if not self.available:
            return ""

        system = (
            "Extract a specific field from web page text. Return only the extracted value, "
            "or NOT_FOUND if the value is not present."
        )
        prompt = (
            f"Extract the {field_name!r} from this page text. Return only the value.\n\n"
            f"Page text:\n{page_text[:6000]}"
        )
        response = self._call_llm(prompt, system=system, max_tokens=300).strip()
        if response and response.upper() != "NOT_FOUND":
            return response.strip('"')
        return ""

    def extract_json(self, schema: Dict[str, Any], page_text: str, url: str = "") -> Dict[str, Any]:
        if not self.available:
            return {}
        system = (
            "You extract structured data from web pages. Return only valid JSON. "
            "Use null for missing scalar values and [] for missing arrays. Do not invent facts."
        )
        prompt = (
            "Extract data from this page according to the requested schema.\n\n"
            f"URL: {url}\n"
            f"Schema JSON:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
            f"Page text:\n{page_text[:12000]}\n\n"
            "Return only the JSON object."
        )
        response = self._call_llm(prompt, system=system, temperature=0.0, max_tokens=1200)
        return _extract_json_object(response) or {}

    def decide_action(self, objective: str, page_summary: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Return the next browser action for an autonomous task loop."""
        if not self.available:
            return {"action": "extract", "target": "page", "value": "", "reason": "LLM not available"}

        system = (
            "You control a browser. Choose exactly one next action from: "
            "click, type, scroll, wait, extract, done. Return only JSON with keys "
            "action, target, value, reason. The target must match visible text, an input label, "
            "or a concise natural language description of the desired element. "
            "Never choose destructive actions such as purchasing, submitting payments, deleting accounts, "
            "or changing passwords."
        )
        prompt = (
            f"Objective: {objective}\n\n"
            f"Recent history:\n{json.dumps(history[-6:], ensure_ascii=False, indent=2)}\n\n"
            f"Current page:\n{page_summary[:9000]}\n\n"
            "Choose the next action as JSON."
        )
        response = self._call_llm(prompt, system=system, temperature=0.0, max_tokens=500)
        data = _extract_json_object(response)
        if not data:
            return {"action": "extract", "target": "page", "value": "", "reason": "could not parse LLM action"}

        action = str(data.get("action", "extract")).lower().strip()
        if action not in {"click", "type", "scroll", "wait", "extract", "done"}:
            action = "extract"
        return {
            "action": action,
            "target": str(data.get("target", "") or ""),
            "value": str(data.get("value", "") or ""),
            "reason": str(data.get("reason", "") or ""),
        }

    def generate_selector(self, element_description: str, page_html: str) -> str:
        if not self.available:
            return ""

        system = (
            "You are a CSS selector expert. Given an element description and page HTML, "
            "return the most resilient CSS selector. Prefer data-testid, id, aria-label, name, "
            "and stable classes. Avoid nth-child and random classes. Return only the selector."
        )
        prompt = (
            f"Find element: {element_description!r}\n\n"
            f"Page HTML snippet:\n{page_html[:4000]}\n\n"
            "Return only the CSS selector."
        )
        return self._call_llm(prompt, system=system, temperature=0.0, max_tokens=200).strip()
