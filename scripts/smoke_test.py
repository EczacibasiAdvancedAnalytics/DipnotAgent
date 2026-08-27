"""Azure bağlantısı gerektirmeyen iç tutarlılık testleri.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.search.documents.indexes.models import (  # noqa: E402
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    VectorSearch,
    VectorSearchProfile,
)

from src.config import Settings  # noqa: E402
from src.history import ChatStore, title_from_question  # noqa: E402
from src.models import SourceDoc, linkify_citations, parse_cited_ordinals  # noqa: E402
from src.prompts import SYSTEM_PROMPT, build_user_message  # noqa: E402
from src.retrieval import (  # noqa: E402
    Retriever,
    _chunk_order,
    _clean,
    _document_urls,
    _format_date,
    _friendly_file_type,
    _passes_relevance,
    _resolve_url,
    _title_from_path,
    apply_sharepoint_links,
    enrich_web_sources,
    resolve_candidate_count,
)
from src.schema import IndexSchema, build_schema  # noqa: E402
from src.webcontent import (  # noqa: E402
    enrich_sources as enrich_web_sources,
    extract_urls,
    html_to_text,
    is_download_url,
    is_paywall_url,
    is_ssrf_unsafe,
    is_thin_or_link_heavy,
)
from src.webcontent import (  # noqa: E402
    extract_urls,
    html_to_text,
    is_download_url,
    is_paywall_url,
    is_ssrf_unsafe,
    is_thin_or_link_heavy,
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[ OK ] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        failures.append(label)


def sharepoint_index() -> SearchIndex:
    """SharePoint indexer'ın ürettiği tipik indeks."""
    string_type = SearchFieldDataType.String
    return SearchIndex(
        name="spo-index",
        fields=[
            SearchField(name="metadata_spo_site_library_item_id", type=string_type, key=True),
            SearchField(name="metadata_spo_item_name", type=string_type, searchable=True),
            SearchField(name="metadata_spo_item_path", type=string_type),
            SearchField(
                name="metadata_spo_item_content_type",
                type=string_type,
                filterable=True,
                facetable=True,
            ),
            SearchField(
                name="metadata_spo_item_last_modified",
                type=SearchFieldDataType.DateTimeOffset,
                filterable=True,
                sortable=True,
            ),
            SearchField(name="metadata_spo_item_weburi", type=string_type),
            SearchField(name="content", type=string_type, searchable=True),
        ],
    )


def vectorized_index() -> SearchIndex:
    """Entegre vektörleştirme ile kurulmuş, semantik yapılandırmalı indeks."""
    string_type = SearchFieldDataType.String
    return SearchIndex(
        name="chunked-index",
        fields=[
            SearchField(name="chunk_id", type=string_type, key=True),
            SearchField(name="title", type=string_type, searchable=True),
            SearchField(name="chunk", type=string_type, searchable=True),
            SearchField(name="url", type=string_type),
            SearchField(
                name="text_vector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=1536,
                vector_search_profile_name="profile-1",
            ),
        ],
        vector_search=VectorSearch(
            profiles=[
                VectorSearchProfile(name="profile-1", algorithm_configuration_name="hnsw-1")
            ],
            algorithms=[HnswAlgorithmConfiguration(name="hnsw-1")],
        ),
        semantic_search=SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name="semantic-1",
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[SemanticField(field_name="chunk")],
                    ),
                )
            ]
        ),
    )


def test_sharepoint_schema() -> None:
    print("\n1) SharePoint indexer şeması")
    schema = build_schema(sharepoint_index(), Settings())
    check("içerik alanı = content", schema.content_field == "content", schema.content_field or "")
    check(
        "başlık alanı = metadata_spo_item_name",
        schema.title_field == "metadata_spo_item_name",
        schema.title_field or "",
    )
    check(
        "URL alanı = metadata_spo_item_weburi",
        schema.url_field == "metadata_spo_item_weburi",
        schema.url_field or "",
    )
    check(
        "tarih alanı = metadata_spo_item_last_modified",
        schema.last_modified_field == "metadata_spo_item_last_modified",
        schema.last_modified_field or "",
    )
    check(
        "dosya türü alanı = metadata_spo_item_content_type",
        schema.file_type_field == "metadata_spo_item_content_type",
        schema.file_type_field or "",
    )
    check(
        "anahtar alan bulundu",
        schema.key_field == "metadata_spo_site_library_item_id",
        schema.key_field or "",
    )
    check("vektör yok", not schema.supports_vector)
    check("semantik yok", not schema.supports_semantic)
    check("uyarı üretildi", len(schema.warnings) >= 2)
    check(
        "filtrelenebilir alanlar tespit edildi",
        "metadata_spo_item_content_type" in schema.filterable_fields,
    )


