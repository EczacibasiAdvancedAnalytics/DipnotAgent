"""Azure AI Search üzerinden doküman getirme (hibrit + semantik arama)."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import (
    QueryAnswerType,
    QueryCaptionType,
    QueryType,
    VectorizableTextQuery,
    VectorizedQuery,
)

from .config import Settings
from .models import SourceDoc
from .schema import IndexSchema

logger = logging.getLogger(__name__)

MIME_LABELS = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/msword": "Word",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "application/vnd.ms-excel": "Excel",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
    "application/vnd.ms-powerpoint": "PowerPoint",
    "text/plain": "Metin",
    "text/html": "HTML",
    "application/json": "JSON",
    "message/rfc822": "E-posta",
}

EXT_LABELS = {
    ".pdf": "PDF",
    ".docx": "Word",
    ".doc": "Word",
    ".xlsx": "Excel",
    ".xls": "Excel",
    ".pptx": "PowerPoint",
    ".ppt": "PowerPoint",
    ".txt": "Metin",
    ".md": "Markdown",
    ".csv": "CSV",
    ".msg": "E-posta",
    ".html": "HTML",
    ".htm": "HTML",
}


def _friendly_file_type(raw: Optional[str], title: Optional[str]) -> Optional[str]:
    if raw:
        value = str(raw).strip().lower()
        if value in MIME_LABELS:
            return MIME_LABELS[value]
        if value.startswith("."):
            return EXT_LABELS.get(value, value.lstrip(".").upper())
        if "/" not in value and len(value) <= 12:
            return value.upper()
    if title:
        ext = os.path.splitext(str(title))[1].lower()
        if ext:
            return EXT_LABELS.get(ext, ext.lstrip(".").upper())
    return None


def _format_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%d.%m.%Y")
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text.replace("+00:00", "Z"), fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return text[:10]


def _clean(text: Any, limit: Optional[int] = None) -> str:
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(part) for part in text if part)
    value = " ".join(str(text).split())
    if limit and len(value) > limit:
        value = value[:limit].rstrip() + " […]"
    return value


# Microsoft Graph sürücü yolu: /drives/{drive-id}/root:/Klasör/Dosya.docx
DRIVE_PATH_RE = re.compile(r"/drives/[^/]+/root:(?P<path>/.*)$", re.IGNORECASE)
# Parça sırası: uid genelde "..._pages_3" gibi biter.
CHUNK_ORDER_RE = re.compile(r"(\d+)\s*$")


def _raw_path(raw: Any) -> str:
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    return str(raw).strip() if raw else ""


def _browse_url(site: str, library: str, relative: str) -> str:
    """Dosyayı SharePoint web arayüzünde, klasörü açık ve dosya seçili gösteren adres.

    Doğrudan dosya adresi tarayıcıda indirme başlattığı için varsayılan budur.
    `id` ve `parent` parametreleri sunucuya göreli yollardır ve tamamen kodlanır.
    """
    site_path = urlsplit(site).path.rstrip("/")
    server_relative = f"{site_path}/{library}/{relative}"
    parent = server_relative.rsplit("/", 1)[0]
    return (
        f"{site}/{quote(library, safe='')}/Forms/AllItems.aspx"
        f"?id={quote(server_relative, safe='')}&parent={quote(parent, safe='')}"
    )


def _http_document_urls(url: str, site: str, library: str) -> Tuple[str, str]:
    """Tam HTTP adresini (web arayüzü, indirme) çiftine çevirir.

    Site ve kitaplık ayarı dosya yolunun altında eşleşirse AllItems.aspx adresi
    üretilir; aksi halde orijinal adres her iki uçta da korunur.
    """
    parsed = urlsplit(url)
    path = unquote(parsed.path)

    if "forms/allitems.aspx" in path.lower():
        file_id = (parse_qs(parsed.query).get("id") or [None])[0]
        if file_id:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            return url, origin + quote(unquote(file_id), safe="/")
        return url, url

    if not site or not library:
        return url, url

    prefix = f"{urlsplit(site).path.rstrip('/')}/{unquote(library).strip('/')}/"
    if not path.startswith(prefix):
        return url, url
    relative = path[len(prefix) :]
    if not relative or relative.lower().startswith("forms/"):
        return url, url
    return _browse_url(site, library, relative), url


def _document_urls(raw: Any, settings: Settings) -> Tuple[Optional[str], Optional[str]]:
    """İndeksteki adres değerinden (web arayüzü adresi, doğrudan dosya adresi) üretir."""
    url = _raw_path(raw)
    if not url:
        return None, None

    site = (settings.sharepoint_site_url or "").rstrip("/")
    library = (settings.sharepoint_doc_library or "").strip("/")

    if url.startswith(("http://", "https://")):
        return _http_document_urls(url, site, library)

    if not site:
        return None, None

    # Graph sürücü yolunu doküman kitaplığı adresine çevir.
    match = DRIVE_PATH_RE.search(url)
    if match:
        relative = match.group("path").lstrip("/")
        parts = [site]
        if library:
            parts.append(quote(library, safe="/"))
        parts.append(quote(relative, safe="/"))
        direct = "/".join(parts)
        browse = _browse_url(site, library, relative) if library else direct
        return browse, direct

    # Kitaplık sınırı bilinmediğinden web arayüzü adresi güvenle kurulamaz.
    fallback = site + "/" + quote(url.lstrip("/"), safe="/")
    return fallback, fallback


def enrich_web_sources(
    docs: List[SourceDoc],
    settings: Settings,
    **kwargs: Any,
) -> Tuple[List[SourceDoc], Dict[str, Any]]:
    """İnce kanca: .msg benzeri ince içerikteki açık web linklerini doldurur."""
    from .webcontent import enrich_sources

    return enrich_sources(docs, settings, **kwargs)


def apply_sharepoint_links(doc: SourceDoc, settings: Settings) -> SourceDoc:
    """Kaynağın birincil / indirme adreslerini `SHAREPOINT_LINK_MODE`'a göre doldurur."""
    raw = doc.url or (doc.extra or {}).get("yol")
    browse, direct = _document_urls(raw, settings)
    if not (browse or direct):
        return doc
    doc.browse_url = browse
    doc.download_url = direct
    doc.url = direct if settings.sharepoint_link_mode == "direct" else (browse or direct)
    return doc


