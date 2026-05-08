import re
from bs4 import BeautifulSoup
from ..schemas import JobDetail
from .base import BaseAdapter


class GreenhouseAdapter(BaseAdapter):
    @property
    def domain_patterns(self) -> list[str]:
        return ["greenhouse.io", "boards.greenhouse"]

    def detect_items(self, page):
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        items = []

        for i, opening in enumerate(soup.find_all(class_="opening")):
            a = opening.find("a", href=True)
            if not a:
                continue
            text = " ".join(opening.get_text(" ", strip=True).split())
            href = a.get("href")
            items.append({
                "index": i,
                "text": text[:300],
                "href": href,
                "tag": "greenhouse_opening",
            })

        return items

    def extract_detail(self, page) -> JobDetail:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        title = None
        h1 = soup.find("h1", class_="app-title") or soup.find("h1")
        if h1:
            title = " ".join(h1.get_text(" ", strip=True).split())

        company = None
        company_el = soup.find(class_="company-name") or soup.find("a", class_="company")
        if company_el:
            company = " ".join(company_el.get_text(" ", strip=True).split())

        location = None
        loc_el = soup.find(class_="location") or soup.find("div", class_=re.compile("location"))
        if loc_el:
            location = " ".join(loc_el.get_text(" ", strip=True).split())

        description = None
        desc_el = soup.find(id="content") or soup.find(class_="job-description")
        if desc_el:
            description = " ".join(desc_el.get_text(" ", strip=True).split())[:5000]

        apply_link = None
        for a in soup.find_all("a", href=True):
            if "apply" in a.get_text(" ", strip=True).lower():
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
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        next_link = soup.find("a", text=re.compile("next", re.I))
        if next_link and next_link.get("href"):
            return next_link.get("href")
        return None