def test_vectorized_schema() -> None:
    print("\n2) Vektörleştirilmiş / chunk'lı indeks şeması")
    schema = build_schema(vectorized_index(), Settings())
    check("içerik alanı = chunk", schema.content_field == "chunk", schema.content_field or "")
    check("başlık alanı = title", schema.title_field == "title", schema.title_field or "")
    check("URL alanı = url", schema.url_field == "url", schema.url_field or "")
    check("vektör alanı = text_vector", schema.vector_fields == ["text_vector"], str(schema.vector_fields))
    check("semantik yapılandırma = semantic-1", schema.semantic_config == "semantic-1")
    check("vectorizer yok (profilde tanımsız)", schema.has_vectorizer is False)


def snippet_index() -> SearchIndex:
    """Gerçek fe-partial-data-index yapısı: başlık alanı yok, anahtar alan aranabilir,
    içerik alanı yalnızca semantik yapılandırmada bildirilmiş."""
    string_type = SearchFieldDataType.String
    return SearchIndex(
        name="fe-partial-data-index",
        fields=[
            SearchField(name="uid", type=string_type, key=True, searchable=True, sortable=True),
            SearchField(name="snippet_parent_id", type=string_type, filterable=True),
            SearchField(name="doc_url", type=string_type, filterable=True),
            SearchField(name="snippet", type=string_type, searchable=True),
        ],
        semantic_search=SemanticSearch(
            default_configuration_name="fe-partial-data-semantic-configuration",
            configurations=[
                SemanticConfiguration(
                    name="fe-partial-data-semantic-configuration",
                    prioritized_fields=SemanticPrioritizedFields(
                        content_fields=[SemanticField(field_name="snippet")]
                    ),
                )
            ],
        ),
    )


def test_snippet_schema() -> None:
    print("\n3) Parçalanmış SharePoint indeksi (başlık alanı yok)")
    schema = build_schema(snippet_index(), Settings())
    check(
        "içerik alanı semantik yapılandırmadan alındı",
        schema.content_field == "snippet",
        schema.content_field or "",
    )
    check("anahtar alan içerik sanılmadı", schema.content_field != "uid")
    check("URL alanı = doc_url", schema.url_field == "doc_url", schema.url_field or "")
    check(
        "parça gruplama alanı = snippet_parent_id",
        schema.parent_id_field == "snippet_parent_id",
        schema.parent_id_field or "",
    )
    check("başlık alanı yok olarak işaretlendi", schema.title_field is None, str(schema.title_field))
    check(
        "semantik yapılandırma bulundu",
        schema.semantic_config == "fe-partial-data-semantic-configuration",
    )
    check("içerik belirsizlik uyarısı üretilmedi", not any("İçerik alanı" in w for w in schema.warnings))


def snippet_vector_index(*, with_dimensions: bool = True) -> SearchIndex:
    """fe-partial-data-vector-index: snippet + gizli snippet_vector (3072)."""
    string_type = SearchFieldDataType.String
    vector_kwargs = {
        "name": "snippet_vector",
        "type": SearchFieldDataType.Collection(SearchFieldDataType.Single),
        "searchable": True,
        "hidden": True,
        "vector_search_profile_name": "snippet-vector-profile",
    }
    if with_dimensions:
        vector_kwargs["vector_search_dimensions"] = 3072
    return SearchIndex(
        name="fe-partial-data-vector-index",
        fields=[
            SearchField(name="uid", type=string_type, key=True, searchable=True, sortable=True),
            SearchField(name="snippet_parent_id", type=string_type, filterable=True),
            SearchField(name="doc_url", type=string_type, filterable=True),
            SearchField(name="snippet", type=string_type, searchable=True),
            SearchField(**vector_kwargs),
        ],
        vector_search=VectorSearch(
            profiles=[
                VectorSearchProfile(
                    name="snippet-vector-profile",
                    algorithm_configuration_name="hnsw-snippet",
                    vectorizer_name="aoai-text-embedding-3-large",
                )
            ],
            algorithms=[HnswAlgorithmConfiguration(name="hnsw-snippet")],
        ),
        semantic_search=SemanticSearch(
            default_configuration_name="fe-partial-data-semantic-configuration",
            configurations=[
                SemanticConfiguration(
                    name="fe-partial-data-semantic-configuration",
                    prioritized_fields=SemanticPrioritizedFields(
                        content_fields=[SemanticField(field_name="snippet")]
                    ),
                )
            ],
        ),
    )


def test_snippet_vector_schema() -> None:
    print("\n3b) Vektör kopyası (snippet_vector, gizli, 3072)")
    schema = build_schema(snippet_vector_index(), Settings(search_index="fe-partial-data-vector-index"))
    check("vektör alanı bulundu", schema.supports_vector, str(schema.vector_fields))
    check(
        "vektör alanı = snippet_vector",
        schema.vector_fields == ["snippet_vector"],
        str(schema.vector_fields),
    )
    check("içerik alanı = snippet", schema.content_field == "snippet", schema.content_field or "")
    check(
        "gizli vektör alanı seçilebilir listede değil",
        "snippet_vector" not in schema.selectable_fields,
    )
    check(
        "vektör yok uyarısı üretilmedi",
        not any("Vektör alanı yok" in w for w in schema.warnings),
        str(schema.warnings),
    )

    fallback = build_schema(
        snippet_vector_index(with_dimensions=False),
        Settings(search_index="fe-partial-data-vector-index"),
    )
    check(
        "dimensions olmasa da Collection(Edm.Single) vektör sayıldı",
        fallback.supports_vector and "snippet_vector" in fallback.vector_fields,
        str(fallback.vector_fields),
    )


