"""Arayüzün uçtan uca testi (Azure'a bağlanmadan).

Streamlit'in AppTest çatısını kullanır: app.py gerçekten çalıştırılır, ancak
Azure çağrıları sahte (fake) nesnelerle değiştirilir.

    python scripts/app_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import src.config as config_module  # noqa: E402
import src.engines.direct as direct_module  # noqa: E402
import src.schema as schema_module  # noqa: E402

# Test, ortam değişkenlerini kendisi kurar. Gerçek bir .env varsa sonuçları
# bozacağı için dosya okuma devre dışı bırakılır.
config_module.load_env = lambda: None

from src.models import SourceDoc  # noqa: E402
from src.schema import IndexSchema  # noqa: E402

APP = str(ROOT / "app.py")
failures: list[str] = []

# Sohbet geçmişi gerçek veritabanına yazmasın.
CHAT_DB_DIR = Path(tempfile.mkdtemp(prefix="fba-chat-test-"))
os.environ["APP_CHAT_DB_PATH"] = str(CHAT_DB_DIR / "chats.db")

QUESTION = "Satın alma onay limiti nedir?"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[ OK ] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        failures.append(label)


def texts(at: AppTest) -> str:
    """Sayfadaki tüm metinsel çıktıyı tek bir dizede toplar."""
    chunks: list[str] = []
    for collection in ("markdown", "error", "warning", "info", "success", "caption"):
        try:
            chunks.extend(str(el.value) for el in getattr(at, collection))
        except Exception:
            pass
    return "\n".join(chunks)


def sidebar_button_labels(at: AppTest) -> list[str]:
    return [str(button.label) for button in at.sidebar.button]


def click_sidebar(at: AppTest, label: str) -> bool:
    """Etiketi verilen yan menü düğmesine basar."""
    for button in at.sidebar.button:
        if str(button.label) == label:
            button.click().run()
            return True
    return False


# ----------------------------------------------------------------------
# Sahte nesneler
# ----------------------------------------------------------------------
def fake_schema() -> IndexSchema:
    return IndexSchema(
        index_name="spo-index",
        key_field="metadata_spo_site_library_item_id",
        content_field="content",
        title_field="metadata_spo_item_name",
        url_field="metadata_spo_item_weburi",
        last_modified_field="metadata_spo_item_last_modified",
        file_type_field="metadata_spo_item_content_type",
        vector_fields=["contentVector"],
        has_vectorizer=True,
        semantic_config="semantic-1",
        selectable_fields=["content", "metadata_spo_item_name"],
        filterable_fields=["metadata_spo_item_content_type"],
        facetable_fields=["metadata_spo_item_content_type"],
        all_fields={"content": "Edm.String", "metadata_spo_item_name": "Edm.String"},
        warnings=[],
    )


BROWSE_URL = (
    "https://contoso.sharepoint.com/sites/kalite/Shared%20Documents/Forms/AllItems.aspx"
    "?id=%2Fsites%2Fkalite%2FShared%20Documents%2Fsatinalma.pdf"
    "&parent=%2Fsites%2Fkalite%2FShared%20Documents"
)
DOWNLOAD_URL = "https://contoso.sharepoint.com/sites/kalite/satinalma.pdf"

SAMPLE_SOURCES = [
    SourceDoc(
        ordinal=1,
        doc_id="1",
        title="Satin Alma Prosedürü.pdf",
        content="Onay limiti 50.000 TL'dir.",
        url=BROWSE_URL,
        browse_url=BROWSE_URL,
        download_url=DOWNLOAD_URL,
        snippet="Onay limiti 50.000 TL'dir.",
        score=0.82,
        reranker_score=2.71,
        last_modified="12.03.2024",
        file_type="PDF",
        chunk_count=2,
    ),
    SourceDoc(
        ordinal=2,
        doc_id="2",
        title="Yetki Tablosu.xlsx",
        content="Genel müdür onayı 100.000 TL üzeri.",
        url="https://contoso.sharepoint.com/sites/kalite/yetki.xlsx",
        snippet="Genel müdür onayı 100.000 TL üzeri.",
        score=0.64,
        reranker_score=1.90,
        last_modified="02.01.2024",
        file_type="Excel",
    ),
]

ANSWER = (
    "Satın alma onay limiti 50.000 TL'dir [1]. Bu tutarın üzerindeki talepler için "
    "genel müdür onayı gerekir [2]."
)


class FakeEngine:
    name = "direct"
    supports_streaming = True
    thread_id = "thread-test"

    def __init__(self, settings, schema=None):
        self.settings = settings
        self.schema = schema or fake_schema()

    def start_stream(self, question, history=None, options=None):
        debug = {"arama tipi": "hibrit + semantik", "referans sayısı": 2}
        return iter([ANSWER[:40], ANSWER[40:]]), SAMPLE_SOURCES, debug

    def health(self):
        return {"Motor": "sahte motor", "İndeks": "spo-index"}

    def close(self):
        pass

    def reset_thread(self) -> None:
        self.thread_id = None

    def set_thread(self, thread_id):
        self.thread_id = thread_id


def configure_env(show_advanced_tabs: bool = False) -> None:
    os.environ.update(
        {
            "RAG_BACKEND": "direct",
            "AZURE_SEARCH_ENDPOINT": "https://test.search.windows.net",
            "AZURE_SEARCH_INDEX": "spo-index",
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-4o",
            "AZURE_SEARCH_API_KEY": "dummy",
            "AZURE_OPENAI_API_KEY": "dummy",
            "APP_SHOW_ADVANCED_TABS": "1" if show_advanced_tabs else "0",
            "RAG_TOP_K": "12",
            "RAG_CANDIDATE_COUNT": "48",
            "RAG_MIN_RERANKER_SCORE": "2.0",
            "APP_AUTH_USER": "",
            "APP_AUTH_PASSWORD": "",
        }
    )


# ----------------------------------------------------------------------
# Testler
# ----------------------------------------------------------------------
def test_unconfigured() -> None:
    print("\n1) Yapılandırma yoksa kurulum ekranı")
    for key in [
        "AZURE_SEARCH_ENDPOINT",
        "AZURE_SEARCH_INDEX",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_CHAT_DEPLOYMENT",
        "RAG_BACKEND",
        "APP_AUTH_USER",
        "APP_AUTH_PASSWORD",
    ]:
        os.environ.pop(key, None)
    os.environ["APP_AUTH_USER"] = ""
    os.environ["APP_AUTH_PASSWORD"] = ""
    st.cache_resource.clear()

    at = AppTest.from_file(APP, default_timeout=60).run()
    check("çalışma zamanı hatası yok", not at.exception, str(at.exception))
    page = texts(at)
    check("kurulum uyarısı gösterildi", "yapılandırılmamış" in page, page[:200])
    check("eksik ayar adı listelendi", "AZURE_SEARCH_ENDPOINT" in page)
    check("rol yönlendirmesi var", "Search Index Data Reader" in page)


def test_configured_chat() -> None:
    print("\n2) Yapılandırılmış uygulama ve soru-cevap akışı")
    configure_env()
    original_discover = schema_module.discover_schema
    original_engine = direct_module.DirectEngine
    schema_module.discover_schema = lambda settings, index_name=None: fake_schema()
    direct_module.DirectEngine = FakeEngine
    st.cache_resource.clear()

    try:
        at = AppTest.from_file(APP, default_timeout=60).run()
        check("çalışma zamanı hatası yok", not at.exception, str(at.exception))

        page = texts(at)
        check("başlık göründü", "Dipnot" in page, page[:200])
        check("kurulum ekranı gösterilmedi", "yapılandırılmamış" not in page)
        check("sohbet giriş alanı var", len(at.chat_input) > 0)

        # Sade yan menü: yeni sohbet önde, teknik ayarlar kapalı bölümde.
        labels = sidebar_button_labels(at)
        check("yeni sohbet düğmesi var", any("Yeni sohbet" in label for label in labels), str(labels))
        expanders = [exp for exp in at.sidebar.expander]
        check("gelişmiş ayarlar bölümü var", len(expanders) == 1, str([e.label for e in expanders]))
        check(
            "gelişmiş ayarlar kapalı geliyor",
            bool(expanders) and not expanders[0].proto.expanded,
        )
        check("motor seçimi gelişmiş ayarlarda", len(at.sidebar.radio) > 0)
        check(
            "eski doküman kaydırıcısı yok",
            not any("Kaç doküman incelensin" in str(s.label) for s in at.sidebar.slider),
            str([s.label for s in at.sidebar.slider]),
        )
        source_sliders = [
            s for s in at.sidebar.slider if "kaynak" in str(s.label).lower()
        ]
        check("kaynak üst sınır kaydırıcısı var", bool(source_sliders), str([s.label for s in at.sidebar.slider]))
        check(
            "kaynak üst sınırı varsayılanı 12",
            bool(source_sliders) and int(source_sliders[0].value) == 12,
            str(source_sliders[0].value) if source_sliders else "yok",
        )
        slider_max = getattr(getattr(source_sliders[0], "proto", None), "max", None) if source_sliders else None
        check("kaynak kaydırıcısı 30'a kadar", slider_max == 30, str(slider_max))

        semantic_toggles = [t for t in at.sidebar.toggle if "akıllı sırala" in t.label]
        check("akıllı sıralama anahtarı aktif", bool(semantic_toggles) and semantic_toggles[0].value)
        vector_toggles = [t for t in at.sidebar.toggle if "Benzer anlamlı" in t.label]
        check("vektör anahtarı açık", bool(vector_toggles) and vector_toggles[0].value)
        disabled_flag = getattr(vector_toggles[0], "disabled", None) if vector_toggles else None
        if disabled_flag is None and vector_toggles:
            disabled_flag = getattr(getattr(vector_toggles[0], "proto", None), "disabled", False)
        check("vektör anahtarı etkin", bool(vector_toggles) and not disabled_flag, str(disabled_flag))
        check("jargon sadeleşti", not any("reranker" in str(s.label) for s in at.sidebar.slider))

        # Teknik sekmeler varsayılan olarak gizli.
        check("sekme çubuğu gizlendi", len(at.tabs) == 0, str(len(at.tabs)))
        check("kaynak tarayıcı gizli", "Yanıt üretmeden yalnızca indeksi sorgular" not in page)

        # Soru gönder
        at.chat_input[0].set_value(QUESTION).run()
        check("soru sonrası hata yok", not at.exception, str(at.exception))

        page = texts(at)
        check("soru ekranda", QUESTION in page)
        check("yanıt metni ekranda", "50.000 TL" in page, page[-400:])
        check("atıf linke çevrildi", f"[[1]]({BROWSE_URL})" in page, page[-500:])
        check("referans başlığı gösterildi", "Satin Alma Prosedürü.pdf" in page)
        check("ikinci referans da listelendi", "Yetki Tablosu.xlsx" in page)
        check(
            "her iki kaynak 'kullanıldı' işaretlendi",
            page.count("pill pill-used") == 2,
            f"sayı={page.count('pill pill-used')}",
        )
        check("kaynak meta bilgisi var", "reranker 2.71" in page)
        check("parça sayısı gösterildi", "2 parça" in page)
        check(
            "indirme bağlantısı ikincil seçenek olarak sunuldu",
            "Dosyayı indir" in page and DOWNLOAD_URL.replace("&", "&amp;") in page,
            page[-600:],
        )
        link_buttons = at.get("link_button")
        open_labels = [str(getattr(el, "label", "")) for el in link_buttons]
        open_urls = [str(getattr(el, "url", "")) for el in link_buttons]
        check(
            "SharePoint'te aç düğmesi var",
            any("SharePoint'te aç" in label for label in open_labels),
            str(open_labels),
        )
        check(
            "SharePoint'te aç browse adresine gider",
            BROWSE_URL in open_urls,
            str(open_urls),
        )

        # Sohbet kaydedildi mi?
        labels = sidebar_button_labels(at)
        check("sohbet geçmişte listelendi", any(QUESTION in label for label in labels), str(labels))

        # İkinci tur: geçmiş korunuyor mu?
        at.chat_input[0].set_value("Peki üst limit?").run()
        check("ikinci turda hata yok", not at.exception, str(at.exception))
        page = texts(at)
        check("ilk soru geçmişte kaldı", QUESTION in page)
        check("ikinci soru göründü", "Peki üst limit?" in page)
        check(
            "aynı sohbet ikinci kez açılmadı",
            sum(1 for label in sidebar_button_labels(at) if QUESTION in label) == 1,
            str(sidebar_button_labels(at)),
        )
    finally:
        schema_module.discover_schema = original_discover
        direct_module.DirectEngine = original_engine


def test_history_reload() -> None:
    print("\n3) Yeni oturumda geçmiş sohbetin yüklenmesi")
    configure_env()
    original_discover = schema_module.discover_schema
    original_engine = direct_module.DirectEngine
    schema_module.discover_schema = lambda settings, index_name=None: fake_schema()
    direct_module.DirectEngine = FakeEngine
    st.cache_resource.clear()

    try:
        # Sayfa yenilenmiş gibi tamamen yeni bir oturum.
        at = AppTest.from_file(APP, default_timeout=60).run()
        check("çalışma zamanı hatası yok", not at.exception, str(at.exception))
        check("yeni oturumda sohbet boş", QUESTION not in texts(at), texts(at)[:200])

        history_label = next(
            (label for label in sidebar_button_labels(at) if QUESTION in label), ""
        )
        check("geçmiş sohbet yan menüde", bool(history_label), str(sidebar_button_labels(at)))

        check("geçmiş sohbet açıldı", click_sidebar(at, history_label))
        check("yükleme sonrası hata yok", not at.exception, str(at.exception))

        page = texts(at)
        check("eski soru geri geldi", QUESTION in page, page[:300])
        check("eski yanıt geri geldi", "50.000 TL" in page)
        check("kaynak kartı geri geldi", "Satin Alma Prosedürü.pdf" in page)
        check("atıflar korundu", f"[[1]]({BROWSE_URL})" in page, page[-400:])
        # İki tur yüklendiği için her turun iki kaynağı işaretli gelir.
        check(
            "kullanıldı işareti korundu",
            page.count("pill pill-used") == 4,
            f"sayı={page.count('pill pill-used')}",
        )
        check("ikinci tur da yüklendi", "Peki üst limit?" in page)

        # Yeni sohbet ekranı temizler, geçmiş listede kalır.
        new_chat_label = next(
            (label for label in sidebar_button_labels(at) if "Yeni sohbet" in label), ""
        )
        check("yeni sohbet düğmesine basıldı", click_sidebar(at, new_chat_label))
        page = texts(at)
        check("ekran temizlendi", QUESTION not in page, page[:300])
        check(
            "geçmiş kaybolmadı",
            any(QUESTION in label for label in sidebar_button_labels(at)),
            str(sidebar_button_labels(at)),
        )

        delete_label = next(
            (label for label in sidebar_button_labels(at) if "🗑" in label), ""
        )
        check("sil düğmesi var", bool(delete_label), str(sidebar_button_labels(at)))
        check("sohbet silindi", click_sidebar(at, delete_label))
        check("silme sonrası hata yok", not at.exception, str(at.exception))
        check(
            "silinen sohbet listeden çıktı",
            not any(QUESTION in label for label in sidebar_button_labels(at)),
            str(sidebar_button_labels(at)),
        )
    finally:
        schema_module.discover_schema = original_discover
        direct_module.DirectEngine = original_engine


def test_advanced_tabs() -> None:
    print("\n4) APP_SHOW_ADVANCED_TABS ile teknik sekmeler")
    configure_env(show_advanced_tabs=True)
    original_discover = schema_module.discover_schema
    original_engine = direct_module.DirectEngine
    schema_module.discover_schema = lambda settings, index_name=None: fake_schema()
    direct_module.DirectEngine = FakeEngine
    st.cache_resource.clear()

    try:
        at = AppTest.from_file(APP, default_timeout=60).run()
        check("çalışma zamanı hatası yok", not at.exception, str(at.exception))
        check("üç sekme göründü", len(at.tabs) == 3, str(len(at.tabs)))
        page = texts(at)
        check("kaynak tarayıcı açıklaması var", "Yanıt üretmeden yalnızca indeksi sorgular" in page)
        check("tanılama tablosu doldu", len(at.table) > 0)
    finally:
        schema_module.discover_schema = original_discover
        direct_module.DirectEngine = original_engine
        configure_env(show_advanced_tabs=False)


def test_schema_error() -> None:
    print("\n5) İndekse ulaşılamadığında hata ekranı")

    def boom(settings, index_name=None):
        raise RuntimeError("(403) Forbidden")

    original = schema_module.discover_schema
    schema_module.discover_schema = boom
    st.cache_resource.clear()
    try:
        at = AppTest.from_file(APP, default_timeout=60).run()
        check("çökme yok", not at.exception, str(at.exception))
        page = texts(at)
        check("hata mesajı anlaşılır", "ulaşılamadı" in page, page[:300])
        check("403 detayı gösterildi", "Forbidden" in page)
    finally:
        schema_module.discover_schema = original


def test_login_gate() -> None:
    print("\n6) Login zorunluysa sohbet çizilmez; yanlış şifre reddedilir")
    configure_env()
    os.environ["APP_AUTH_USER"] = "admin"
    os.environ["APP_AUTH_PASSWORD"] = "test-password"
    original_discover = schema_module.discover_schema
    original_engine = direct_module.DirectEngine
    schema_module.discover_schema = lambda settings, index_name=None: fake_schema()
    direct_module.DirectEngine = FakeEngine
    st.cache_resource.clear()

    try:
        at = AppTest.from_file(APP, default_timeout=60).run()
        check("çalışma zamanı hatası yok", not at.exception, str(at.exception))
        page = texts(at)
        check("Dipnot başlığı login'de", "Dipnot" in page, page[:200])
        check("sohbet giriş alanı yok", len(at.chat_input) == 0)
        check("giriş formu var", any("Giriş" in str(b.label) for b in at.button), str([b.label for b in at.button]))

        user_box = next((t for t in at.text_input if "Kullanıcı" in str(t.label)), None)
        pass_box = next((t for t in at.text_input if "Şifre" in str(t.label)), None)
        check("kullanıcı alanı var", user_box is not None)
        check("şifre alanı var", pass_box is not None)
        if user_box is not None and pass_box is not None:
            user_box.set_value("admin")
            pass_box.set_value("yanlis")
            login_btn = next((b for b in at.button if "Giriş" in str(b.label)), None)
            if login_btn is not None:
                login_btn.click().run()
            check("yanlış şifre sonrası hata yok", not at.exception, str(at.exception))
            page = texts(at)
            check("yanlış şifre reddedildi", "hatalı" in page, page[-300:])
            check("sohbet hâlâ kapalı", len(at.chat_input) == 0)
    finally:
        schema_module.discover_schema = original_discover
        direct_module.DirectEngine = original_engine
        os.environ["APP_AUTH_USER"] = ""
        os.environ["APP_AUTH_PASSWORD"] = ""


def main() -> int:
    print("=" * 62)
    print(" Arayüz testi (AppTest) - Azure bağlantısı gerektirmez")
    print("=" * 62)

    try:
        test_unconfigured()
        test_configured_chat()
        test_history_reload()
        test_advanced_tabs()
        test_schema_error()
        test_login_gate()
    finally:
        st.cache_resource.clear()
        shutil.rmtree(CHAT_DB_DIR, ignore_errors=True)

    print("\n" + "=" * 62)
    if failures:
        print(f" {len(failures)} test BAŞARISIZ: " + ", ".join(failures))
        return 1
    print(" Tüm arayüz testleri geçti.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
