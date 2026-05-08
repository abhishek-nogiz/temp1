from abc import ABC, abstractmethod
from typing import Optional
from ..schemas import JobDetail


class BaseAdapter(ABC):
    @property
    @abstractmethod
    def domain_patterns(self) -> list[str]:
        pass

    def matches(self, url: str) -> bool:
        return any(pattern in url.lower() for pattern in self.domain_patterns)

    @abstractmethod
    def detect_items(self, page) -> list:
        pass

    @abstractmethod
    def extract_detail(self, page) -> JobDetail:
        pass

    def paginate(self, page) -> Optional[str]:
        return None