def test_candidate_count() -> None:
    print("\n3c) Aday havuzu / kaynak üst sınırı")
    check("yapılandırılmış 48, üst sınır 12 -> 48", resolve_candidate_count(12, configured=48) == 48)
    check("üst sınır 30 iken aday en az 120", resolve_candidate_count(30, configured=48) == 120)
    check("yapılandırma yoksa max(48, top*4)", resolve_candidate_count(12, configured=None) == 48)
    check("üst sınır 20, yapılandırma yok -> 80", resolve_candidate_count(20, configured=None) == 80)
    check("birleştirme kapalı ve yapılandırma yoksa top_k", resolve_candidate_count(12, merge_chunks=False, configured=None) == 12)
    check("en az 1", resolve_candidate_count(0, configured=48) == 48)


def _test_retriever() -> Retriever:
    schema = IndexSchema(
        index_name="t",
        key_field="id",
        content_field="content",
        title_field="title",
        url_field="url",
        parent_id_field="parent_id",
    )
    return Retriever(
        Settings(
            search_endpoint="https://example.search.windows.net",
            search_index="t",
            search_api_key="x",
        ),
        schema,
    )


def test_relevance_filter() -> None:
    print("\n3d) Semantik yakınlık filtresi ve sıralama")
    check("eşik altı düşer", not _passes_relevance(1.5, 2.0, True))
    check("eşik üstü kalır", _passes_relevance(2.0, 2.0, True))
    check("semantikte skorsuz düşer", not _passes_relevance(None, 2.0, True))
    check("semantik kapalıyken skorsuz kalır", _passes_relevance(None, 2.0, False))

    retriever = _test_retriever()
    raw = [
        {
            "id": "low",
            "parent_id": "a",
            "title": "Alakasız.pdf",
            "content": "Hava durumu raporu.",
            "url": "https://sp/a.pdf",
            "@search.reranker_score": 1.4,
            "@search.score": 9.0,
        },
        {
            "id": "mid",
            "parent_id": "b",
            "title": "Zayıf.pdf",
            "content": "Konuya uzaktan değinen metin.",
            "url": "https://sp/b.pdf",
            "@search.reranker_score": 1.9,
            "@search.score": 8.0,
        },
        {
            "id": "hit-2",
            "parent_id": "c",
            "title": "Prosedür.pdf",
            "content": "Onay limiti 50.000 TL.",
            "url": "https://sp/c.pdf",
            "@search.reranker_score": 2.4,
            "@search.score": 3.0,
        },
        {
            "id": "hit-1",
            "parent_id": "d",
            "title": "Yetki.pdf",
            "content": "Genel müdür onayı gerekir.",
            "url": "https://sp/d.pdf",
            "@search.reranker_score": 3.1,
            "@search.score": 1.0,
        },
        {
            "id": "noscore",
            "parent_id": "e",
            "title": "Skorsuz.pdf",
            "content": "Reranker skoru yok.",
            "url": "https://sp/e.pdf",
            "@search.score": 12.0,
        },
    ]
    kept = retriever._to_sources(raw, 2.0, True, 12, semantic_on=True)
    titles = [doc.title for doc in kept]
    check("eşik altındakiler modele gitmedi", titles == ["Yetki.pdf", "Prosedür.pdf"], str(titles))
    check(
        "sıra reranker sonra skor",
        [doc.reranker_score for doc in kept] == [3.1, 2.4],
        str([doc.reranker_score for doc in kept]),
    )

    empty = retriever._to_sources(raw[:2], 2.0, True, 12, semantic_on=True)
    check("hepsi eşik altındaysa boş (en iyi 3 yok)", empty == [], str(empty))

    unranked = retriever._to_sources(raw, 2.0, True, 12, semantic_on=False)
    check(
        "semantik kapalıyken skorsuz da kalır",
        any(doc.title == "Skorsuz.pdf" for doc in unranked),
        str([doc.title for doc in unranked]),
    )


def test_auth_credentials() -> None:
    print("\n3e) Uygulama girişi")
    from src.auth import auth_is_required, check_credentials

    empty = Settings()
    check("şifre boşken login yok", not auth_is_required(empty))
    check("boş ayarda giriş reddedilir", not check_credentials("admin", "test-password", empty))

    locked = Settings(auth_user="admin", auth_password="test-password")
    check("şifre doluyken login zorunlu", auth_is_required(locked))
    check("doğru bilgiler kabul", check_credentials("admin", "test-password", locked))
    check("yanlış şifre reddedilir", not check_credentials("admin", "yanlis", locked))
    check("yanlış kullanıcı reddedilir", not check_credentials("root", "test-password", locked))
    check("boş form reddedilir", not check_credentials("", "", locked))


