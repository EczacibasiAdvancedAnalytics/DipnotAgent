"""Dipnot - Streamlit arayüzü.

Çalıştırma:  streamlit run app.py
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import streamlit as st

from src import ui
from src.auth import render_logout_button, require_login
from src.config import env_cache_token, get_settings, running_on_streamlit_cloud
from src.engines import AskOptions, build_engine
from src.history import ChatStore, title_from_question
from src.models import SourceDoc, linkify_citations, parse_cited_ordinals
from src.retrieval import Retriever
from src.schema import IndexSchema, discover_schema

logging.basicConfig(level=logging.WARNING)

SETTINGS = get_settings()
SCHEMA_CACHE_TOKEN = env_cache_token()

st.set_page_config(
    page_title=SETTINGS.app_title,
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)
require_login()


# ----------------------------------------------------------------------
# Önbelleklenen kaynaklar
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def load_schema(endpoint: str, index: str, env_token: str = "") -> IndexSchema:
    # endpoint / index / env_token önbellek anahtarıdır; keşif verilen indeks adıyla yapılır.
    return discover_schema(get_settings(), index_name=index)


@st.cache_resource(show_spinner=False)
def load_retriever(endpoint: str, index: str, env_token: str = "") -> Retriever:
    settings = get_settings()
    schema = load_schema(endpoint, index, env_token)
    embedder = None
    if settings.aoai_endpoint and settings.aoai_embedding_deployment:
        try:
            from src.llm import LlmClient

            embedder = LlmClient(settings).embedder()
        except Exception as exc:  # embedding olmadan da arama çalışır
            logging.warning("Embedding istemcisi kurulamadı: %s", exc)
    return Retriever(settings, schema, embedder=embedder)


@st.cache_resource(show_spinner=False)
def load_engine(backend: str, endpoint: str, index: str, env_token: str = ""):
    settings = get_settings()
    if backend == "direct":
        from src.engines.direct import DirectEngine

        return DirectEngine(settings, schema=load_schema(endpoint, index, env_token))
    return build_engine(settings, backend)


@st.cache_resource(show_spinner=False)
def load_store(db_path: Optional[str]) -> ChatStore:
    return ChatStore(db_path)


def reset_caches() -> None:
    load_schema.clear()
    load_retriever.clear()
    load_engine.clear()
    for key in (
        "_search_schema_sig",
        "use_vector",
        "use_semantic",
        "max_sources",
        "min_reranker",
    ):
        st.session_state.pop(key, None)


def get_store() -> Optional[ChatStore]:
    """Sohbet geçmişi deposu; açılamazsa uygulama geçmişsiz çalışmaya devam eder."""
    try:
        return load_store(SETTINGS.chat_db_path)
    except Exception as exc:
        st.session_state["_history_error"] = str(exc)
        logging.warning("Sohbet geçmişi açılamadı: %s", exc)
        return None


# ----------------------------------------------------------------------
# Durum
# ----------------------------------------------------------------------
for key, initial in {"messages": [], "conversation_id": None, "_thread_applied": None}.items():
    if key not in st.session_state:
        st.session_state[key] = initial

STORE = get_store()

ui.inject_css()
ui.header(SETTINGS.app_title, SETTINGS.app_subtitle)
if running_on_streamlit_cloud():
    st.caption(
        "Streamlit Community Cloud'da sohbet geçmişi sunucu diskinde tutulur ve "
        "uygulama yeniden başlatıldığında (güncelleme, uyku, restart) silinir."
    )


# ----------------------------------------------------------------------
# Sohbet geçmişi
# ----------------------------------------------------------------------
def start_new_chat() -> None:
    st.session_state.messages = []
    st.session_state.conversation_id = None
    engine_ref = st.session_state.get("_engine_ref")
    if engine_ref is not None and hasattr(engine_ref, "reset_thread"):
        engine_ref.reset_thread()
    st.session_state["_thread_applied"] = None


def open_conversation(conversation_id: int) -> None:
    if STORE is None:
        return
    st.session_state.messages = STORE.load_messages(conversation_id)
    st.session_state.conversation_id = conversation_id


def sync_thread(engine) -> None:
    """Foundry motorunda her sohbetin kendi thread'i olmasını sağlar."""
    conversation_id = st.session_state.get("conversation_id")
    if st.session_state.get("_thread_applied") == conversation_id:
        return
    thread_id = None
    if STORE is not None and conversation_id:
        thread_id = STORE.get_thread_id(conversation_id)
    if hasattr(engine, "set_thread"):
        engine.set_thread(thread_id)
    elif hasattr(engine, "reset_thread"):
        engine.reset_thread()
    st.session_state["_thread_applied"] = conversation_id


