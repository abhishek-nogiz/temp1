import re
from bs4 import BeautifulSoup
from .schemas import JobDetail


SALARY_RE = re.compile(
    r"(\$|₹|€|£)\s?[\d,]+(?:\s?-\s?(\$|₹|€|£)?\s?[\d,]+)?",
    re.IGNORECASE,
)

LOCATION_HINTS = [
    "remote", "hybrid", "onsite", "on-site",
    "new york", "san francisco", "london", "berlin",
    "usa", "uk", "canada", "europe", "asia", "india",
]


def _clean_text(text: str) -> str:
    return " ".join(text.split()).strip()


def extract_job_detail(page, adapter=None) -> JobDetail:
    if adapter:
        return adapter.extract_detail(page)

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    title = None
    h1 = soup.find("h1")
    if h1:
        title = _clean_text(h1.get_text(" ", strip=True))

    text = _clean_text(soup.get_text(" ", strip=True))

    salary_match = SALARY_RE.search(text)
    salary = salary_match.group(0) if salary_match else None

    location = None
    lower_text = text.lower()
    for hint in LOCATION_HINTS:
        if hint in lower_text:
            idx = lower_text.find(hint)
            start = max(0, idx - 20)
            end = min(len(text), idx + len(hint) + 20)
            location = text[start:end].strip()
            break

    apply_link = None
    for a in soup.find_all("a", href=True):
        label = _clean_text(a.get_text(" ", strip=True)).lower()
        if "apply" in label or "submit" in label:
            apply_link = a.get("href")
            break

    company = None
    meta_candidates = []
    for tag in soup.find_all(["h2", "h3", "span", "div", "p", "meta"]):
        if tag.name == "meta" and tag.get("property") in ["og:site_name", "twitter:site"]:
            t = tag.get("content", "")
        else:
            t = _clean_text(tag.get_text(" ", strip=True))
        if 2 < len(t) < 120:
            meta_candidates.append(t)

    if title:
        for t in meta_candidates:
            if t != title and title.lower() not in t.lower():
                company = t
                break

    description = text[:5000] if text else None

    requirements = []
    benefits = []
    for ul in soup.find_all("ul"):
        heading = ul.find_previous(["h2", "h3", "h4", "strong"])
        heading_text = heading.get_text(" ", strip=True).lower() if heading else ""
        items = [_clean_text(li.get_text(" ", strip=True)) for li in ul.find_all("li")]
        if any(k in heading_text for k in ["requirement", "qualification", "skill", "experience"]):
            requirements.extend(items)
        elif any(k in heading_text for k in ["benefit", "perk", "offer", "compensation"]):
            benefits.extend(items)

    return JobDetail(
        title=title,
        company=company,
        location=location,
        salary=salary,
        description=description,
        apply_link=apply_link,
        requirements=requirements,
        benefits=benefits,
    )