GRAPH_PATH = (
    "/drives/b!s9Sr4PwqOkSib5DCCCjGhA/root:/Faruk Bey Agent Data/EKA-Partial-Data/"
    "Cybersec/2014/Why senior leaders are the front line.msg"
)

DIRECT_URL = (
    "https://eczacibasi.sharepoint.com/sites/AGIPulse/Shared%20Documents/"
    "Faruk%20Bey%20Agent%20Data/EKA-Partial-Data/Cybersec/2014/"
    "Why%20senior%20leaders%20are%20the%20front%20line.msg"
)

BROWSE_URL = (
    "https://eczacibasi.sharepoint.com/sites/AGIPulse/Shared%20Documents/Forms/AllItems.aspx"
    "?id=%2Fsites%2FAGIPulse%2FShared%20Documents%2FFaruk%20Bey%20Agent%20Data"
    "%2FEKA-Partial-Data%2FCybersec%2F2014"
    "%2FWhy%20senior%20leaders%20are%20the%20front%20line.msg"
    "&parent=%2Fsites%2FAGIPulse%2FShared%20Documents%2FFaruk%20Bey%20Agent%20Data"
    "%2FEKA-Partial-Data%2FCybersec%2F2014"
)


def sharepoint_settings(link_mode: str = "browse") -> Settings:
    return Settings(
        sharepoint_site_url="https://eczacibasi.sharepoint.com/sites/AGIPulse",
        sharepoint_doc_library="Shared Documents",
        sharepoint_link_mode=link_mode,
    )


def test_sharepoint_links() -> None:
    print("\n4) SharePoint bağlantısı ve başlık türetme")
    settings = sharepoint_settings()
    url = _resolve_url(GRAPH_PATH, settings)
    check("varsayılan mod tarayıcıda açan adresi üretti", url == BROWSE_URL, str(url))
    check("boşluklar kodlandı", url is not None and " " not in url)
    check(
        "doğrudan dosya adresi ayrıca üretildi",
        _document_urls(GRAPH_PATH, settings)[1] == DIRECT_URL,
        str(_document_urls(GRAPH_PATH, settings)[1]),
    )
    check(
        "direct modunda dosya adresi birincil",
        _resolve_url(GRAPH_PATH, sharepoint_settings("direct")) == DIRECT_URL,
        str(_resolve_url(GRAPH_PATH, sharepoint_settings("direct"))),
    )
    check(
        "başlık dosya adından türetildi",
        _title_from_path(GRAPH_PATH) == "Why senior leaders are the front line.msg",
        _title_from_path(GRAPH_PATH),
    )
    check(
        "dosya türü uzantıdan bulundu",
        _friendly_file_type(None, _title_from_path(GRAPH_PATH)) == "E-posta",
        str(_friendly_file_type(None, _title_from_path(GRAPH_PATH))),
    )
    check("site adresi yoksa link üretilmez", _resolve_url(GRAPH_PATH, Settings()) is None)
    check("parça sırası uid'den okundu", _chunk_order("abc_pages_12") == 12, str(_chunk_order("abc_pages_12")))
    check("sırasız uid için 0", _chunk_order("abc") == 0)


def test_browse_url_parts() -> None:
    print("\n4b) Tarayıcıda açan adresin bileşenleri")
    settings = sharepoint_settings()
    browse, direct = _document_urls(GRAPH_PATH, settings)
    parsed = urlparse(browse or "")
    query = parse_qs(parsed.query)
    check("form sayfası kullanıldı", parsed.path.endswith("/Shared%20Documents/Forms/AllItems.aspx"), parsed.path)
    check(
        "id sunucuya göreli dosya yolu",
        query.get("id", [""])[0]
        == "/sites/AGIPulse/Shared Documents/Faruk Bey Agent Data/EKA-Partial-Data/"
        "Cybersec/2014/Why senior leaders are the front line.msg",
        query.get("id", [""])[0],
    )
    check(
        "parent üst klasör yolu",
        query.get("parent", [""])[0]
        == "/sites/AGIPulse/Shared Documents/Faruk Bey Agent Data/EKA-Partial-Data/"
        "Cybersec/2014",
        query.get("parent", [""])[0],
    )
    check("eğik çizgiler %2F olarak kodlandı", "%2Fsites%2FAGIPulse" in (browse or ""), str(browse))
    check("doğrudan adres indirme adresidir", direct == DIRECT_URL, str(direct))
    check(
        "kitaplık adı değişince adres de değişir",
        "/Belgeler/Forms/AllItems.aspx"
        in (
            _document_urls(
                GRAPH_PATH,
                Settings(
                    sharepoint_site_url="https://eczacibasi.sharepoint.com/sites/AGIPulse",
                    sharepoint_doc_library="Belgeler",
                ),
            )[0]
            or ""
        ),
    )
    check(
        "tam adres veren indekste iki adres de aynı",
        _document_urls("https://sp/a.pdf", settings) == ("https://sp/a.pdf", "https://sp/a.pdf"),
    )
    browse_from_file, direct_from_file = _document_urls(DIRECT_URL, settings)
    check(
        "tam dosya adresi tarayıcıda açan adrese çevrildi",
        browse_from_file == BROWSE_URL,
        str(browse_from_file),
    )
    check("tam dosya adresinin indirme adresi korundu", direct_from_file == DIRECT_URL)
    check(
        "AllItems adresi browse olarak kaldı",
        _document_urls(BROWSE_URL, settings)[0] == BROWSE_URL,
    )


