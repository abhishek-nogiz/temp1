from __future__ import annotations

from bs4 import BeautifulSoup, Tag
from .schemas import PageSnapshot, PageItem, RepeatedBlock


ANNOTATION_ATTR = "data-tinyfish-id"


def _clean_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _get_tag_path(tag: Tag, max_depth: int = 6) -> str:
    path = []
    current = tag
    depth = 0
    while current and depth < max_depth:
        if isinstance(current, Tag):
            tag_name = current.name
            classes = current.get("class", [])
            if classes:
                tag_name += "." + ".".join(classes[:2])
            path.append(tag_name)
        current = current.parent
        depth += 1
    return " > ".join(reversed(path))


def annotate_interactive_elements(page) -> None:
    """Attach stable temporary ids to interactive DOM nodes for this browser session."""
    try:
        page.evaluate(
            """
            () => {
                const selector = [
                    'a[href]', 'button', 'input', 'textarea', 'select',
                    '[role="button"]', '[role="link"]', '[contenteditable="true"]',
                    '[tabindex]:not([tabindex="-1"])'
                ].join(',');
                const nodes = Array.from(document.querySelectorAll(selector))
                    .filter((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return style.visibility !== 'hidden' && style.display !== 'none' &&
                               rect.width >= 1 && rect.height >= 1;
                    });
                nodes.forEach((el, i) => {
                    if (!el.getAttribute('data-tinyfish-id')) {
                        el.setAttribute('data-tinyfish-id', `tf-${i}`);
                    }
                });
            }
            """
        )
    except Exception:
        # Snapshot building should never fail only because annotation failed.
        return


def _selector_for(tag: Tag) -> str | None:
    tfid = tag.get(ANNOTATION_ATTR)
    if tfid:
        safe = str(tfid).replace('"', '\\"')
        return f'[{ANNOTATION_ATTR}="{safe}"]'
    if tag.get("id"):
        safe_id = str(tag.get("id")).replace('"', '\\"')
        return f'[id="{safe_id}"]'
    name = tag.get("name")
    if name and tag.name in {"input", "textarea", "select"}:
        safe_name = str(name).replace('"', '\\"')
        return f'{tag.name}[name="{safe_name}"]'
    return None


def _element_text(tag: Tag) -> str:
    pieces = [
        tag.get("aria-label"),
        tag.get("title"),
        tag.get("placeholder"),
        tag.get("name"),
        tag.get("value") if tag.name in {"input", "textarea", "select"} else None,
        tag.get_text(" ", strip=True),
    ]
    return _clean_text(" ".join(str(p) for p in pieces if p))


def build_snapshot(page) -> PageSnapshot:
    annotate_interactive_elements(page)
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    title = page.title()
    body_text = _clean_text(soup.get_text(" ", strip=True))

    links = []
    seen_links = set()
    for a in soup.find_all("a"):
        text = _element_text(a)
        href = a.get("href")
        if text or href:
            key = (text, href, _selector_for(a))
            if key in seen_links:
                continue
            seen_links.add(key)
            links.append(
                PageItem(
                    index=len(links),
                    text=(text or href or "")[:300],
                    href=href,
                    tag="a",
                    selector=_selector_for(a),
                )
            )

    buttons = []
    seen_buttons = set()
    for b in soup.find_all(["button"]):
        text = (_element_text(b)
                or str(b.get("aria-label") or "")
                or str(b.get("title") or "")
                or str(b.get("name") or "")
                or str(b.get("value") or "")).strip()
        if text:
            key = (text, _selector_for(b))
            if key in seen_buttons:
                continue
            seen_buttons.add(key)
            buttons.append(
                PageItem(
                    index=len(buttons),
                    text=text[:300],
                    tag="button",
                    selector=_selector_for(b),
                )
            )

    # Add non-button elements that behave as buttons.
    for b in soup.find_all(attrs={"role": "button"}):
        text = _element_text(b)
        if text:
            key = (text, _selector_for(b))
            if key in seen_buttons:
                continue
            seen_buttons.add(key)
            buttons.append(
                PageItem(
                    index=len(buttons),
                    text=text[:300],
                    tag=b.name or "button",
                    selector=_selector_for(b),
                )
            )

    inputs = []
    seen_inputs = set()
    for inp in soup.find_all(["input", "textarea", "select"]):
        input_type = str(inp.get("type") or "").lower()
        if input_type in {"hidden", "image"}:
            continue
        # Treat submit/button typed inputs as clickable buttons, not form fields
        if input_type in {"submit", "button"}:
            text = _element_text(inp) or input_type
            key = (text, _selector_for(inp))
            if key not in seen_buttons:
                seen_buttons.add(key)
                buttons.append(
                    PageItem(
                        index=len(buttons),
                        text=text[:300],
                        tag="button",
                        selector=_selector_for(inp),
                    )
                )
            continue
        text = _element_text(inp) or input_type or inp.name
        key = (text, _selector_for(inp))
        if key in seen_inputs:
            continue
        seen_inputs.add(key)
        inputs.append(
            PageItem(
                index=len(inputs),
                text=text[:300],
                tag=inp.name,
                selector=_selector_for(inp),
            )
        )

    return PageSnapshot(
        url=page.url,
        title=title,
        text=body_text[:20000],
        links=links[:300],
        buttons=buttons[:200],
        inputs=inputs[:100],
    )


def detect_repeated_blocks(page, min_repeats: int = 3, limit: int = 20) -> list[RepeatedBlock]:
    annotate_interactive_elements(page)
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    candidates = []
    for tag in soup.find_all([
        "div", "li", "article", "section", "tr", "card", "job", "listing", "item"
    ]):
        path = _get_tag_path(tag)
        text = _clean_text(tag.get_text(" ", strip=True))
        if len(text) < 10:
            continue
        children = len(list(tag.children))
        candidates.append({
            "tag": tag,
            "path": path,
            "text": text,
            "children": children,
            "html": str(tag)[:500],
        })

    path_groups = {}
    for c in candidates:
        key = c["path"]
        if key not in path_groups:
            path_groups[key] = []
        path_groups[key].append(c)

    scored_blocks = []
    for path, group in path_groups.items():
        if len(group) < min_repeats:
            continue

        for i, item in enumerate(group[:limit]):
            score = 0.0
            score += len(group) * 2
            if item["tag"].find("a", href=True):
                score += 15
            if item["tag"].find(["h1", "h2", "h3", "h4", "h5"]):
                score += 10
            text_len = len(item["text"])
            if 30 < text_len < 500:
                score += 5
            if item["children"] > 2:
                score += 3

            href = None
            a_tag = item["tag"].find("a", href=True)
            if a_tag:
                href = a_tag.get("href")

            scored_blocks.append(RepeatedBlock(
                index=i,
                html=item["html"],
                text=item["text"][:300],
                children_count=item["children"],
                tag_path=path,
                href=href,
                score=score,
            ))

    scored_blocks.sort(key=lambda x: x.score, reverse=True)
    return scored_blocks[:limit]
