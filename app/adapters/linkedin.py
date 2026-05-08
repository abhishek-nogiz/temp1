import re
from bs4 import BeautifulSoup
from ..schemas import JobDetail
from .base import BaseAdapter


class LinkedInAdapter(BaseAdapter):
    @property
    def domain_patterns(self) -> list[str]:
        return ["linkedin.com/jobs"]

    def detect_items(self, page):
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        items = []

        selectors = [
            "div.job-card-container",
            "div.base-card",
            "li.jobs-search-results__list-item",
            "div.job-search-card",
        ]

        for selector in selectors:
            cards = soup.select(selector)
            if cards:
                for i, card in enumerate(cards):
                    a = card.find("a", href=True)
                    if not a:
                        continue
                    text = " ".join(card.get_text(" ", strip=True).split())
                    href = a.get("href")
                    items.append({
                        "index": i,
                        "text": text[:300],
                        "href": href,
                        "tag": "linkedin_card",
                    })
                break

        return items

    def extract_detail(self, page) -> JobDetail:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        title = None
        title_el = soup.find("h1", class_=re.compile("job-title|top-card")) or soup.find("h1")
        if title_el:
            title = " ".join(title_el.get_text(" ", strip=True).split())

        company = None
        company_el = soup.find("a", class_=re.compile("company")) or soup.find("span", class_=re.compile("company"))
        if company_el:
            company = " ".join(company_el.get_text(" ", strip=True).split())

        location = None
        loc_el = soup.find("span", class_=re.compile("location")) or soup.find("div", class_=re.compile("location"))
        if loc_el:
            location = " ".join(loc_el.get_text(" ", strip=True).split())

        description = None
        desc_el = soup.find("div", class_=re.compile("description")) or soup.find("div", id="job-details")
        if desc_el:
            description = " ".join(desc_el.get_text(" ", strip=True).split())[:5000]

        apply_link = page.url

        salary = None
        text = soup.get_text(" ", strip=True)
        salary_match = re.search(r"(\$|€|£)\s?[\d,]+(?:\s?K?\s?-\s?[\d,]+K?)?", text)
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
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        next_btn = soup.find("button", text=re.compile("next", re.I))
        if next_btn:
            return "__JS_PAGINATION__"
        return None
