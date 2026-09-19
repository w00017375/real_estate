from abc import ABC, abstractmethod
from typing import Any

from core.existing_listings import ExistingListings


class ListingSource(ABC):
    """Единый контракт для адаптеров сайтов с объявлениями."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def start_url(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def collect(
        self,
        limit: int | None,
        existing: ExistingListings | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает полностью открытые объявления; None означает весь каталог."""
        raise NotImplementedError
