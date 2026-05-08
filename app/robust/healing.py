"""
Selector healing: when a selector fails, try to find the element
by alternative means (text, LLM, structural similarity).
"""
from typing import Optional
from bs4 import BeautifulSoup


class SelectorHealer:
    """
    Heals broken selectors by:
    1. Finding similar elements by text content
    2. Using structural similarity
    3. LLM-generated alternative selectors
    """

    def __init__(self, llm_matcher=None):
        self.llm = llm_matcher
        self._selector_history = {}

    def heal(self, page, failed_selector: str, element_description: str = "") -> Optional[str]:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        healed = self._heal_by_class(soup, failed_selector)
        if healed:
            return healed

        healed = self._heal_by_text(page, failed_selector)
        if healed:
            return healed

        if self.llm and element_description:
            healed = self.llm.generate_selector(element_description, html)
            if healed:
                return healed

        return None

    def _heal_by_class(self, soup, selector: str) -> Optional[str]:
        import re
        classes = re.findall(r'\.([a-zA-Z0-9_-]+)', selector)
        if not classes:
            return None

        for cls in classes:
            elements = soup.find_all(class_=re.compile(cls, re.I))
            if elements:
                el = elements[0]
                if el.get("id"):
                    return f"#{el.get('id')}"
                if el.get("class"):
                    stable_class = [c for c in el.get("class") if len(c) > 5][0] if el.get("class") else None
                    if stable_class:
                        return f".{stable_class}"
                if el.name:
                    return el.name

        return None

    def _heal_by_text(self, page, selector: str) -> Optional[str]:
        try:
            common_labels = ["apply", "search", "submit", "next", "login"]
            for label in common_labels:
                if label in selector.lower():
                    locator = page.get_by_text(label, exact=False)
                    if locator.count() > 0:
                        return f"text={label}"
        except Exception:
            pass
        return None

    def record_healing(self, url: str, original: str, healed: str):
        if url not in self._selector_history:
            self._selector_history[url] = {}
        self._selector_history[url][original] = healed

    def get_healed(self, url: str, original: str) -> Optional[str]:
        return self._selector_history.get(url, {}).get(original)
