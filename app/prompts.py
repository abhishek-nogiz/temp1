from __future__ import annotations

from typing import List, Optional
from .schemas import PageItem


SEMANTIC_MAP = {
    "search": ["search", "find", "lookup", "query", "filter", "explore"],
    "apply": ["apply", "submit", "send", "upload", "post", "register"],
    "next": ["next", "more", "load more", "show more", "pagination", "page"],
    "previous": ["prev", "previous", "back", "older"],
    "login": ["login", "sign in", "log in", "authenticate"],
    "signup": ["sign up", "register", "create account", "join"],
    "contact": ["contact", "email", "message", "reach out"],
    "cart": ["cart", "basket", "bag", "checkout"],
    "menu": ["menu", "navigation", "nav", "hamburger"],
    "close": ["close", "dismiss", "exit", "x", "cancel"],
    "save": ["save", "bookmark", "favorite", "star", "keep"],
    "download": ["download", "export", "pdf", "csv"],
}


def score_prompt(text: str, prompt: str) -> float:
    text_l = (text or "").lower()
    prompt_l = (prompt or "").lower()

    score = 0.0
    if not text_l or not prompt_l:
        return score

    prompt_tokens = [t for t in prompt_l.split() if len(t) > 1]
    for token in prompt_tokens:
        if token in text_l:
            score += 5.0

    for category, keywords in SEMANTIC_MAP.items():
        if category in prompt_l:
            for kw in keywords:
                if kw in text_l:
                    score += 8.0
                    break

    if prompt_l in text_l:
        score += 15.0
    if text_l in prompt_l and len(text_l) >= 3:
        score += 8.0

    for i in range(len(prompt_tokens)):
        for j in range(i + 1, len(prompt_tokens) + 1):
            phrase = " ".join(prompt_tokens[i:j])
            if len(phrase) >= 3 and phrase in text_l:
                score += 3.0 * (j - i)

    if any(role in text_l for role in ["button", "submit", "input", "search"]):
        score += 2.0

    if "first" in prompt_l:
        score += 1.0

    return score


def find_element(prompt: str, items: List[PageItem], top_k: int = 1) -> Optional[PageItem] | List[PageItem]:
    if not items:
        return None if top_k == 1 else []

    prompt_l = (prompt or "").lower().strip()
    prompt_word_count = len(prompt_l.split())

    scored = []
    for item in items:
        s = score_prompt(item.text, prompt)

        tag = (item.tag or "").lower()
        text_l = (item.text or "").lower().strip()

        # Strong boost for short exact matches on actionable elements
        if text_l == prompt_l and tag in ("button", "input", "a"):
            s += 40.0
        elif text_l == prompt_l:
            s += 25.0

        # Prefer buttons/inputs over long-text links for short action prompts
        if tag == "button" and prompt_word_count <= 3:
            s += 12.0
        elif tag == "input" and prompt_word_count <= 3:
            s += 8.0

        # Penalise long link text — a 10-word link shouldn't beat a 1-word button
        if tag == "a" and len(text_l.split()) > prompt_word_count * 3:
            s -= min(20.0, (len(text_l.split()) - prompt_word_count * 3) * 2.0)

        try:
            item.score = s
        except Exception:
            pass
        scored.append((s, item))

    scored.sort(key=lambda x: x[0], reverse=True)

    if top_k == 1:
        return scored[0][1] if scored else None
    return [item for _, item in scored[:top_k]]


def find_element_multi(prompt: str, snapshot) -> Optional[PageItem]:
    all_items = snapshot.buttons + snapshot.links + snapshot.inputs
    result = find_element(prompt, all_items, top_k=1)
    return result if isinstance(result, PageItem) else None