def persist_turn(engine, question: str, answer: Dict[str, Any]) -> None:
    """Soru ve yanıtı SQLite'a yazar; hata olursa sohbet akışı bozulmaz."""
    if STORE is None:
        return
    try:
        conversation_id = st.session_state.get("conversation_id")
        if not conversation_id:
            conversation_id = STORE.create_conversation(title_from_question(question))
            st.session_state.conversation_id = conversation_id
            st.session_state["_thread_applied"] = conversation_id
        STORE.append_message(conversation_id, {"role": "user", "content": question})
        STORE.append_message(conversation_id, answer)
        thread_id = getattr(engine, "thread_id", None)
        if thread_id:
            STORE.set_thread_id(conversation_id, thread_id)
    except Exception as exc:
        logging.warning("Sohbet kaydedilemedi: %s", exc)
        st.session_state["_history_error"] = str(exc)


def render_history(slot) -> None:
    """Yan menünün üst bölümü: yeni sohbet ve geçmiş sohbetler.

    Betiğin sonunda çizilir; böylece o an kaydedilen sohbet hemen listede görünür.
    """
    with slot:
        if st.button("＋  Yeni sohbet", key="new_chat", width="stretch", type="primary"):
            start_new_chat()
            st.rerun()

        error = st.session_state.get("_history_error")
        if error:
            st.caption(f"Sohbet geçmişi kaydedilemiyor: {error}")
            return
        if running_on_streamlit_cloud():
            st.caption("Cloud: sohbet geçmişi restart sonrası silinir.")
        if STORE is None:
            return

        conversations = STORE.list_conversations()
        if not conversations:
            st.caption("Aradığınız bölümler ve kaynaklar burada birikir; sohbetlere sonradan dönebilirsiniz.")
            return

        st.markdown("<div class='history-label'>GEÇMİŞ SOHBETLER</div>", unsafe_allow_html=True)
        current = st.session_state.get("conversation_id")
        for conversation in conversations:
            row_open, row_delete = st.columns([0.82, 0.18], vertical_alignment="center")
            selected = conversation.id == current
            if row_open.button(
                ("• " if selected else "") + conversation.title,
                key=f"open_chat_{conversation.id}",
                width="stretch",
                help=f"{conversation.updated_label} · {conversation.message_count} mesaj",
            ):
                open_conversation(conversation.id)
                st.rerun()
            if row_delete.button("🗑", key=f"delete_chat_{conversation.id}", help="Sohbeti sil"):
                STORE.delete_conversation(conversation.id)
                if selected:
                    start_new_chat()
                st.rerun()


def leave(slot) -> None:
    """Erken çıkışlarda da yan menü tamamlanmış olsun."""
    render_history(slot)
    st.stop()


