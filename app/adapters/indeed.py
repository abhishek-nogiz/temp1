import re
from bs4 import BeautifulSoup
from ..schemas import JobDetail
from .base import BaseAdapter


class IndeedAdapter(BaseAdapter):
    @property
    def domain_patterns(self) -> list[str]:
        return ["indeed.com", "indeed.co"]

    def detect_items(self, page):
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        items = []

        selectors = [
            "div.job_seen_beacon",
            "div.slider_container",
            "div[data-jk]",
            "div.jobTitle-color-purple",
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
                    if href.startswith("/"):
                        href = "https://www.indeed.com" + href
                    items.append({
                        "index": i,
                        "text": text[:300],
                        "href": href,
                        "tag": "indeed_card",
                    })
                break

        return items

    def extract_detail(self, page) -> JobDetail:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        title = None
        title_el = soup.find("h1", class_=re.compile("jobsearch-JobInfoHeader-title")) or soup.find("h1")
        if title_el:
            title = " ".join(title_el.get_text(" ", strip=True).split())

        company = None
        company_el = soup.find("div", class_=re.compile("company")) or soup.find("a", class_=re.compile("company"))
        if company_el:
            company = " ".join(company_el.get_text(" ", strip=True).split())

        location = None
        loc_el = soup.find("div", class_=re.compile("location")) or soup.find("span", class_=re.compile("location"))
        if loc_el:
            location = " ".join(loc_el.get_text(" ", strip=True).split())

        description = None
        desc_el = soup.find("div", id="jobDescriptionText") or soup.find("div", class_=re.compile("jobsearch-JobComponent"))
        if desc_el:
            description = " ".join(desc_el.get_text(" ", strip=True).split())[:5000]

        apply_link = page.url

        salary = None
        text = soup.get_text(" ", strip=True)
        salary_match = re.search(r"(\$|€|£)\s?[\d,]+(?:\s?-\s?[\d,]+)?\s*(?:a year|per year|/year|yr)?", text, re.I)
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
        next_link = soup.find("a", attrs={"aria-label": re.compile("next", re.I)})
        if next_link and next_link.get("href"):
            href = next_link.get("href")
            if href.startswith("/"):
                href = "https://www.indeed.com" + href
            return href
        return None