def test_source_serialization() -> None:
    print("\n4c) Kaynak dokümanın JSON'a çevrilmesi")
    doc = SourceDoc(
        ordinal=3,
        doc_id="uid-3",
        title="Rapor.pdf",
        content="İçerik",
        url=BROWSE_URL,
        browse_url=BROWSE_URL,
        download_url=DIRECT_URL,
        snippet="özet",
        score=0.5,
        reranker_score=2.2,
        last_modified="01.02.2024",
        file_type="PDF",
        chunk_count=3,
        extra={"yol": GRAPH_PATH},
    )
    restored = SourceDoc.from_dict(json.loads(json.dumps(doc.to_dict(), ensure_ascii=False)))
    check("tur çevriminde kaynak korundu", restored == doc, str(restored))
    check("birincil bağlantı browse adresi", restored.open_url == BROWSE_URL)
    check("indirme adresi ayrı kaldı", restored.download_url == DIRECT_URL)
    check(
        "eksik alanlar varsayılana düştü",
        SourceDoc.from_dict({"title": "x", "bilinmeyen": 1}).chunk_count == 1,
    )
    decorated = apply_sharepoint_links(
        SourceDoc(ordinal=1, doc_id="d", title="x.msg", content="", url=DIRECT_URL),
        sharepoint_settings(),
    )
    check("Foundry atıf adresi browse'a çevrildi", decorated.browse_url == BROWSE_URL)
    check("Foundry atıf indirme adresi korundu", decorated.download_url == DIRECT_URL)
    check("browse modunda birincil adres AllItems", decorated.url == BROWSE_URL)


def test_history_store() -> None:
    print("\n4d) Sohbet geçmişi (SQLite)")
    with tempfile.TemporaryDirectory() as folder:
        store = ChatStore(Path(folder) / "alt" / "chats.db")
        check("veritabanı klasörü oluşturuldu", store.path.exists(), str(store.path))

        title = title_from_question("Satın alma onay limiti nedir ve kim onaylar?")
        conversation_id = store.create_conversation(title)
        store.append_message(
            conversation_id, {"role": "user", "content": "Satın alma onay limiti nedir?"}
        )
        store.append_message(
            conversation_id,
            {
                "role": "assistant",
                "content": "Limit 50.000 TL'dir [1].",
                "sources": [
                    SourceDoc(
                        ordinal=1,
                        doc_id="a",
                        title="Prosedür.pdf",
                        content="Limit 50.000 TL.",
                        url=BROWSE_URL,
                        browse_url=BROWSE_URL,
                        download_url=DIRECT_URL,
                    )
                ],
                "cited": {1},
                "meta": {"motor": "direct", "doküman": 1},
                "failed": False,
            },
        )

        messages = store.load_messages(conversation_id)
        check("iki mesaj geri yüklendi", len(messages) == 2, str(len(messages)))
        check("kullanıcı sorusu korundu", messages[0]["content"] == "Satın alma onay limiti nedir?")
        check("atıflar küme olarak döndü", messages[1]["cited"] == {1}, str(messages[1]["cited"]))
        check(
            "kaynak nesnesi geri yüklendi",
            isinstance(messages[1]["sources"][0], SourceDoc)
            and messages[1]["sources"][0].download_url == DIRECT_URL,
        )
        check("meta korundu", messages[1]["meta"].get("motor") == "direct")

        listed = store.list_conversations()
        check("sohbet listelendi", len(listed) == 1 and listed[0].title == title, str(listed))
        check("mesaj sayısı sayıldı", listed[0].message_count == 2, str(listed[0].message_count))

        store.set_thread_id(conversation_id, "thread_abc")
        check("thread saklandı", store.get_thread_id(conversation_id) == "thread_abc")

        second = store.create_conversation("İkinci sohbet")
        store.append_message(second, {"role": "user", "content": "merhaba"})
        check(
            "en son güncellenen sohbet başta",
            store.list_conversations()[0].id == second,
            str([c.id for c in store.list_conversations()]),
        )

        store.delete_conversation(conversation_id)
        check("sohbet silindi", [c.id for c in store.list_conversations()] == [second])
        check("silinen sohbetin mesajları gitti", store.load_messages(conversation_id) == [])
        store.close()


