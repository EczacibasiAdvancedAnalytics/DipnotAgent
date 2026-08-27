"""Motorlar için ortak arayüz ve seçenekler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from ..config import Settings
from ..models import AnswerResult, SourceDoc


@dataclass
class AskOptions:
    """Arayüzden gelen, tek bir soru için geçerli ayarlar."""

    top_k: Optional[int] = None
    use_semantic: bool = True
    use_vector: bool = True
    min_reranker_score: Optional[float] = None
    merge_chunks: Optional[bool] = None
    odata_filter: Optional[str] = None
    temperature: Optional[float] = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "AskOptions":
        return cls(
            top_k=settings.top_k,
            min_reranker_score=settings.min_reranker_score,
            merge_chunks=settings.merge_chunks,
            temperature=settings.temperature,
        )


class Engine:
    """Tüm motorların uyduğu temel sözleşme."""

    name: str = "engine"
    supports_streaming: bool = False

    def ask(
        self,
        question: str,
        history: Optional[Sequence[dict]] = None,
        options: Optional[AskOptions] = None,
    ) -> AnswerResult:
        raise NotImplementedError

    def start_stream(
        self,
        question: str,
        history: Optional[Sequence[dict]] = None,
        options: Optional[AskOptions] = None,
    ) -> Tuple[Iterator[str], List[SourceDoc], Dict]:
        """Kaynakları hemen, cevabı akış halinde döndürür."""
        raise NotImplementedError

    def health(self) -> Dict[str, str]:
        return {}

    def close(self) -> None:
        pass