def _resolve_url(raw: Any, settings: Settings) -> Optional[str]:
    """Ayardaki bağlantı biçimine göre birincil adresi döndürür."""
    browse, direct = _document_urls(raw, settings)
    return direct if settings.sharepoint_link_mode == "direct" else browse


def _title_from_path(raw: Any) -> str:
    """Başlık alanı olmayan indekslerde yol/URL'den dosya adını çıkarır."""
    path = _raw_path(raw)
    if not path:
        return ""
    path = path.split("?")[0].split("#")[0].rstrip("/")
    base = path.rsplit("/", 1)[-1]
    if base.startswith("root:"):
        base = base[len("root:") :]
    return unquote(base)


def _chunk_order(key: Any) -> int:
    match = CHUNK_ORDER_RE.search(str(key or ""))
    return int(match.group(1)) if match else 0


def _caption_text(result: Dict[str, Any]) -> str:
    captions = result.get("@search.captions") or []
    parts = []
    for caption in captions:
        text = getattr(caption, "text", None) or (
            caption.get("text") if isinstance(caption, dict) else None
        )
        if text:
            parts.append(_clean(text))
    return " … ".join(parts)


def _passes_relevance(
    reranker: Any,
    min_reranker_score: float,
    semantic_on: bool,
) -> bool:
    """Semantik açıkken eşik altı (ve skoru olmayan) kayıtlar kesinlikle düşer.

    Eşik geçilmezse en iyi N'ye düşülmez; alakasız kaynak modele gitmez.
    """
    if semantic_on:
        if reranker is None:
            return False
        try:
            return float(reranker) >= float(min_reranker_score)
        except (TypeError, ValueError):
            return False
    if min_reranker_score and reranker is not None:
        try:
            return float(reranker) >= float(min_reranker_score)
        except (TypeError, ValueError):
            return True
    return True


def _relevance_sort_key(entry: Dict[str, Any]) -> Tuple[float, float]:
    """Birincil: semantik reranker; ikincil: vektör/arama skoru."""
    reranker = entry.get("reranker")
    score = entry.get("score")
    try:
        rerank_key = float(reranker) if reranker is not None else -1.0
    except (TypeError, ValueError):
        rerank_key = -1.0
    try:
        score_key = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score_key = 0.0
    return (rerank_key, score_key)


def resolve_candidate_count(
    top_k: int,
    *,
    merge_chunks: bool = True,
    configured: Optional[int] = None,
) -> int:
    """Azure Search `top` değeri: arşivden çekilecek aday sayısı.

    Modele giden kaynak sayısı `top_k` ile sınırlanır; aday havuzu daha geniştir
    ki reranker yeterince alakalı parçaları seçebilsin ve aynı dosya birleşsin.
    Birleştirme açıkken havuz slider'dan büyük kalır: en az 48 veya top_k*4.
    """
    top_k = max(int(top_k), 1)
    if not merge_chunks:
        return max(int(configured), top_k) if configured else top_k
    floor = max(48, top_k * 4)
    if configured:
        return max(int(configured), floor)
    return floor


