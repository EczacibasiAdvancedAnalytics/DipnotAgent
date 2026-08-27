"""Azure AI Search indeks şemasının otomatik keşfi.

SharePoint indexer'ın ürettiği indeksler `metadata_spo_*` alanlarını kullanır,
entegre vektörleştirme ile kurulan indeksler ise `chunk` / `text_vector` gibi
adlar taşır. Bu modül indeks tanımını okuyup hangi alanın içerik, başlık, adres
ve vektör alanı olduğunu tahmin eder; böylece .env içinde alan adı yazmak
zorunlu olmaz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient

from .config import Settings

# Sıra önemlidir: baştaki adaylar daha güçlü eşleşme sayılır.
CONTENT_CANDIDATES = [
    "content",
    "chunk",
    "snippet",
    "merged_content",
    "text",
    "page_content",
    "pagecontent",
    "body",
    "document_content",
    "extracted_content",
]

# Parçalanmış (chunk) indekslerde aynı dokümana ait parçaları birbirine bağlayan alan.
PARENT_ID_CANDIDATES = [
    "snippet_parent_id",
    "parent_id",
    "parent_key",
    "parentid",
    "document_id",
    "source_id",
]

TITLE_CANDIDATES = [
    "metadata_spo_item_name",
    "title",
    "document_title",
    "metadata_title",
    "metadata_storage_name",
    "filename",
    "file_name",
    "name",
]

URL_CANDIDATES = [
    "metadata_spo_item_weburi",
    "url",
    "source_url",
    "sourceurl",
    "web_url",
    "weburl",
    "metadata_spo_item_path",
    "metadata_storage_path",
    "filepath",
    "path",
    "source",
]

LAST_MODIFIED_CANDIDATES = [
    "metadata_spo_item_last_modified",
    "last_modified",
    "lastmodified",
    "metadata_storage_last_modified",
    "modified",
    "created",
]

FILE_TYPE_CANDIDATES = [
    "metadata_spo_item_content_type",
    "metadata_content_type",
    "file_type",
    "filetype",
    "metadata_storage_file_extension",
    "extension",
    "doc_type",
]

DATE_TYPES = {"Edm.DateTimeOffset"}

# Azure AI Search vektör alanları Collection(Edm.Single|Half) + dimensions taşır.
VECTOR_COLLECTION_TYPES = {
    "Collection(Edm.Single)",
    "Collection(Edm.Half)",
    "Collection(Edm.Float)",
}
VECTOR_NAME_CANDIDATES = [
    "snippet_vector",
    "text_vector",
    "content_vector",
    "contentVector",
    "embedding",
    "embeddings",
]


def _field_type(f: Any) -> str:
    raw = getattr(f, "type", "")
    return str(getattr(raw, "value", raw) or "")


def _is_vector(f: Any) -> bool:
    """Vektör alanı: dimensions, profil, Collection(Edm.Single) veya *vector* adı."""
    if getattr(f, "vector_search_dimensions", None) or getattr(f, "dimensions", None):
        return True
    if getattr(f, "vector_search_profile_name", None) or getattr(
        f, "vectorSearchProfile", None
    ):
        return True
    type_name = _field_type(f)
    if type_name in VECTOR_COLLECTION_TYPES:
        return True
    name = (getattr(f, "name", "") or "").lower()
    return "vector" in name and "collection" in type_name.lower()


def _is_retrievable(f: Any) -> bool:
    hidden = getattr(f, "hidden", None)
    if hidden is not None:
        return not hidden
    retrievable = getattr(f, "retrievable", None)
    return True if retrievable is None else bool(retrievable)


def _pick(candidates: Sequence[str], available: Dict[str, Any]) -> Optional[str]:
    lowered = {name.lower(): name for name in available}
    for candidate in candidates:
        hit = lowered.get(candidate.lower())
        if hit:
            return hit
    # Adaylar bulunamazsa "içinde geçiyor mu" şeklinde gevşek eşleşme dene.
    for candidate in candidates:
        for lower_name, original in lowered.items():
            if candidate.lower() in lower_name:
                return original
    return None


@dataclass
class IndexSchema:
    """Keşfedilmiş indeks yapısı."""

    index_name: str
    key_field: Optional[str] = None
    content_field: Optional[str] = None
    title_field: Optional[str] = None
    url_field: Optional[str] = None
    last_modified_field: Optional[str] = None
    file_type_field: Optional[str] = None
    parent_id_field: Optional[str] = None
    vector_fields: List[str] = field(default_factory=list)
    has_vectorizer: bool = False
    semantic_config: Optional[str] = None
    selectable_fields: List[str] = field(default_factory=list)
    filterable_fields: List[str] = field(default_factory=list)
    facetable_fields: List[str] = field(default_factory=list)
    all_fields: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def supports_semantic(self) -> bool:
        return bool(self.semantic_config)

    @property
    def supports_vector(self) -> bool:
        return bool(self.vector_fields)

    def as_summary(self) -> Dict[str, str]:
        """Tanılama tablosu için tamamı metin olan özet (Arrow uyumluluğu)."""
        return {
            "indeks": self.index_name,
            "anahtar alan": self.key_field or "-",
            "içerik alanı": self.content_field or "-",
            "başlık alanı": self.title_field or "-",
            "adres (URL) alanı": self.url_field or "-",
            "son değişiklik alanı": self.last_modified_field or "-",
            "dosya türü alanı": self.file_type_field or "-",
            "parça gruplama alanı": self.parent_id_field or "-",
            "vektör alanları": ", ".join(self.vector_fields) or "-",
            "indekste vectorizer var": "evet" if self.has_vectorizer else "hayır",
            "semantik yapılandırma": self.semantic_config or "-",
            "toplam alan sayısı": str(len(self.all_fields)),
        }


def _credential(settings: Settings):
    if settings.search_api_key:
        return AzureKeyCredential(settings.search_api_key)
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=False)


def _detect_vectorizer(index: Any, vector_fields: List[str], fields_by_name: Dict[str, Any]) -> bool:
    """İndekste sorgu metnini otomatik vektöre çeviren bir vectorizer var mı?"""
    vector_search = getattr(index, "vector_search", None)
    if not vector_search:
        return False
    vectorizers = getattr(vector_search, "vectorizers", None) or []
    if not vectorizers:
        return False

    profiles = {p.name: p for p in (getattr(vector_search, "profiles", None) or [])}
    vectorizer_names = {getattr(v, "vectorizer_name", None) or getattr(v, "name", None) for v in vectorizers}

    for name in vector_fields:
        f = fields_by_name.get(name)
        profile_name = getattr(f, "vector_search_profile_name", None)
        profile = profiles.get(profile_name) if profile_name else None
        if profile is None:
            continue
        attached = getattr(profile, "vectorizer_name", None) or getattr(profile, "vectorizer", None)
        if attached and attached in vectorizer_names:
            return True
    # Profil eşlemesi çözülemediyse, vectorizer varlığını yeterli kabul et.
    return True


def _semantic_hints(semantic: Any, config_name: Optional[str]) -> tuple:
    """Semantik yapılandırmada bildirilen içerik ve başlık alanlarını döndürür."""
    if not semantic:
        return None, None
    configs = list(getattr(semantic, "configurations", None) or [])
    chosen = next((c for c in configs if getattr(c, "name", None) == config_name), None)
    if chosen is None:
        chosen = configs[0] if configs else None
    if chosen is None:
        return None, None

    prioritized = getattr(chosen, "prioritized_fields", None)
    title = getattr(getattr(prioritized, "title_field", None), "field_name", None)
    contents = [
        x.field_name
        for x in (getattr(prioritized, "content_fields", None) or [])
        if getattr(x, "field_name", None)
    ]
    return (contents[0] if contents else None), title


def list_index_names(settings: Settings) -> List[str]:
    """Serviste tanımlı indeks adları. Yanlış indeks adı hatalarında yol göstermek için."""
    client = SearchIndexClient(endpoint=settings.search_endpoint, credential=_credential(settings))
    try:
        return sorted(client.list_index_names())
    finally:
        client.close()


def discover_schema(settings: Settings, index_name: Optional[str] = None) -> IndexSchema:
    """İndeks tanımını Azure'dan okuyup alan eşlemesini üretir.

    `index_name` verilirse (Streamlit önbellek anahtarı) `.env`'deki addan bağımsız
    olarak o indeks okunur; böylece cache anahtarı ile gerçek çağrı ayrışmaz.
    """
    name = index_name or settings.search_index
    client = SearchIndexClient(endpoint=settings.search_endpoint, credential=_credential(settings))
    try:
        index = client.get_index(name)
    finally:
        client.close()
    return build_schema(index, settings, index_name=name)


def build_schema(
    index: Any, settings: Settings, index_name: Optional[str] = None
) -> IndexSchema:
    """Bir SearchIndex tanımından alan eşlemesini çıkarır."""
    fields = list(getattr(index, "fields", None) or [])
    fields_by_name = {f.name: f for f in fields}
    schema = IndexSchema(
        index_name=index_name or getattr(index, "name", None) or settings.search_index
    )
    schema.all_fields = {f.name: _field_type(f) for f in fields}

    simple_fields = {f.name: f for f in fields if not _is_vector(f)}
    retrievable = {name: f for name, f in simple_fields.items() if _is_retrievable(f)}
    text_fields = {
        name: f for name, f in retrievable.items() if _field_type(f) == "Edm.String"
    }
    date_fields = {name: f for name, f in retrievable.items() if _field_type(f) in DATE_TYPES}

    for f in fields:
        if getattr(f, "key", False):
            schema.key_field = f.name
        if getattr(f, "filterable", False):
            schema.filterable_fields.append(f.name)
        if getattr(f, "facetable", False):
            schema.facetable_fields.append(f.name)

    discovered_vectors = [f.name for f in fields if _is_vector(f)]
    if not discovered_vectors:
        lowered = {f.name.lower(): f.name for f in fields}
        for candidate in VECTOR_NAME_CANDIDATES:
            hit = lowered.get(candidate.lower())
            if hit and hit not in discovered_vectors:
                discovered_vectors.append(hit)
    schema.vector_fields = list(settings.field_vector) if settings.field_vector else discovered_vectors
    schema.has_vectorizer = _detect_vectorizer(index, schema.vector_fields, fields_by_name)

    semantic = getattr(index, "semantic_search", None)
    if settings.semantic_config:
        schema.semantic_config = settings.semantic_config
    elif semantic:
        configs = list(getattr(semantic, "configurations", None) or [])
        default_name = getattr(semantic, "default_configuration_name", None)
        if default_name:
            schema.semantic_config = default_name
        elif configs:
            schema.semantic_config = configs[0].name

    # Semantik yapılandırma, içerik ve başlık alanını açıkça bildirir; en güvenilir ipucu.
    semantic_content, semantic_title = _semantic_hints(semantic, schema.semantic_config)

    schema.content_field = (
        settings.field_content
        or (semantic_content if semantic_content in text_fields else None)
        or _pick(CONTENT_CANDIDATES, text_fields)
    )
    if not schema.content_field and text_fields:
        # Son çare: aranabilir metin alanlarından ilkini içerik say. Anahtar alan
        # genelde kimlik taşır, içerik değil; bu yüzden hariç tutulur.
        searchable = [
            name
            for name, f in text_fields.items()
            if getattr(f, "searchable", False) and name != schema.key_field
        ]
        fallback = [name for name in text_fields if name != schema.key_field]
        schema.content_field = (
            searchable[0] if searchable else (fallback[0] if fallback else schema.key_field)
        )
        schema.warnings.append(
            f"İçerik alanı kesin belirlenemedi, '{schema.content_field}' kullanılıyor. "
            "Gerekirse .env içinde SEARCH_FIELD_CONTENT ile sabitleyin."
        )

    schema.title_field = (
        settings.field_title
        or (semantic_title if semantic_title in text_fields else None)
        or _pick(TITLE_CANDIDATES, {n: f for n, f in text_fields.items() if n != schema.key_field})
    )
    schema.url_field = settings.field_url or _pick(URL_CANDIDATES, text_fields)
    schema.last_modified_field = settings.field_last_modified or _pick(
        LAST_MODIFIED_CANDIDATES, date_fields
    ) or _pick(LAST_MODIFIED_CANDIDATES, text_fields)
    schema.file_type_field = settings.field_file_type or _pick(FILE_TYPE_CANDIDATES, text_fields)
    schema.parent_id_field = _pick(
        PARENT_ID_CANDIDATES,
        {n: f for n, f in text_fields.items() if n not in {schema.key_field, schema.content_field}},
    )

    # select listesi: vektör alanlarını ve çok büyük olmayan alanları içerir.
    schema.selectable_fields = sorted(retrievable.keys())

    if not schema.content_field:
        schema.warnings.append("İndekste metin içeriği taşıyan bir alan bulunamadı.")
    if not schema.url_field:
        schema.warnings.append(
            "Doküman adresi (URL) alanı bulunamadı; referanslar link içermeyecek. "
            "SharePoint indexer için beklenen alan: metadata_spo_item_weburi"
        )
    if not schema.semantic_config:
        schema.warnings.append(
            "Semantik yapılandırma yok; semantik sıralama ve extractive caption devre dışı. "
            "Azure portalda indekse bir semantic configuration eklemeniz kalite artırır."
        )
    if not schema.vector_fields:
        schema.warnings.append(
            "Vektör alanı yok; yalnızca anahtar kelime (keyword) araması yapılacak."
        )

    return schema
