"""Soru-cevap motorları ve fabrika fonksiyonu."""

from __future__ import annotations

from typing import Optional

from ..config import Settings
from .base import AskOptions, Engine

__all__ = ["AskOptions", "Engine", "build_engine", "DirectEngine", "FoundryEngine"]


def build_engine(settings: Settings, backend: Optional[str] = None) -> Engine:
    backend = (backend or settings.backend).lower()
    if backend == "foundry":
        from .foundry import FoundryEngine

        return FoundryEngine(settings)

    from .direct import DirectEngine

    return DirectEngine(settings)


def __getattr__(name: str):
    if name == "DirectEngine":
        from .direct import DirectEngine

        return DirectEngine
    if name == "FoundryEngine":
        from .foundry import FoundryEngine

        return FoundryEngine
    raise AttributeError(name)