# ----------------------------------------------------------------------
# Yan menü
# ----------------------------------------------------------------------
def sidebar(schema: Optional[IndexSchema]):
    with st.sidebar:
        history_slot = st.container()
        st.divider()

        # Teknik ayarların tamamı burada; son kullanıcı görmek zorunda değil.
        with st.expander("Gelişmiş ayarlar", expanded=False):
            backend_labels = {
                "direct": "Azure AI Search + Azure OpenAI",
                "foundry": "Foundry Agent Service",
            }
            backend = st.radio(
                "Yanıtı hangi sistem üretsin?",
                options=["direct", "foundry"],
                index=0 if SETTINGS.backend == "direct" else 1,
                format_func=lambda key: backend_labels[key],
                help=(
                    "Birincisinde dokümanları uygulama getirir, kaynaklar birebir "
                    "doğrulanabilir. İkincisinde arama ve kaynak gösterimi Foundry "
                    "tarafında yapılır."
                ),
            )

            st.markdown("**Arama**")
            semantic_supported = bool(schema and schema.supports_semantic)
            vector_supported = bool(schema and schema.supports_vector)
            schema_sig = (
                f"{getattr(schema, 'index_name', '')}|"
                f"v={int(vector_supported)}|"
                f"s={int(semantic_supported)}|"
                f"vf={','.join(getattr(schema, 'vector_fields', []) or [])}"
            )
            if st.session_state.get("_search_schema_sig") != schema_sig:
                st.session_state["_search_schema_sig"] = schema_sig
                st.session_state["use_vector"] = vector_supported
                st.session_state["use_semantic"] = semantic_supported
                st.session_state["max_sources"] = min(max(int(SETTINGS.top_k), 1), 30)
                st.session_state["min_reranker"] = float(SETTINGS.min_reranker_score)

            top_k = st.slider(
                "Modele en fazla kaç kaynak gitsin",
                min_value=1,
                max_value=30,
                key="max_sources",
                help=(
                    "Arama tüm arşivi tarar; bu yalnızca yanıta kaç kaynak sığacağını sınırlar."
                ),
            )
            use_semantic = st.toggle(
                "Sonuçları akıllı sırala",
                key="use_semantic",
                disabled=not semantic_supported,
                help=(
                    "Sorunun anlamına en uygun dokümanları öne çıkarır."
                    if semantic_supported
                    else "Bu indekste kullanılamıyor."
                ),
            )
            use_vector = st.toggle(
                "Benzer anlamlı içerikleri de bul",
                key="use_vector",
                disabled=not vector_supported,
                help=(
                    "Birebir aynı kelimeler geçmese de konuyla ilgili dokümanları bulur."
                    if vector_supported
                    else (
                        "Bu indeks vektör alanı taşımıyor. "
                        "`AZURE_SEARCH_INDEX` değerinin `fe-partial-data-vector-index` "
                        "olduğundan emin olup Yeniden bağlan'a basın."
                    )
                ),
            )

            if schema:
                vector_note = (
                    ", ".join(schema.vector_fields)
                    if schema.vector_fields
                    else "yok"
                )
                st.caption(f"İndeks: {schema.index_name} · vektör: {vector_note}")

            min_reranker = 0.0
            if use_semantic:
                min_reranker = st.slider(
                    "İlgisiz sonuçları ele",
                    0.0,
                    4.0,
                    key="min_reranker",
                    step=0.1,
                    help=(
                        "Yükseltirseniz yalnızca çok ilgili dokümanlar kalır. "
                        "0 = eleme yok; varsayılan zayıf eşleşmeleri keser."
                    ),
                )

            merge_chunks = st.toggle(
                "Aynı dosyanın parçalarını tek kaynak say",
                value=SETTINGS.merge_chunks,
                help="Uzun dokümanlar bölünerek indekslenir; tek satırda gösterilir.",
            )

            st.markdown("**Yanıt**")
            temperature = st.slider(
                "Yanıt çeşitliliği",
                0.0,
                1.0,
                float(SETTINGS.temperature),
                0.1,
                help=(
                    "Düşük değer daha kararlı yanıt verir. gpt-5 ve o-serisi modeller bu "
                    "ayarı desteklemez; o modellerde değişiklik etkisizdir."
                ),
            )
            odata_filter = st.text_input(
                "Sonuçları daralt (OData filtresi)",
                value="",
                placeholder="örn. metadata_spo_item_content_type eq 'application/pdf'",
                help="Azure AI Search $filter söz dizimi. Alan filterable olmalı.",
            )
            if schema and schema.filterable_fields:
                st.caption("Filtrelenebilir alanlar: " + ", ".join(schema.filterable_fields[:15]))

            if st.button("Yeniden bağlan", width="stretch", help="Önbellekleri temizler"):
                reset_caches()
                st.rerun()

        render_logout_button()

        missing = SETTINGS.missing_for(backend)
        if missing:
            st.warning("Eksik ayar: " + ", ".join(missing))

        return (
            backend,
            AskOptions(
                top_k=top_k,
                use_semantic=use_semantic,
                use_vector=use_vector,
                min_reranker_score=min_reranker,
                merge_chunks=merge_chunks,
                odata_filter=odata_filter.strip() or None,
                temperature=temperature,
            ),
            history_slot,
        )