def test_title_from_question() -> None:
    print("\n4e) Sohbet başlığı üretimi")
    check("kısa soru aynen başlık olur", title_from_question("İzin kaç gün?") == "İzin kaç gün?")
    long_question = "Bilgi güvenliği politikasında " + "uzun " * 30 + "madde"
    title = title_from_question(long_question)
    check("uzun başlık kırpıldı", len(title) <= 61, f"{len(title)}: {title}")
    check("kırpma işareti eklendi", title.endswith("…"), title)
    check("boş soruda varsayılan başlık", title_from_question("   ") == "Yeni sohbet")
    check("satır sonları temizlendi", title_from_question("a\n\n b") == "a b")


def test_override() -> None:
    print("\n5) .env ile alan eşlemesini sabitleme")
    settings = Settings(field_content="metadata_spo_item_name", field_url="metadata_spo_item_path")
    schema = build_schema(sharepoint_index(), settings)
    check("içerik override çalıştı", schema.content_field == "metadata_spo_item_name")
    check("URL override çalıştı", schema.url_field == "metadata_spo_item_path")


def test_citations() -> None:
    print("\n6) Atıf ayrıştırma ve linkleme")
    sources = [
        SourceDoc(ordinal=1, doc_id="a", title="Satın Alma Prosedürü.pdf", content="", url="https://sp/a.pdf"),
        SourceDoc(ordinal=2, doc_id="b", title="Limitler.xlsx", content="", url=None),
    ]
    answer = "Onay limiti 50.000 TL'dir [1]. Detay tabloda yer alır [1, 2]. Kaynaksız cümle [7]."
    cited = parse_cited_ordinals(answer)
    check("atıflar bulundu", cited == {1, 2, 7}, str(cited))

    linked = linkify_citations(answer, sources)
    check("linkli atıf üretildi", "[[1]](https://sp/a.pdf)" in linked, linked)
    check("URL'siz kaynak düz metin kaldı", "[2]" in linked and "[[2]](" not in linked)
    check("çoklu atıf ayrıştı", "[[1]](https://sp/a.pdf)[2]" in linked, linked)
    check("bilinmeyen atıf korundu", "[7]" in linked)
    browse_source = SourceDoc(
        ordinal=1,
        doc_id="a",
        title="a.msg",
        content="",
        url=DIRECT_URL,
        browse_url=BROWSE_URL,
        download_url=DIRECT_URL,
    )
    check(
        "atıf birincil olarak SharePoint arayüzüne gider",
        f"[[1]]({BROWSE_URL})" in linkify_citations("kaynak [1]", [browse_source]),
    )


def test_prompt() -> None:
    print("\n7) Bağlam biçimlendirme")
    sources = [
        SourceDoc(
            ordinal=1,
            doc_id="a",
            title="Izin Yonetmeligi.docx",
            content="Yillik izin 14 gundur.",
            url="https://sp/izin.docx",
            file_type="Word",
            last_modified="01.02.2024",
        )
    ]
    message = build_user_message("Yıllık izin kaç gün?", sources)
    check("kaynak numarası bağlamda var", "[1] Doküman: Izin Yonetmeligi.docx" in message)
    check("adres bağlamda var", "https://sp/izin.docx" in message)
    check("içerik bağlamda var", "Yillik izin 14 gundur." in message)
    check("soru bağlamda var", "SORU: Yıllık izin kaç gün?" in message)
    check("alakasız kaynak kuralı var", "Alakasız kaynakları kullanma" in SYSTEM_PROMPT)
    check("emin değilsen kuralı var", "bu kaynakta yok" in SYSTEM_PROMPT)


def test_normalizers() -> None:
    print("\n8) Normalizasyon yardımcıları")
    check(
        "MIME türü etiketlendi",
        _friendly_file_type("application/pdf", None) == "PDF",
        str(_friendly_file_type("application/pdf", None)),
    )
    check(
        "uzantıdan tür çıkarıldı",
        _friendly_file_type(None, "Rapor.xlsx") == "Excel",
        str(_friendly_file_type(None, "Rapor.xlsx")),
    )
    check("tarih biçimlendi", _format_date("2024-03-12T10:00:00Z") == "12.03.2024", str(_format_date("2024-03-12T10:00:00Z")))
    check("boşluklar sadeleşti", _clean("  a\n\n b  ") == "a b")
    check("kırpma çalıştı", _clean("abcdefghij", 5).startswith("abcde"))
    check(
        "tam URL korundu",
        _resolve_url("https://sp/a.pdf", Settings()) == "https://sp/a.pdf",
    )
    check(
        "göreli yol site adresiyle birleşti",
        _resolve_url("Shared/a.pdf", Settings(sharepoint_site_url="https://x.sharepoint.com/sites/s"))
        == "https://x.sharepoint.com/sites/s/Shared/a.pdf",
    )
    check("adres yoksa None", _resolve_url("Shared/a.pdf", Settings()) is None)


