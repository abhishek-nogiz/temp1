import re
from bs4 import BeautifulSoup
from ..schemas import JobDetail
from .base import BaseAdapter


class LeverAdapter(BaseAdapter):
    @property
    def domain_patterns(self) -> list[str]:
        return ["lever.co", "jobs.lever"]

    def detect_items(self, page):
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        items = []

        for i, posting in enumerate(soup.find_all(class_="posting")):
            a = posting.find("a", href=True)
            if not a:
                continue
            text = " ".join(posting.get_text(" ", strip=True).split())
            href = a.get("href")
            items.append({
                "index": i,
                "text": text[:300],
                "href": href,
                "tag": "lever_posting",
            })

        return items

    def extract_detail(self, page) -> JobDetail:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        title = None
        h1 = soup.find("h1", class_=re.compile("posting")) or soup.find("h1")
        if h1:
            title = " ".join(h1.get_text(" ", strip=True).split())

        company = None
        meta = soup.find("meta", property="og:site_name")
        if meta:
            company = meta.get("content")

        location = None
        loc_el = soup.find(class_=re.compile("location")) or soup.find("div", class_="sort-by-team")
        if loc_el:
            location = " ".join(loc_el.get_text(" ", strip=True).split())

        description = None
        desc_el = soup.find(class_=re.compile("content")) or soup.find("div", class_="section")
        if desc_el:
            description = " ".join(desc_el.get_text(" ", strip=True).split())[:5000]

        apply_link = None
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True).lower()
            if "apply" in text or "application" in text:
                apply_link = a.get("href")
                break

        salary = None
        text = soup.get_text(" ", strip=True)
        salary_match = re.search(r"(\$|€|£)\s?[\d,]+(?:\s?-\s?[\d,]+)?", text)
        if salary_match:
            salary = salary_match.group(0)

        requirements = []
        benefits = []
        for ul in soup.find_all("ul"):
            heading = ul.find_previous(["h2", "h3", "h4", "strong"])
            heading_text = heading.get_text(" ", strip=True).lower() if heading else ""
            items = [" ".join(li.get_text(" ", strip=True).split()) for li in ul.find_all("li")]
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

    def paginate(self, page):
        return None
