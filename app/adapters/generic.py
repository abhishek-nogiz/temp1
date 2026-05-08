from ..schemas import JobDetail
from ..dom import detect_repeated_blocks
from ..extractors import extract_job_detail
from .base import BaseAdapter


class GenericAdapter(BaseAdapter):
    @property
    def domain_patterns(self) -> list[str]:
        return ["*"]

    def matches(self, url: str) -> bool:
        return True

    def detect_items(self, page):
        return detect_repeated_blocks(page, min_repeats=2, limit=20)

    def extract_detail(self, page) -> JobDetail:
        return extract_job_detail(page, adapter=None)