def test_config_validation() -> None:
    print("\n9) Ayar doğrulama")
    empty = Settings()
    check("direct için eksikler listelendi", "AZURE_SEARCH_ENDPOINT" in empty.missing_for("direct"))
    check("foundry için eksikler listelendi", "FOUNDRY_PROJECT_ENDPOINT" in empty.missing_for("foundry"))
    ready = Settings(
        search_endpoint="https://s.search.windows.net",
        search_index="i",
        aoai_endpoint="https://a.openai.azure.com",
        aoai_chat_deployment="gpt-4o",
    )
    check("tam yapılandırma temiz", ready.missing_for("direct") == [])
    check("anahtar yoksa Entra ID", ready.search_uses_entra and ready.aoai_uses_entra)
    check("bağlantı biçimi varsayılanı browse", empty.sharepoint_link_mode == "browse")
    check("teknik sekmeler varsayılan olarak kapalı", empty.show_advanced_tabs is False)
    check("web çekme varsayılan açık", empty.web_fetch_enabled is True)
    check("web çekme soru limiti 6", empty.web_fetch_max_per_question == 6)
    check("varsayılan kaynak üst sınırı 12", empty.top_k == 12, str(empty.top_k))
    check("varsayılan aday havuzu 48", empty.candidate_count == 48, str(empty.candidate_count))
    check("varsayılan reranker eşiği 2.0", empty.min_reranker_score == 2.0, str(empty.min_reranker_score))
    check("auth varsayılanı boş", empty.auth_user == "" and empty.auth_password == "")


def test_imports() -> None:
    print("\n10) Modül içe aktarma")
    import src.auth  # noqa: F401
    import src.engines.direct  # noqa: F401
    import src.engines.foundry  # noqa: F401
    import src.history  # noqa: F401
    import src.llm  # noqa: F401
    import src.ui  # noqa: F401
    import src.webcontent  # noqa: F401

    check("tüm modüller içe aktarıldı", True)

    from src.engines import build_engine

    check("fabrika fonksiyonu mevcut", callable(build_engine))

    from src.engines.foundry import FoundryEngine

    engine = FoundryEngine.__new__(FoundryEngine)
    engine._thread_id = "t1"
    engine.reset_thread()
    check("Foundry thread sıfırlandı", engine.thread_id is None)
    engine.set_thread("t2")
    check("Foundry thread atandı", engine.thread_id == "t2")
    engine.set_thread(None)
    check("Foundry boş thread yeni sohbet", engine.thread_id is None)


