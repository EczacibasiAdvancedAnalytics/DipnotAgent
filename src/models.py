"""Motorlar ve arayüz arasında paylaşılan veri yapıları."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Set

CITATION_RE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")


@dataclass
class SourceDoc:
    """Knowledge base'den dönen tek bir kaynak doküman."""

    ordinal: int
    doc_id: str
    title: str
    content: str
    url: Optional[str] = None
    snippet: str = ""
    score: Optional[float] = None
    reranker_score: Optional[float] = None
    last_modified: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)
    # SharePoint web arayüzünde açan adres (klasör görünümü, dosya seçili).
    browse_url: Optional[str] = None
    # Dosyayı doğrudan indiren adres.
    download_url: Optional[str] = None

    @property
    def display_title(self) -> str:
        return self.title or self.url or self.doc_id or f"Kaynak {self.ordinal}"

    @property
    def best_score(self) -> Optional[float]:
        return self.reranker_score if self.reranker_score is not None else self.score

    @property
    def open_url(self) -> Optional[str]:
        """Kullanıcıya sunulan birincil bağlantı."""
        return self.browse_url or self.url

    def to_dict(self) -> Dict[str, Any]:
        """Sohbet geçmişinde saklanabilmesi için JSON uyumlu sözlük."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SourceDoc":
        """`to_dict` çıktısını geri yükler; bilinmeyen alanları yok sayar."""
        known = {f.name for f in fields(cls)}
        kwargs = {key: value for key, value in (data or {}).items() if key in known}
        kwargs.setdefault("ordinal", 0)
        kwargs.setdefault("doc_id", "")
        kwargs.setdefault("title", "")
        kwargs.setdefault("content", "")
        kwargs["extra"] = dict(kwargs.get("extra") or {})
        return cls(**kwargs)


@dataclass
class AnswerResult:
    """Bir sorunun cevabı, kaynakları ve çalışma bilgileri."""

    answer: str
    sources: List[SourceDoc] = field(default_factory=list)
    cited_ordinals: Set[int] = field(default_factory=set)
    backend: str = "direct"
    latency_ms: int = 0
    debug: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def cited_sources(self) -> List[SourceDoc]:
        return [doc for doc in self.sources if doc.ordinal in self.cited_ordinals]


def parse_cited_ordinals(answer: str) -> Set[int]:
    """Cevap metnindeki [1], [2, 3] biçimindeki atıfları çıkarır."""
    found: Set[int] = set()
    for match in CITATION_RE.finditer(answer or ""):
        for part in re.split(r"[,;]", match.group(1)):
            part = part.strip()
            if part.isdigit():
                found.add(int(part))
    return found


def linkify_citations(answer: str, sources: List[SourceDoc]) -> str:
    """[1] atıflarını, kaynağın SharePoint adresine giden tıklanabilir linklere çevirir."""
    by_ordinal = {doc.ordinal: doc for doc in sources}

    def replace(match: re.Match) -> str:
        rendered = []
        for part in re.split(r"[,;]", match.group(1)):
            part = part.strip()
            if not part.isdigit():
                continue
            doc = by_ordinal.get(int(part))
            href = (doc.open_url or doc.url) if doc else None
            if href:
                rendered.append(f"[[{part}]]({href})")
            else:
                rendered.append(f"[{part}]")
        return "".join(rendered) or match.group(0)

    return CITATION_RE.sub(replace, answer or "")
