from bs4 import BeautifulSoup
from .schemas import PageItem
from .dom import detect_repeated_blocks


def _clean_text(text: str) -> str:
    return " ".join(text.split()).strip()


def detect_candidate_items(page, use_repeated_blocks: bool = True, limit: int = 20) -> list[PageItem]:
    if use_repeated_blocks:
        blocks = detect_repeated_blocks(page, min_repeats=2, limit=limit)
        if blocks:
            return [
                PageItem(
                    index=b.index,
                    text=b.text[:300],
                    href=b.href,
                    tag="repeated_block",
                    score=b.score,
                )
                for b in blocks
            ]

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    candidates = []
    seen = set()

    for a in soup.find_all("a", href=True):
        text = _clean_text(a.get_text(" ", strip=True))
        href = a.get("href")
        if not text or len(text) < 8:
            continue
        key = (text, href)
        if key in seen:
            continue
        seen.add(key)

        candidates.append(
            PageItem(
                index=len(candidates),
                text=text[:300],
                href=href,
                tag="a",
            )
        )

        if len(candidates) >= limit:
            break

    return candidates
