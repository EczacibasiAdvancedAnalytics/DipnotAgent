"""Streamlit arayüz bileşenleri."""

from __future__ import annotations

from html import escape
from typing import Dict, Iterable, List, Optional, Set

import streamlit as st

from .models import SourceDoc, linkify_citations

CSS = """
<style>
  .block-container {
    padding-top: 1.6rem;
    padding-bottom: 3.2rem;
    max-width: 980px;
  }
  .app-header {
    margin: 0 0 1.6rem;
    padding: .15rem 0 1.05rem;
    border-bottom: 1px solid #d4e1f0;
  }
  .app-header h1 {
    margin: 0 0 .28rem;
    font-size: 1.82rem;
    font-weight: 650;
    letter-spacing: -.02em;
    color: #1f4e79;
  }
  .app-header p {
    color: #5d6b7a;
    margin: 0;
    font-size: .98rem;
    line-height: 1.45;
  }
  .src-title { font-weight: 600; font-size: .95rem; line-height: 1.4; color: #243447; }
  .src-meta { color: #6b7280; font-size: .76rem; margin-top: .2rem; }
  .src-snippet {
    color: #3d4a5c;
    font-size: .86rem;
    line-height: 1.5;
    margin-top: .55rem;
    border-left: 2px solid #8eafd4;
    padding: .2rem 0 .2rem .7rem;
  }
  .pill { display: inline-block; padding: .08rem .45rem; border-radius: 999px;
          font-size: .7rem; font-weight: 600; vertical-align: middle; }
  .pill-used { background: #e7f3ec; color: #1b6b3a; }
  .pill-rel  { background: #eef3f9; color: #4a5d73; }
  .stChatMessage { max-width: 100%; }
  .stChatMessage a { text-decoration: none; font-weight: 600; }
  .answer-meta { color: #8a929c; font-size: .76rem; margin-top: .5rem; }
  .src-download { text-align: center; margin-top: .25rem; }
  .src-download a { color: #6b7280; font-size: .75rem; text-decoration: none; }
  .src-download a:hover { text-decoration: underline; }
  section[data-testid='stSidebar'] .stButton button { text-align: left; justify-content: flex-start; }
  .history-label { color: #6b7280; font-size: .78rem; font-weight: 600;
                   letter-spacing: .04em; margin: .6rem 0 .2rem; }
  .login-shell { max-width: 420px; margin: 3.5rem auto 0; }
  .login-shell .app-header { text-align: left; border-bottom: 1px solid #d4e1f0; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def header(title: str, subtitle: str) -> None:
    st.markdown(
        f"<div class='app-header'><h1>{title}</h1><p>{subtitle}</p></div>",
        unsafe_allow_html=True,
    )


def _meta_line(doc: SourceDoc) -> str:
    parts: List[str] = []
    if doc.file_type:
        parts.append(doc.file_type)
    if doc.last_modified:
        parts.append(f"güncelleme {doc.last_modified}")
    if doc.best_score is not None:
        label = "reranker" if doc.reranker_score is not None else "skor"
        parts.append(f"{label} {doc.best_score:.2f}")
    if doc.chunk_count > 1:
        parts.append(f"{doc.chunk_count} parça")
    return " · ".join(parts)


def render_source_card(doc: SourceDoc, used: bool) -> None:
    with st.container(border=True):
        left, right = st.columns([0.72, 0.28], vertical_alignment="center")
        pill = (
            "<span class='pill pill-used'>yanıtta kullanıldı</span>"
            if used
            else "<span class='pill pill-rel'>ilgili</span>"
        )
        left.markdown(
            f"<div class='src-title'>[{doc.ordinal}] {doc.display_title} {pill}</div>"
            f"<div class='src-meta'>{_meta_line(doc)}</div>",
            unsafe_allow_html=True,
        )

        open_url = doc.open_url
        download_url = doc.download_url
        if open_url:
            right.link_button("SharePoint'te aç ↗", open_url, width="stretch")
            # İndirme, dosyayı doğrudan çeken ikinci bir seçenek olarak sunulur.
            if download_url and download_url != open_url:
                right.markdown(
                    "<div class='src-download'>"
                    f"<a href='{escape(download_url, quote=True)}' target='_blank'>Dosyayı indir</a>"
                    "</div>",
                    unsafe_allow_html=True,
                )
        elif doc.extra.get("yol"):
            st.caption(f"Konum: {doc.extra['yol']}")

        if doc.snippet:
            st.markdown(f"<div class='src-snippet'>{doc.snippet}</div>", unsafe_allow_html=True)
        if doc.content and doc.content != doc.snippet:
            with st.expander("Modele giden metni gör"):
                st.text(doc.content)


def render_sources(
    sources: List[SourceDoc], cited: Optional[Set[int]] = None, *, expanded: bool = False
) -> None:
    if not sources:
        return
    cited = cited or set()
    used = [doc for doc in sources if doc.ordinal in cited]
    others = [doc for doc in sources if doc.ordinal not in cited]

    label = f"Kaynaklar — {len(used)} atıf / {len(sources)} bulundu"
    with st.expander(label, expanded=expanded or bool(used)):
        for doc in used + others:
            render_source_card(doc, doc.ordinal in cited)


def info_table(mapping: Dict) -> None:
    """Anahtar-değer tablosu. Değerleri metne çevirir; Arrow karışık tip hatasını önler."""
    st.table({"Değer": {str(key): str(value) for key, value in (mapping or {}).items()}})


def render_answer_meta(meta: Dict) -> None:
    if not meta:
        return
    bits = [f"{key}: {value}" for key, value in meta.items() if value not in (None, "")]
    st.markdown(f"<div class='answer-meta'>{' · '.join(bits)}</div>", unsafe_allow_html=True)


def stream_answer(stream: Iterable[str], sources: List[SourceDoc]) -> str:
    """Yanıtı akış halinde yazar, atıfları anında linke çevirir."""
    placeholder = st.empty()
    accumulated = ""
    for piece in stream:
        accumulated += piece
        placeholder.markdown(linkify_citations(accumulated, sources) + " ▌")
    placeholder.markdown(linkify_citations(accumulated, sources))
    return accumulated


def render_setup_help(missing: List[str], backend: str) -> None:
    st.error(
        "Uygulama henüz yapılandırılmamış. Aşağıdaki ayarlar `.env` dosyasında eksik: "
        + ", ".join(f"`{name}`" for name in missing)
    )
    st.markdown(
        f"""
**Nasıl yapılandırılır**

1. Proje klasöründeki `.env.example` dosyasını `.env` olarak kopyalayın.
2. Seçili motor (`RAG_BACKEND={backend}`) için gerekli alanları doldurun.
3. API anahtarı girmezseniz Entra ID kullanılır; bu durumda terminalde `az login`
   yapın ve kullanıcınıza gerekli rolleri verin
   (`Search Index Data Reader`, `Cognitive Services OpenAI User`).
4. Bu sayfayı yenileyin.
"""
    )
    with st.expander("Gerekli roller ve sık yapılan hatalar"):
        st.markdown(
            """
- **403 / Forbidden (Search):** kullanıcıya veya uygulamaya `Search Index Data Reader`
  rolü verilmemiş, ya da arama servisinde "Role-based access control" kapalı.
- **401 (Azure OpenAI):** `Cognitive Services OpenAI User` rolü eksik.
- **Boş sonuç:** indeks adı yanlış veya indexer henüz çalışmamış olabilir.
- **Referanslarda link yok:** indekste doküman adresini tutan alan yok. SharePoint
  indexer'da bu alan `metadata_spo_item_weburi` olarak gelir; `SEARCH_FIELD_URL` ile
  elle de belirtebilirsiniz.
"""
        )
