"""Microsoft Foundry Agent Service motoru (Azure AI Search aracı ile).

Bu motorda arama ve atıf üretimi Foundry tarafında yapılır: agent'a bir
`AzureAISearchTool` bağlanır, agent kendi thread'inde geçmişi tutar ve yanıtta
`url_citation` ek açıklamalarını döndürür.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import Settings
from ..models import AnswerResult, SourceDoc, parse_cited_ordinals
from ..prompts import FOUNDRY_INSTRUCTIONS
from ..retrieval import apply_sharepoint_links
from .base import AskOptions, Engine

logger = logging.getLogger(__name__)

# Foundry atıf yer tutucuları: 【3:0†source】
ANNOTATION_MARKER_RE = re.compile(r"【[^】]*】")
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")

QUERY_TYPES = {
    "simple": "SIMPLE",
    "semantic": "SEMANTIC",
    "vector": "VECTOR",
    "vector_simple_hybrid": "VECTOR_SIMPLE_HYBRID",
    "vector_semantic_hybrid": "VECTOR_SEMANTIC_HYBRID",
}


class FoundryEngine(Engine):
    name = "foundry"
    supports_streaming = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self._project = None
        self._agent_id: Optional[str] = settings.foundry_agent_id
        self._thread_id: Optional[str] = None
        self._agent_owned = False

    # ------------------------------------------------------------------
    # Bağlantı / agent yaşam döngüsü
    # ------------------------------------------------------------------
    def _client(self):
        if self._project is None:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            self._project = AIProjectClient(
                endpoint=self.settings.foundry_endpoint,
                credential=DefaultAzureCredential(exclude_interactive_browser_credential=False),
            )
        return self._project

    def _search_tool(self, top_k: int):
        from azure.ai.agents.models import AzureAISearchQueryType, AzureAISearchTool

        settings = self.settings
        project = self._client()
        connection = project.connections.get(name=settings.foundry_search_connection)

        enum_name = QUERY_TYPES.get(settings.foundry_query_type, "VECTOR_SEMANTIC_HYBRID")
        query_type = getattr(AzureAISearchQueryType, enum_name, AzureAISearchQueryType.SIMPLE)

        return AzureAISearchTool(
            index_connection_id=connection.id,
            index_name=settings.search_index,
            query_type=query_type,
            top_k=top_k,
        )

    def _find_existing_agent(self) -> Optional[str]:
        try:
            for agent in self._client().agents.list_agents():
                if getattr(agent, "name", None) == self.settings.foundry_agent_name:
                    return agent.id
        except Exception as exc:
            logger.warning("Mevcut agent listelenemedi: %s", exc)
        return None

    def ensure_agent(self, top_k: Optional[int] = None) -> str:
        if self._agent_id:
            return self._agent_id

        existing = self._find_existing_agent()
        if existing:
            self._agent_id = existing
            return existing

        tool = self._search_tool(top_k or self.settings.top_k)
        agent = self._client().agents.create_agent(
            model=self.settings.foundry_model,
            name=self.settings.foundry_agent_name,
            instructions=FOUNDRY_INSTRUCTIONS,
            tools=tool.definitions,
            tool_resources=tool.resources,
        )
        self._agent_id = agent.id
        self._agent_owned = True
        logger.info("Foundry agent oluşturuldu: %s", agent.id)
        return agent.id

    def ensure_thread(self) -> str:
        if not self._thread_id:
            thread = self._client().agents.threads.create()
            self._thread_id = thread.id
        return self._thread_id

    def reset_thread(self) -> None:
        """Sohbeti temizler; agent tarafındaki geçmişi bırakır."""
        self._thread_id = None

    @property
    def thread_id(self) -> Optional[str]:
        return self._thread_id

    def set_thread(self, thread_id: Optional[str]) -> None:
        """Geçmişten bir sohbet açıldığında o sohbetin thread'ine geri döner."""
        self._thread_id = thread_id or None

    def health(self) -> Dict[str, str]:
        return {
            "Motor": "Microsoft Foundry Agent Service",
            "Proje": self.settings.foundry_endpoint,
            "Model": self.settings.foundry_model,
            "Search bağlantısı": self.settings.foundry_search_connection or "-",
            "İndeks": self.settings.search_index,
            "Sorgu tipi": self.settings.foundry_query_type,
            "Agent": self._agent_id or "(ilk soruda oluşturulacak)",
            "Thread": self._thread_id or "(ilk soruda oluşturulacak)",
        }

    def close(self) -> None:
        if self._project is not None:
            try:
                self._project.close()
            except Exception:  # pragma: no cover
                pass
            self._project = None

    def delete_agent(self) -> None:
        if self._agent_id and self._agent_owned:
            try:
                self._client().agents.delete_agent(self._agent_id)
            except Exception as exc:
                logger.warning("Agent silinemedi: %s", exc)
        self._agent_id = None

    # ------------------------------------------------------------------
    # Yanıt üretimi
    # ------------------------------------------------------------------
    def ask(
        self,
        question: str,
        history: Optional[Sequence[dict]] = None,
        options: Optional[AskOptions] = None,
    ) -> AnswerResult:
        # Geçmiş Foundry thread'inde tutulduğu için history parametresi kullanılmaz.
        options = options or AskOptions.from_settings(self.settings)
        started = time.perf_counter()
        debug: Dict[str, Any] = {"kullanıcı sorusu": question}

        try:
            from azure.ai.agents.models import ListSortOrder

            project = self._client()
            agent_id = self.ensure_agent(options.top_k)
            thread_id = self.ensure_thread()
            debug["agent"] = agent_id
            debug["thread"] = thread_id

            project.agents.messages.create(thread_id=thread_id, role="user", content=question)
            run = project.agents.runs.create_and_process(thread_id=thread_id, agent_id=agent_id)

            status = str(getattr(run.status, "value", run.status) or "").lower()
            debug["run"] = getattr(run, "id", "-")
            debug["durum"] = status

            if status == "failed":
                error = getattr(run, "last_error", None)
                message = getattr(error, "message", None) or str(error) or "bilinmeyen hata"
                return AnswerResult(
                    answer="",
                    backend=self.name,
                    error=f"Foundry run başarısız: {message}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    debug=debug,
                )

            text, sources = self._read_answer(project, thread_id, run, ListSortOrder)
            debug.update(self._tool_debug(project, thread_id, run))

        except Exception as exc:
            logger.exception("Foundry motorunda hata")
            return AnswerResult(
                answer="",
                backend=self.name,
                error=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
                debug=debug,
            )

        debug["referans sayısı"] = len(sources)
        return AnswerResult(
            answer=text,
            sources=sources,
            cited_ordinals=parse_cited_ordinals(text),
            backend=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
            debug=debug,
        )

    # ------------------------------------------------------------------
    def _read_answer(self, project, thread_id: str, run, ListSortOrder) -> Tuple[str, List[SourceDoc]]:
        message = self._latest_assistant_message(project, thread_id, run, ListSortOrder)
        if message is None:
            return "", []

        text_parts: List[str] = []
        annotations: List[Any] = []

        contents = getattr(message, "text_messages", None)
        if not contents:
            contents = [
                c for c in (getattr(message, "content", None) or []) if hasattr(c, "text")
            ]

        for content in contents:
            details = getattr(content, "text", None)
            if details is None:
                continue
            value = getattr(details, "value", None) or str(details)
            text_parts.append(value)
            annotations.extend(getattr(details, "annotations", None) or [])

        text = "\n".join(part for part in text_parts if part)
        text, sources = self._apply_annotations(text, annotations)

        if not sources:
            sources = self._sources_from_run_steps(project, thread_id, run)
        return text, sources

    def _latest_assistant_message(self, project, thread_id: str, run, ListSortOrder):
        for kwargs in (
            {"run_id": getattr(run, "id", None), "order": ListSortOrder.DESCENDING},
            {"order": ListSortOrder.DESCENDING, "limit": 10},
        ):
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            try:
                for message in project.agents.messages.list(thread_id=thread_id, **kwargs):
                    role = str(getattr(message.role, "value", message.role) or "")
                    if role.lower() in {"assistant", "messagerole.agent", "agent"}:
                        return message
            except Exception as exc:
                logger.warning("Mesajlar okunamadı (%s): %s", kwargs, exc)
        return None

    def _apply_annotations(self, text: str, annotations: Sequence[Any]) -> Tuple[str, List[SourceDoc]]:
        """url_citation ek açıklamalarını [1], [2] atıflarına ve kaynak listesine çevirir."""
        by_url: Dict[str, SourceDoc] = {}
        replacements: List[Tuple[str, int]] = []

        for annotation in annotations:
            citation = getattr(annotation, "url_citation", None)
            if citation is None:
                continue
            url = (getattr(citation, "url", None) or "").strip()
            title = (getattr(citation, "title", None) or "").strip()
            key = url or title
            if not key:
                continue

            doc = by_url.get(key)
            if doc is None:
                doc = apply_sharepoint_links(
                    SourceDoc(
                        ordinal=len(by_url) + 1,
                        doc_id=key,
                        title=title or url,
                        content="",
                        url=url or None,
                        snippet="",
                    ),
                    self.settings,
                )
                by_url[key] = doc

            marker = getattr(annotation, "text", None)
            if marker:
                replacements.append((marker, doc.ordinal))

        for marker, ordinal in replacements:
            text = text.replace(marker, f"[{ordinal}]")
        # Eşleşmeyen yer tutucuları temizle.
        text = ANNOTATION_MARKER_RE.sub("", text)

        return text.strip(), sorted(by_url.values(), key=lambda d: d.ordinal)

    def _sources_from_run_steps(self, project, thread_id: str, run) -> List[SourceDoc]:
        """Model atıf üretmediyse arama aracının çıktısından kaynak çıkarmayı dener."""
        sources: List[SourceDoc] = []
        seen = set()
        try:
            for step in project.agents.run_steps.list(thread_id=thread_id, run_id=run.id):
                details = getattr(step, "step_details", None)
                for call in getattr(details, "tool_calls", None) or []:
                    payload = getattr(call, "azure_ai_search", None)
                    if not payload:
                        continue
                    output = str(payload.get("output") if isinstance(payload, dict) else payload)
                    for url in URL_RE.findall(output):
                        url = url.rstrip(".,);")
                        if url in seen:
                            continue
                        seen.add(url)
                        sources.append(
                            apply_sharepoint_links(
                                SourceDoc(
                                    ordinal=len(sources) + 1,
                                    doc_id=url,
                                    title=url.rsplit("/", 1)[-1] or url,
                                    content="",
                                    url=url,
                                    snippet="",
                                ),
                                self.settings,
                            )
                        )
        except Exception as exc:
            logger.warning("Run adımları okunamadı: %s", exc)
        return sources

    def _tool_debug(self, project, thread_id: str, run) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        try:
            usage = getattr(run, "usage", None)
            if usage is not None:
                info["token kullanımı"] = getattr(usage, "total_tokens", None) or str(usage)
            calls = 0
            for step in project.agents.run_steps.list(thread_id=thread_id, run_id=run.id):
                details = getattr(step, "step_details", None)
                for call in getattr(details, "tool_calls", None) or []:
                    if getattr(call, "azure_ai_search", None):
                        calls += 1
            info["arama aracı çağrısı"] = calls
        except Exception:
            pass
        return info
