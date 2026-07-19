from abc import ABC, abstractmethod

from storage.models import Signal


class Strategy(ABC):
    """Base class for a signal source.

    Implementations are pure: they read pre-computed indicators and return
    signals. They never place orders — execution and risk live elsewhere.
    """

    name: str
    weight: float

    @abstractmethod
    async def generate_signals(
        self, universe: list[str], tech_data: dict, **kwargs
    ) -> list[Signal]:
        pass
