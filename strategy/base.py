from abc import ABC, abstractmethod

from storage.models import Signal


class Strategy(ABC):
    name: str
    weight: float

    @abstractmethod
    async def generate_signals(self, universe: list[str], tech_data: dict,
                               **kwargs) -> list[Signal]:
        pass