class Retriever:
    """Şema keşfine dayanarak indeks tipinden bağımsız arama yapar."""

    def __init__(self, settings: Settings, schema: IndexSchema, embedder=None):
        self.settings = settings
        self.schema = schema
        self._embedder = embedder
        self._client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=settings.search_index,
            credential=self._credential(),
        )

    def _credential(self):
        if self.settings.search_api_key:
            return AzureKeyCredential(self.settings.search_api_key)
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential(exclude_interactive_browser_credential=False)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - kapanışta hata önemli değil
            pass

    # ------------------------------------------------------------------
    # Sorgu kurulumu
    # ------------------------------------------------------------------
    def _vector_queries(self, query: str, k: int) -> List[Any]:
        if not self.schema.vector_fields:
            return []
        fields = ",".join(self.schema.vector_fields)

        if self.schema.has_vectorizer:
            return [VectorizableTextQuery(text=query, k_nearest_neighbors=k, fields=fields)]

        if self._embedder is not None:
            try:
                vector = self._embedder(query)
            except Exception as exc:
                logger.warning("Embedding üretilemedi, vektör arama atlanıyor: %s", exc)
                return []
            if vector:
                return [VectorizedQuery(vector=vector, k_nearest_neighbors=k, fields=fields)]
        return []

    def _select_fields(self) -> Optional[List[str]]:
        wanted = [
            self.schema.key_field,
            self.schema.content_field,
            self.schema.title_field,
            self.schema.url_field,
            self.schema.last_modified_field,
            self.schema.file_type_field,
            self.schema.parent_id_field,
        ]
        allowed = set(self.schema.selectable_fields or self.schema.all_fields)
        selected = [f for f in dict.fromkeys(filter(None, wanted)) if f in allowed]
        return selected or None

    # ------------------------------------------------------------------
    # Arama
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        odata_filter: Optional[str] = None,
        use_semantic: bool = True,
        use_vector: bool = True,
        min_reranker_score: Optional[float] = None,
        merge_chunks: Optional[bool] = None,
    ) -> Tuple[List[SourceDoc], Dict[str, Any]]:
        settings = self.settings
        top_k = top_k or settings.top_k
        min_reranker_score = (
            settings.min_reranker_score if min_reranker_score is None else min_reranker_score
        )
        merge_chunks = settings.merge_chunks if merge_chunks is None else merge_chunks

        semantic_on = bool(use_semantic and self.schema.supports_semantic)
        candidate_count = resolve_candidate_count(
            top_k,
            merge_chunks=merge_chunks,
            configured=settings.candidate_count,
        )

        kwargs: Dict[str, Any] = {
            "search_text": query,
            "top": candidate_count,
            "include_total_count": False,
        }

        select = self._select_fields()
        if select:
            kwargs["select"] = select
        if odata_filter:
            kwargs["filter"] = odata_filter

        vector_queries = self._vector_queries(query, candidate_count) if use_vector else []
        if vector_queries:
            kwargs["vector_queries"] = vector_queries

        if semantic_on:
            kwargs["query_type"] = QueryType.SEMANTIC
            kwargs["semantic_configuration_name"] = self.schema.semantic_config
            kwargs["query_caption"] = QueryCaptionType.EXTRACTIVE
            kwargs["query_answer"] = QueryAnswerType.EXTRACTIVE

        debug: Dict[str, Any] = {
            "sorgu": query,
            "arama tipi": (
                "hibrit + semantik"
                if vector_queries and semantic_on
                else "hibrit"
                if vector_queries
                else "semantik"
                if semantic_on
                else "anahtar kelime"
            ),
            "aday sayısı": candidate_count,
            "kaynak üst sınırı": top_k,
            "reranker eşiği": min_reranker_score,
            "filtre": odata_filter or "-",
        }

        results = self._client.search(**kwargs)
        raw_docs: List[Dict[str, Any]] = []
        for result in results:
            raw_docs.append(dict(result))

        debug["dönen kayıt"] = len(raw_docs)

        docs = self._to_sources(
            raw_docs,
            min_reranker_score,
            merge_chunks,
            top_k,
            semantic_on=semantic_on,
        )
        debug["referans sayısı"] = len(docs)
        return docs, debug

    def _to_sources(
        self,
        raw_docs: List[Dict[str, Any]],
        min_reranker_score: float,
        merge_chunks: bool,
        top_k: int,
        *,
        semantic_on: bool = False,
    ) -> List[SourceDoc]:
        schema = self.schema
        settings = self.settings
        grouped: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []

        for raw in raw_docs:
            reranker = raw.get("@search.reranker_score")
            if not _passes_relevance(reranker, min_reranker_score, semantic_on):
                continue

            content = _clean(raw.get(schema.content_field) if schema.content_field else "")
            if not content:
                continue

            raw_url = raw.get(schema.url_field) if schema.url_field else None
            browse_url, download_url = _document_urls(raw_url, settings)
            url = download_url if settings.sharepoint_link_mode == "direct" else browse_url
            title = _clean(raw.get(schema.title_field) if schema.title_field else "") or ""
            if not title:
                # Başlık alanı olmayan indekslerde dosya adı en okunur başlıktır.
                title = _title_from_path(raw_url) or _title_from_path(url)

            chunk_key = raw.get(schema.key_field) if schema.key_field else None
            parent_id = raw.get(schema.parent_id_field) if schema.parent_id_field else None

            if merge_chunks:
                doc_key = str(parent_id or _raw_path(raw_url) or title or chunk_key or len(order))
            else:
                doc_key = str(chunk_key or len(order))

            entry = grouped.get(doc_key)
            if entry is None:
                entry = {
                    "doc_id": str(parent_id or chunk_key or doc_key),
                    "title": title,
                    "url": url,
                    "browse_url": browse_url,
                    "download_url": download_url,
                    "raw_path": _raw_path(raw_url),
                    "chunks": [],
                    "score": raw.get("@search.score"),
                    "reranker": reranker,
                    "caption": _caption_text(raw),
                    "last_modified": _format_date(
                        raw.get(schema.last_modified_field) if schema.last_modified_field else None
                    ),
                    "file_type": _friendly_file_type(
                        raw.get(schema.file_type_field) if schema.file_type_field else None,
                        title or url,
                    ),
                }
                grouped[doc_key] = entry
                order.append(doc_key)
            else:
                # Aynı dokümanın başka bir parçası: en iyi skoru koru.
                if raw.get("@search.score") and (entry["score"] or 0) < raw["@search.score"]:
                    entry["score"] = raw["@search.score"]
                if reranker is not None and (entry["reranker"] or 0) < reranker:
                    entry["reranker"] = reranker
                if not entry["caption"]:
                    entry["caption"] = _caption_text(raw)
                if not entry["title"]:
                    entry["title"] = title

            entry["chunks"].append((_chunk_order(chunk_key), content))

        ranked = sorted(
            (grouped[key] for key in order),
            key=_relevance_sort_key,
            reverse=True,
        )[:top_k]

        sources: List[SourceDoc] = []
        for ordinal, entry in enumerate(ranked, start=1):
            # Parçaları doküman içindeki sırasına göre birleştir; bağlam tutarlı olsun.
            ordered_chunks = [text for _, text in sorted(entry["chunks"], key=lambda c: c[0])]
            merged = _clean("\n\n".join(ordered_chunks[:4]), settings.max_chars_per_doc)
            snippet = entry["caption"] or _clean(merged, 320)
            sources.append(
                SourceDoc(
                    ordinal=ordinal,
                    doc_id=entry["doc_id"],
                    title=entry["title"],
                    content=merged,
                    url=entry["url"],
                    browse_url=entry["browse_url"],
                    download_url=entry["download_url"],
                    snippet=snippet,
                    score=entry["score"],
                    reranker_score=entry["reranker"],
                    last_modified=entry["last_modified"],
                    file_type=entry["file_type"],
                    chunk_count=len(entry["chunks"]),
                    extra={"yol": entry["raw_path"]} if entry["raw_path"] else {},
                )
            )
        return sources

    def document_count(self) -> Optional[int]:
        try:
            return int(self._client.get_document_count())
        except Exception as exc:
            logger.warning("Doküman sayısı alınamadı: %s", exc)
            return None

    def facet_values(self, field_name: str, limit: int = 25) -> List[Tuple[str, int]]:
        """Sidebar filtreleri için facet değerlerini getirir."""
        if field_name not in self.schema.facetable_fields:
            return []
        try:
            results = self._client.search(
                search_text="*", top=0, facets=[f"{field_name},count:{limit}"]
            )
            facets = results.get_facets() or {}
        except Exception as exc:
            logger.warning("Facet alınamadı (%s): %s", field_name, exc)
            return []
        return [
            (str(item["value"]), int(item.get("count", 0)))
            for item in facets.get(field_name, [])
            if item.get("value") not in (None, "")
        ]