def test_vector_index_target_name() -> None:
    print("\n11) Vektör indeks hedef adı")
    import importlib.util

    path = Path(__file__).resolve().parent / "build_vector_index.py"
    spec = importlib.util.spec_from_file_location("build_vector_index_mod", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    got = mod.default_target_name("fe-partial-data-index")
    check(
        "fe-partial-data-index -> fe-partial-data-vector-index",
        got == "fe-partial-data-vector-index",
        got,
    )
    check(
        "fe-partial-data-index-vector-index üretilmedi",
        got != "fe-partial-data-index-vector-index",
    )


def test_webcontent() -> None:
    print("\n12) Web içerik çekme (paywall / SSRF / ince içerik)")
    urls = extract_urls(
        "Okuyun: https://example.com/a. Ayrıca http://news.org/b) ve tekrar "
        "https://example.com/a"
    )
    check(
        "URL'ler çıkarıldı ve tekilleştirildi",
        urls == ["https://example.com/a", "http://news.org/b"],
        str(urls),
    )

    check("paywall wsj atlanır", is_paywall_url("https://www.wsj.com/articles/x"))
    check("paywall nytimes atlanır", is_paywall_url("https://nytimes.com/2024/a"))
    check("paywall bloomberg atlanır", is_paywall_url("https://www.bloomberg.com/news/x"))
    check("paywall medium atlanır", is_paywall_url("https://medium.com/p/abc"))
    check("paywall linkedin atlanır", is_paywall_url("https://www.linkedin.com/posts/x"))
    check("açık site paywall değil", not is_paywall_url("https://example.com/page"))
    check("PDF indirme atlanır", is_download_url("https://example.com/report.pdf"))
    check("HTML sayfa indirme değil", not is_download_url("https://example.com/article"))

    check("localhost SSRF", is_ssrf_unsafe("http://localhost/secret", resolve=False))
    check("127.0.0.1 SSRF", is_ssrf_unsafe("http://127.0.0.1/", resolve=False))
    check("özel IPv4 SSRF", is_ssrf_unsafe("http://192.168.1.10/x", resolve=False))
    check("10.x SSRF", is_ssrf_unsafe("http://10.0.0.5/x", resolve=False))
    check("link-local metadata SSRF", is_ssrf_unsafe("http://169.254.169.254/", resolve=False))
    check("file şeması SSRF", is_ssrf_unsafe("file:///etc/passwd", resolve=False))
    check("ftp şeması SSRF", is_ssrf_unsafe("ftp://example.com/a", resolve=False))
    check(
        "genel host isimle SSRF değil",
        not is_ssrf_unsafe("https://example.com/article", resolve=False),
    )

    thin = "Makaleyi okuyun: https://example.com/article"
    fat = ("A" * 900) + " https://example.com/article"
    link_heavy = "  https://a.example/x   https://b.example/y  \n"
    check("ince içerik + URL tetikler", is_thin_or_link_heavy(thin))
    check("uzun gövde tetiklemez", not is_thin_or_link_heavy(fat))
    check("yalnızca link tetikler", is_thin_or_link_heavy(link_heavy))
    check("URL yoksa tetiklemez", not is_thin_or_link_heavy("Kısa e-posta gövdesi."))

    markup = (
        "<html><head><script>secret=1</script><style>p{}</style></head>"
        "<body><h1>Başlık</h1><p>Paragraf metni</p></body></html>"
    )
    visible = html_to_text(markup)
    check("script atıldı", "secret" not in visible, visible)
    check("görünür metin kaldı", "Başlık" in visible and "Paragraf metni" in visible, visible)

    def fake_fetch(url: str) -> str:
        return f"Çekilen gövde ({url})"

    doc = SourceDoc(
        ordinal=1,
        doc_id="msg-1",
        title="bulten.msg",
        content="İlginç yazı: https://example.com/open",
        file_type="E-posta",
    )
    enriched, stats = enrich_web_sources([doc], Settings(), fetch_fn=fake_fetch)
    check("web metni eklendi", "Çekilen gövde" in enriched[0].content, enriched[0].content)
    check("web ayırıcı eklendi", "--- Web kaynağı (https://example.com/open) ---" in enriched[0].content)
    check("extra.web doldu", bool((enriched[0].extra or {}).get("web")), str(enriched[0].extra))
    check("istatistik çekilen=1", stats.get("web çekilen") == 1, str(stats))

    paywalled = SourceDoc(
        ordinal=2,
        doc_id="msg-2",
        title="wsj.msg",
        content="Wall Street: https://www.wsj.com/articles/secret",
    )
    skipped, skip_stats = enrich_web_sources([paywalled], Settings(), fetch_fn=fake_fetch)
    check("paywall içeriği eklenmedi", "Çekilen gövde" not in skipped[0].content, skipped[0].content)
    check("paywall atlandı sayıldı", skip_stats.get("web atlanan") == 1, str(skip_stats))

    ssrf_doc = SourceDoc(
        ordinal=3,
        doc_id="msg-3",
        title="local.msg",
        content="İç servis: http://127.0.0.1/admin",
    )
    ssrf_out, ssrf_stats = enrich_web_sources([ssrf_doc], Settings(), fetch_fn=fake_fetch)
    check("SSRF içeriği eklenmedi", "Çekilen gövde" not in ssrf_out[0].content)
    check("SSRF atlandı sayıldı", ssrf_stats.get("web atlanan") == 1, str(ssrf_stats))

    fat_doc = SourceDoc(
        ordinal=4,
        doc_id="msg-4",
        title="uzun.msg",
        content=("Gövde " * 200) + "https://example.com/open",
    )
    fat_out, fat_stats = enrich_web_sources([fat_doc], Settings(), fetch_fn=fake_fetch)
    check("uzun içerikte fetch yok", fat_stats.get("web çekilen") == 0, str(fat_stats))
    check("uzun içerik değişmedi", "Çekilen gövde" not in fat_out[0].content)

    disabled = enrich_web_sources(
        [doc], Settings(web_fetch_enabled=False), fetch_fn=fake_fetch
    )[1]
    check("kapalıyken çekilmez", disabled.get("web çekme") == "kapalı", str(disabled))


def main() -> int:
    print("=" * 62)
    print(" Smoke test - Azure bağlantısı gerektirmez")
    print("=" * 62)

    test_sharepoint_schema()
    test_vectorized_schema()
    test_snippet_schema()
    test_snippet_vector_schema()
    test_candidate_count()
    test_relevance_filter()
    test_auth_credentials()
    test_sharepoint_links()
    test_browse_url_parts()
    test_source_serialization()
    test_history_store()
    test_title_from_question()
    test_override()
    test_citations()
    test_prompt()
    test_normalizers()
    test_config_validation()
    test_imports()
    test_vector_index_target_name()
    test_webcontent()

    print("\n" + "=" * 62)
    if failures:
        print(f" {len(failures)} test BAŞARISIZ: " + ", ".join(failures))
        return 1
    print(" Tüm testler geçti.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