# ----------------------------------------------------------------------
# Yapılandırma kontrolü
# ----------------------------------------------------------------------
if not SETTINGS.search_endpoint or not SETTINGS.search_index:
    _, _, history_slot = sidebar(None)
    ui.render_setup_help(SETTINGS.missing_for(), SETTINGS.backend)
    leave(history_slot)

schema: Optional[IndexSchema] = None
schema_error: Optional[str] = None
try:
    with st.spinner("İndeks şeması okunuyor…"):
        schema = load_schema(
            SETTINGS.search_endpoint, SETTINGS.search_index, SCHEMA_CACHE_TOKEN
        )
        # Aynı indeks adına vektör eklenmiş olabilir; eski önbelleği bir kez at.
        if (
            schema
            and not schema.supports_vector
            and "vector" in (SETTINGS.search_index or "").lower()
        ):
            load_schema.clear()
            schema = load_schema(
                SETTINGS.search_endpoint, SETTINGS.search_index, SCHEMA_CACHE_TOKEN
            )
except Exception as exc:
    schema_error = str(exc)

backend, options, history_slot = sidebar(schema)

if schema_error:
    st.error(f"Azure AI Search indeksine ulaşılamadı: {schema_error}")
    if "no index with the name" in schema_error.lower():
        try:
            from src.schema import list_index_names

            names = list_index_names(SETTINGS)
            if names:
                st.info(
                    "Bu arama servisindeki indeksler: "
                    + ", ".join(f"`{name}`" for name in names)
                    + ". Doğru adı `.env` içindeki `AZURE_SEARCH_INDEX` alanına yazın."
                )
        except Exception:
            pass
    ui.render_setup_help(SETTINGS.missing_for(backend), backend)
    leave(history_slot)

missing = SETTINGS.missing_for(backend)
if missing:
    ui.render_setup_help(missing, backend)
    leave(history_slot)

try:
    engine = load_engine(
        backend, SETTINGS.search_endpoint, SETTINGS.search_index, SCHEMA_CACHE_TOKEN
    )
    st.session_state["_engine_ref"] = engine
except Exception as exc:
    st.error(f"Motor başlatılamadı: {exc}")
    leave(history_slot)

sync_thread(engine)


# ----------------------------------------------------------------------
# Sekmeler
# ----------------------------------------------------------------------
if SETTINGS.show_advanced_tabs:
    tab_chat, tab_search, tab_diag = st.tabs(["Sohbet", "Kaynak tarayıcı", "Tanılama"])
else:
    # Son kullanıcıya yalnızca sohbet gösterilir.
    tab_chat, tab_search, tab_diag = st.container(), None, None


# ---------------------------- Sohbet ----------------------------------
with tab_chat:
    if not st.session_state.messages:
        st.info(
            "Bir bölüm, iddia veya kaynak sorun. Yanıtın altında, dayandığı arşiv "
            "belgelerinin atıf kartları ve bağlantıları listelenir.",
            icon="💬",
        )

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                sources: List[SourceDoc] = message.get("sources") or []
                st.markdown(linkify_citations(message["content"], sources))
                ui.render_sources(sources, message.get("cited"))
                ui.render_answer_meta(message.get("meta") or {})
            else:
                st.markdown(message["content"])

    question = st.chat_input("Bir bölüm, iddia veya kaynak sorun…")

    if question:
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.messages
            if m["role"] in {"user", "assistant"} and not m.get("failed")
        ]
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            answer_text = ""
            sources: List[SourceDoc] = []
            meta: dict = {}
            failed = False

            try:
                if engine.supports_streaming:
                    with st.status("Bilgi tabanı taranıyor…", expanded=False) as status:
                        stream, sources, debug = engine.start_stream(question, history, options)
                        status.update(
                            label=(
                                f"{len(sources)} doküman bulundu · {debug.get('arama tipi', '-')}"
                                if sources
                                else "İlgili doküman bulunamadı"
                            ),
                            state="complete",
                        )
                    answer_text = ui.stream_answer(stream, sources)
                    meta = {
                        "motor": "direct",
                        "arama": debug.get("arama tipi", "-"),
                        "doküman": len(sources),
                    }
                else:
                    with st.spinner("Foundry agent knowledge base'i tarıyor…"):
                        result = engine.ask(question, history, options)
                    if result.error:
                        raise RuntimeError(result.error)
                    answer_text = result.answer
                    sources = result.sources
                    st.markdown(linkify_citations(answer_text, sources))
                    meta = {
                        "motor": "foundry",
                        "süre": f"{result.latency_ms} ms",
                        "doküman": len(sources),
                    }
            except Exception as exc:
                failed = True
                answer_text = f"Yanıt üretilemedi: {exc}"
                st.error(answer_text)

            cited = parse_cited_ordinals(answer_text) if not failed else set()
            if not failed:
                ui.render_sources(sources, cited, expanded=True)
                ui.render_answer_meta(meta)

        answer_message = {
            "role": "assistant",
            "content": answer_text,
            "sources": sources,
            "cited": cited,
            "meta": meta,
            "failed": failed,
        }
        st.session_state.messages.append(answer_message)
        persist_turn(engine, question, answer_message)


