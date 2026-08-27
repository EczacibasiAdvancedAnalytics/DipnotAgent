"""Azure AI Search + Azure OpenAI ile doğrudan RAG motoru.

Atıfların doğruluğu üzerinde tam kontrol sağladığı için varsayılan motordur:
dokümanları biz getirir, numaralandırır ve modele bu numaralarla veririz.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from ..config import Settings
from ..llm import LlmClient
from ..models import AnswerResult, SourceDoc, parse_cited_ordinals
from ..prompts import NO_RESULTS_MESSAGE, SYSTEM_PROMPT, build_user_message
from ..retrieval import Retriever, enrich_web_sources
from ..schema import IndexSchema, discover_schema
from .base import AskOptions, Engine

logger = logging.getLogger(__name__)

REWRITE_SYSTEM = (
    "Kullanıcının son mesajını, sohbet geçmişini dikkate alarak tek başına anlaşılır bir "
    "arama sorgusuna dönüştür. Zamirleri ve eksik özneleri geçmişten tamamla. "
    "Yalnızca arama sorgusunu yaz; açıklama, tırnak veya ön ek ekleme. "
    "Son mesaj zaten bağımsızsa aynen tekrar et."
)


class DirectEngine(Engine):
    name = "direct"
    supports_streaming = True

    def __init__(self, settings: Settings, schema: Optional[IndexSchema] = None):
        self.settings = settings
        self.schema = schema or discover_schema(settings)
        self.llm = LlmClient(settings)
        self.retriever = Retriever(settings, self.schema, embedder=self.llm.embedder())

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.retriever.close()

    def health(self) -> Dict[str, str]:
        return {
            "Motor": "Azure AI Search + Azure OpenAI (direct)",
            "İndeks": self.settings.search_index,
            "Sohbet modeli": self.settings.aoai_chat_deployment,
            "Arama kimlik doğrulama": "Entra ID" if self.settings.search_uses_entra else "API anahtarı",
            "OpenAI kimlik doğrulama": "Entra ID" if self.settings.aoai_uses_entra else "API anahtarı",
            "Semantik sıralama": self.schema.semantic_config or "kapalı",
            "Vektör arama": ", ".join(self.schema.vector_fields) or "kapalı",
            "Model parametre biçimi": self.llm.param_mode,
        }

    # ------------------------------------------------------------------
    def _history_messages(self, history: Optional[Sequence[dict]]) -> List[dict]:
        if not history:
            return []
        turns = max(self.settings.history_turns, 0) * 2
        trimmed = list(history)[-turns:] if turns else []
        return [
            {"role": m["role"], "content": m["content"]}
            for m in trimmed
            if m.get("role") in {"user", "assistant"} and m.get("content")
        ]

    def _search_query(self, question: str, history: Optional[Sequence[dict]]) -> Tuple[str, bool]:
        """Takip sorularını bağımsız arama sorgusuna çevirir."""
        if not self.settings.rewrite_query or not history:
            return question, False
        try:
            rewritten = self.llm.chat(
                [
                    {"role": "system", "content": REWRITE_SYSTEM},
                    *self._history_messages(history),
                    {"role": "user", "content": question},
                ],
                temperature=0.0,
                max_tokens=120,
            ).strip()
        except Exception as exc:
            logger.warning("Sorgu yeniden yazma başarısız: %s", exc)
            return question, False

        rewritten = rewritten.strip().strip('"').strip()
        if not rewritten or len(rewritten) > 400:
            return question, False
        return rewritten, rewritten.lower() != question.strip().lower()

    def retrieve(
        self, question: str, history=None, options: Optional[AskOptions] = None
    ) -> Tuple[List[SourceDoc], Dict]:
        options = options or AskOptions.from_settings(self.settings)
        query, rewritten = self._search_query(question, history)
        sources, debug = self.retriever.search(
            query,
            top_k=options.top_k,
            odata_filter=options.odata_filter,
            use_semantic=options.use_semantic,
            use_vector=options.use_vector,
            min_reranker_score=options.min_reranker_score,
            merge_chunks=options.merge_chunks,
        )
        try:
            sources, web_debug = enrich_web_sources(sources, self.settings)
            debug.update(web_debug)
        except Exception as exc:
            logger.warning("Web içerik ekleme atlandı: %s", exc)
        debug["kullanıcı sorusu"] = question
        debug["yeniden yazıldı"] = "evet" if rewritten else "hayır"
        return sources, debug

    # ------------------------------------------------------------------
    def start_stream(
        self,
        question: str,
        history: Optional[Sequence[dict]] = None,
        options: Optional[AskOptions] = None,
    ) -> Tuple[Iterator[str], List[SourceDoc], Dict]:
        options = options or AskOptions.from_settings(self.settings)
        sources, debug = self.retrieve(question, history, options)

        if not sources:
            return iter([NO_RESULTS_MESSAGE]), [], debug

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self._history_messages(history),
            {"role": "user", "content": build_user_message(question, sources)},
        ]
        debug["modele giden karakter"] = sum(len(m["content"]) for m in messages)

        stream = self.llm.stream_chat(messages, temperature=options.temperature)
        return stream, sources, debug

    def ask(
        self,
        question: str,
        history: Optional[Sequence[dict]] = None,
        options: Optional[AskOptions] = None,
    ) -> AnswerResult:
        started = time.perf_counter()
        try:
            stream, sources, debug = self.start_stream(question, history, options)
            answer = "".join(stream)
        except Exception as exc:
            logger.exception("Direct motorunda hata")
            return AnswerResult(
                answer="",
                backend=self.name,
                error=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        return AnswerResult(
            answer=answer,
            sources=sources,
            cited_ordinals=parse_cited_ordinals(answer),
            backend=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            debug=debug,
        )
