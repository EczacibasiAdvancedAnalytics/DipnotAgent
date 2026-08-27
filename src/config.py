"""Ortam değişkenlerinden ve Streamlit secrets'tan okunan uygulama yapılandırması."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_TRUE = {"1", "true", "yes", "y", "on", "evet"}


def _apply_streamlit_secrets() -> None:
    """`st.secrets` varsa değerleri ortam değişkenlerinin üzerine yazar.

    Streamlit Community Cloud'da `.env` yoktur; Advanced → Secrets panosundaki
    TOML (ortam değişkeni adlarıyla aynı anahtarlar) buradan okunur. Secrets
    dosyası yoksa veya Streamlit yüklü değilse sessizce geçer; CLI / test
    import'u kırılmaz.
    """
    try:
        import streamlit as st
    except ImportError:
        return
    try:
        secrets = st.secrets
        mapping = secrets.to_dict() if hasattr(secrets, "to_dict") else dict(secrets)
    except Exception:
        return
    if not mapping:
        return
    for key, value in mapping.items():
        if isinstance(value, dict):
            for nested_key, nested_val in value.items():
                if nested_val is None:
                    continue
                os.environ[str(nested_key)] = str(nested_val)
            continue
        if value is None:
            continue
        os.environ[str(key)] = str(value)


def load_env() -> None:
    # Streamlit süreci uzun yaşar; indeks adı `.env`'de değişince bir sonraki
    # çalıştırmada (ve "Yeniden bağlan" sonrası) yeni değer okunabilsin.
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    # Cloud (ve yerelde secrets.toml varsa): secrets, .env üzerine yazılır.
    _apply_streamlit_secrets()


def env_cache_token() -> str:
    """`.env` / secrets değişince Streamlit `cache_resource` anahtarının yenilenmesi için."""
    path = PROJECT_ROOT / ".env"
    try:
        stat = path.stat()
        token = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        token = "no-env"
    if running_on_streamlit_cloud():
        # Cloud'da .env yok; API anahtarı varlığı önbelleği ayırır (değerin kendisi değil).
        token += ":cloud"
        token += ":sk" if _s("AZURE_SEARCH_API_KEY") else ":nsk"
        token += ":ok" if _s("AZURE_OPENAI_API_KEY") else ":nok"
    return token


def running_on_streamlit_cloud() -> bool:
    """Streamlit Community Cloud (share.streamlit.io) üzerinde mi çalışıyor."""
    env_flag = (os.getenv("STREAMLIT_RUNTIME_ENV") or os.getenv("STREAMLIT_CLOUD") or "").lower()
    if env_flag in {"cloud", "1", "true", "yes"}:
        return True
    if os.getenv("STREAMLIT_SHARING_MODE"):
        return True
    hostname = (os.getenv("HOSTNAME") or "").lower()
    if "streamlit" in hostname:
        return True
    # Community Cloud uygulamayı /mount/src altına koyar; disk ephemeral'dır.
    return Path("/mount/src").is_dir()


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _opt(name: str) -> Optional[str]:
    value = _s(name)
    return value or None


def _b(name: str, default: bool) -> bool:
    raw = _s(name)
    return raw.lower() in _TRUE if raw else default


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float((_s(name) or str(default)).replace(",", "."))
    except ValueError:
        return default


def _list(name: str) -> List[str]:
    raw = _s(name)
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


def _aoai_endpoint(name: str) -> str:
    """Azure OpenAI kaynak adresini normalize eder.

    Portalda gösterilen adres bazen `/openai/v1` veya `/openai` yolunu içerir; klasik
    `AzureOpenAI` istemcisi ise yalnızca kaynak kökünü bekler, yolu kendisi ekler.
    """
    value = _s(name).rstrip("/")
    for suffix in ("/openai/v1", "/openai/deployments", "/openai"):
        if value.lower().endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    backend: str = "direct"

    search_endpoint: str = ""
    search_index: str = ""
    search_api_key: Optional[str] = None
    semantic_config: Optional[str] = None

    field_content: Optional[str] = None
    field_title: Optional[str] = None
    field_url: Optional[str] = None
    field_last_modified: Optional[str] = None
    field_file_type: Optional[str] = None
    field_vector: List[str] = field(default_factory=list)

    aoai_endpoint: str = ""
    aoai_chat_deployment: str = ""
    aoai_api_key: Optional[str] = None
    aoai_api_version: str = "2024-10-21"
    aoai_embedding_deployment: Optional[str] = None

    foundry_endpoint: str = ""
    foundry_model: str = ""
    foundry_search_connection: Optional[str] = None
    foundry_agent_id: Optional[str] = None
    foundry_agent_name: str = "sharepoint-knowledge-agent"
    foundry_query_type: str = "vector_semantic_hybrid"

    top_k: int = 12
    candidate_count: int = 48
    temperature: float = 0.0
    max_tokens: int = 1500
    reasoning_budget: int = 2000
    max_chars_per_doc: int = 4000
    min_reranker_score: float = 2.0
    merge_chunks: bool = True
    history_turns: int = 4
    rewrite_query: bool = True
    # İnce içerikli (.msg) kaynaklardaki açık web linklerini çek.
    web_fetch_enabled: bool = True
    web_fetch_max_per_question: int = 6

    app_title: str = "Dipnot"
    app_subtitle: str = "Kitap yazarken SharePoint arşivinden kaynak ve atıf."
    # Teknik sekmeler (Kaynak tarayıcı, Tanılama) varsayılan olarak gizlidir.
    show_advanced_tabs: bool = False
    chat_db_path: Optional[str] = None
    sharepoint_site_url: Optional[str] = None
    sharepoint_doc_library: str = "Shared Documents"
    # browse: dosyayı SharePoint web arayüzünde açar, direct: dosyayı indirir.
    sharepoint_link_mode: str = "browse"
    # Boşsa giriş ekranı yok; APP_AUTH_PASSWORD doluysa login zorunlu.
    auth_user: str = ""
    auth_password: str = ""

    @property
    def search_uses_entra(self) -> bool:
        return not self.search_api_key

    @property
    def aoai_uses_entra(self) -> bool:
        return not self.aoai_api_key

    def missing_for(self, backend: Optional[str] = None) -> List[str]:
        """Seçili motorun çalışması için eksik olan ayarları döndürür."""
        backend = (backend or self.backend).lower()
        missing: List[str] = []

        if backend == "foundry":
            if not self.foundry_endpoint:
                missing.append("FOUNDRY_PROJECT_ENDPOINT")
            if not self.foundry_model:
                missing.append("FOUNDRY_MODEL_DEPLOYMENT")
            if not self.foundry_search_connection:
                missing.append("FOUNDRY_SEARCH_CONNECTION_NAME")
            if not self.search_index:
                missing.append("AZURE_SEARCH_INDEX")
            return missing

        if not self.search_endpoint:
            missing.append("AZURE_SEARCH_ENDPOINT")
        if not self.search_index:
            missing.append("AZURE_SEARCH_INDEX")
        if not self.aoai_endpoint:
            missing.append("AZURE_OPENAI_ENDPOINT")
        if not self.aoai_chat_deployment:
            missing.append("AZURE_OPENAI_CHAT_DEPLOYMENT")
        return missing


def get_settings() -> Settings:
    load_env()
    backend = _s("RAG_BACKEND", "direct").lower()
    if backend not in {"direct", "foundry"}:
        backend = "direct"

    link_mode = _s("SHAREPOINT_LINK_MODE", "browse").lower()
    if link_mode not in {"browse", "direct"}:
        link_mode = "browse"

    return Settings(
        backend=backend,
        search_endpoint=_s("AZURE_SEARCH_ENDPOINT").rstrip("/"),
        search_index=_s("AZURE_SEARCH_INDEX"),
        search_api_key=_opt("AZURE_SEARCH_API_KEY"),
        semantic_config=_opt("AZURE_SEARCH_SEMANTIC_CONFIG"),
        field_content=_opt("SEARCH_FIELD_CONTENT"),
        field_title=_opt("SEARCH_FIELD_TITLE"),
        field_url=_opt("SEARCH_FIELD_URL"),
        field_last_modified=_opt("SEARCH_FIELD_LAST_MODIFIED"),
        field_file_type=_opt("SEARCH_FIELD_FILE_TYPE"),
        field_vector=_list("SEARCH_FIELD_VECTOR"),
        aoai_endpoint=_aoai_endpoint("AZURE_OPENAI_ENDPOINT"),
        aoai_chat_deployment=_s("AZURE_OPENAI_CHAT_DEPLOYMENT"),
        aoai_api_key=_opt("AZURE_OPENAI_API_KEY"),
        aoai_api_version=_s("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        aoai_embedding_deployment=_opt("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
        foundry_endpoint=_s("FOUNDRY_PROJECT_ENDPOINT").rstrip("/"),
        foundry_model=_s("FOUNDRY_MODEL_DEPLOYMENT"),
        foundry_search_connection=_opt("FOUNDRY_SEARCH_CONNECTION_NAME"),
        foundry_agent_id=_opt("FOUNDRY_AGENT_ID"),
        foundry_agent_name=_s("FOUNDRY_AGENT_NAME", "sharepoint-knowledge-agent"),
        foundry_query_type=_s("FOUNDRY_QUERY_TYPE", "vector_semantic_hybrid").lower(),
        top_k=_i("RAG_TOP_K", 12),
        candidate_count=_i("RAG_CANDIDATE_COUNT", 48),
        temperature=_f("RAG_TEMPERATURE", 0.0),
        max_tokens=_i("RAG_MAX_TOKENS", 1500),
        reasoning_budget=_i("RAG_REASONING_BUDGET", 2000),
        max_chars_per_doc=_i("RAG_MAX_CHARS_PER_DOC", 4000),
        min_reranker_score=_f("RAG_MIN_RERANKER_SCORE", 2.0),
        merge_chunks=_b("RAG_MERGE_CHUNKS", True),
        history_turns=_i("RAG_HISTORY_TURNS", 4),
        rewrite_query=_b("RAG_REWRITE_QUERY", True),
        web_fetch_enabled=_b("WEB_FETCH_ENABLED", True),
        web_fetch_max_per_question=_i("WEB_FETCH_MAX_PER_QUESTION", 6),
        app_title=_s("APP_TITLE", "Dipnot"),
        app_subtitle=_s(
            "APP_SUBTITLE",
            "Kitap yazarken SharePoint arşivinden kaynak ve atıf.",
        ),
        show_advanced_tabs=_b("APP_SHOW_ADVANCED_TABS", False),
        chat_db_path=_opt("APP_CHAT_DB_PATH"),
        sharepoint_site_url=_opt("SHAREPOINT_SITE_URL"),
        sharepoint_doc_library=_s("SHAREPOINT_DOC_LIBRARY", "Shared Documents"),
        sharepoint_link_mode=link_mode,
        auth_user=_s("APP_AUTH_USER"),
        auth_password=_s("APP_AUTH_PASSWORD"),
    )