# ------------------------ Kaynak tarayıcı -----------------------------
if tab_search is not None:
    with tab_search:
        st.caption(
            "Yanıt üretmeden yalnızca indeksi sorgular. Doğru dokümanların gelip gelmediğini "
            "kontrol etmek için kullanın."
        )
        raw_query = st.text_input("Arama sorgusu", placeholder="örn. satın alma onay limitleri")
        if raw_query:
            try:
                retriever = load_retriever(
                    SETTINGS.search_endpoint, SETTINGS.search_index, SCHEMA_CACHE_TOKEN
                )
                with st.spinner("Aranıyor…"):
                    docs, debug = retriever.search(
                        raw_query,
                        top_k=options.top_k,
                        odata_filter=options.odata_filter,
                        use_semantic=options.use_semantic,
                        use_vector=options.use_vector,
                        min_reranker_score=options.min_reranker_score,
                        merge_chunks=options.merge_chunks,
                    )
                st.write(f"**{len(docs)} doküman** · {debug.get('arama tipi')}")
                for doc in docs:
                    ui.render_source_card(doc, used=False)
                if not docs:
                    st.warning(
                        "Sonuç yok. Filtreyi kaldırmayı veya eleme eşiğini düşürmeyi deneyin."
                    )
            except Exception as exc:
                st.error(f"Arama başarısız: {exc}")


# --------------------------- Tanılama ---------------------------------
if tab_diag is not None:
    with tab_diag:
        left, right = st.columns(2)

        with left:
            st.subheader("Motor durumu")
            ui.info_table(engine.health())

            st.subheader("Ortam değişkenleri")
            watched = [
                "RAG_BACKEND",
                "AZURE_SEARCH_ENDPOINT",
                "AZURE_SEARCH_INDEX",
                "AZURE_SEARCH_API_KEY",
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_CHAT_DEPLOYMENT",
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
                "FOUNDRY_PROJECT_ENDPOINT",
                "FOUNDRY_MODEL_DEPLOYMENT",
                "FOUNDRY_SEARCH_CONNECTION_NAME",
                "SHAREPOINT_LINK_MODE",
            ]
            rows = {}
            for name in watched:
                value = os.getenv(name) or ""
                if not value:
                    rows[name] = "— (boş)"
                elif "KEY" in name:
                    rows[name] = "✓ tanımlı (gizli)"
                else:
                    rows[name] = value if len(value) < 60 else value[:57] + "…"
            ui.info_table(rows)

        with right:
            st.subheader("İndeks şeması")
            ui.info_table(schema.as_summary())

            if st.button("Doküman sayısını getir"):
                retriever = load_retriever(
                    SETTINGS.search_endpoint, SETTINGS.search_index, SCHEMA_CACHE_TOKEN
                )
                count = retriever.document_count()
                if count is None:
                    st.error("Doküman sayısı alınamadı.")
                else:
                    st.metric("İndekslenmiş doküman", f"{count:,}".replace(",", "."))

            if schema.warnings:
                st.subheader("Uyarılar")
                for warning in schema.warnings:
                    st.warning(warning)

            with st.expander(f"Tüm alanlar ({len(schema.all_fields)})"):
                st.table({"Tür": schema.all_fields})


render_history(history_slot)
