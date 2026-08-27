"""Azure OpenAI istemcisi: sohbet tamamlama ve embedding.

Model aileleri parametre konusunda farklılaşır: klasik modeller `temperature` ve
`max_tokens` alır, gpt-5 gibi akıl yürütme modelleri `temperature` kabul etmez ve
`max_tokens` yerine `max_completion_tokens` bekler. Bu modül hangi biçimin
çalıştığını ilk istekte tespit eder ve sonraki isteklerde tekrar denemez.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from openai import AzureOpenAI

from .config import Settings

logger = logging.getLogger(__name__)

AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"

# Denenecek parametre biçimleri, sırayla.
PARAM_MODES = ("standard", "reasoning", "minimal")

MAX_RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BASE_WAIT = 6.0

PARAM_ERROR_HINTS = (
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "unsupported",
    "not supported",
    "unrecognized",
)


def build_client(settings: Settings) -> AzureOpenAI:
    """API anahtarı varsa onu, yoksa Entra ID token sağlayıcısını kullanır."""
    if settings.aoai_api_key:
        return AzureOpenAI(
            azure_endpoint=settings.aoai_endpoint,
            api_key=settings.aoai_api_key,
            api_version=settings.aoai_api_version,
        )

    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(exclude_interactive_browser_credential=False), AOAI_SCOPE
    )
    return AzureOpenAI(
        azure_endpoint=settings.aoai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version=settings.aoai_api_version,
    )


def _is_rate_limit(exc: Exception) -> bool:
    if type(exc).__name__ == "RateLimitError":
        return True
    return getattr(exc, "status_code", None) == 429


def _is_param_error(exc: Exception) -> bool:
    """Hata, desteklenmeyen bir parametreden mi kaynaklanıyor?"""
    if _is_rate_limit(exc):
        return False
    status = getattr(exc, "status_code", None)
    if status is not None and status != 400:
        return False
    message = str(exc).lower()
    return any(hint in message for hint in PARAM_ERROR_HINTS)


def _retry_after(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
        value = headers.get(key) if hasattr(headers, "get") else None
        if value:
            try:
                return float(str(value).rstrip("s"))
            except ValueError:
                continue
    return None


class LlmClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = build_client(settings)
        self._param_mode: Optional[str] = None

    @property
    def param_mode(self) -> str:
        return self._param_mode or "(henüz belirlenmedi)"

    # ------------------------------------------------------------------
    def _call_with_rate_limit_retry(self, factory: Callable[[], Any]) -> Any:
        """429 durumunda bekleyip tekrar dener."""
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES):
            try:
                return factory()
            except Exception as exc:
                if not _is_rate_limit(exc) or attempt == MAX_RATE_LIMIT_RETRIES - 1:
                    raise
                wait = _retry_after(exc) or RATE_LIMIT_BASE_WAIT * (2**attempt)
                wait = min(wait, 60.0)
                logger.warning(
                    "Azure OpenAI kota sınırı (429). %.0f saniye beklenip tekrar denenecek "
                    "(%d/%d).",
                    wait,
                    attempt + 1,
                    MAX_RATE_LIMIT_RETRIES - 1,
                )
                last_exc = exc
                time.sleep(wait)
        raise last_exc  # pragma: no cover

    def _build_kwargs(
        self, mode: str, messages: Sequence[dict], temperature: float, tokens: int, stream: bool
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.settings.aoai_chat_deployment,
            "messages": list(messages),
            "stream": stream,
        }
        if mode == "standard":
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = tokens
        elif mode == "reasoning":
            # Görünmeyen düşünme adımları da token harcar; bu pay eklenmezse yanıt
            # boş dönebilir.
            kwargs["max_completion_tokens"] = tokens + max(self.settings.reasoning_budget, 0)
        return kwargs

    def _create_completion(
        self, messages: Sequence[dict], temperature: float, tokens: int, stream: bool
    ) -> Any:
        modes = [self._param_mode] if self._param_mode else list(PARAM_MODES)
        last_exc: Optional[Exception] = None

        for mode in modes:
            kwargs = self._build_kwargs(mode, messages, temperature, tokens, stream)
            try:
                result = self._call_with_rate_limit_retry(
                    lambda: self.client.chat.completions.create(**kwargs)
                )
            except Exception as exc:
                if not _is_param_error(exc):
                    raise
                logger.info("'%s' parametre biçimi reddedildi, sonraki deneniyor: %s", mode, exc)
                last_exc = exc
                continue

            if self._param_mode != mode:
                logger.info("Model '%s' için parametre biçimi: %s", kwargs["model"], mode)
                self._param_mode = mode
            return result

        raise last_exc if last_exc else RuntimeError("Sohbet isteği oluşturulamadı.")

    # ------------------------------------------------------------------
    def embed(self, text: str) -> Optional[List[float]]:
        deployment = self.settings.aoai_embedding_deployment
        if not deployment:
            return None
        response = self._call_with_rate_limit_retry(
            lambda: self.client.embeddings.create(model=deployment, input=[text])
        )
        return list(response.data[0].embedding)

    def embedder(self):
        """Retriever'a verilecek çağrılabilir; embedding deployment yoksa None."""
        return self.embed if self.settings.aoai_embedding_deployment else None

    # ------------------------------------------------------------------
    def stream_chat(
        self,
        messages: Sequence[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        temp = self.settings.temperature if temperature is None else temperature
        tokens = self.settings.max_tokens if max_tokens is None else max_tokens

        stream = self._create_completion(messages, temp, tokens, stream=True)
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            piece = getattr(chunk.choices[0].delta, "content", None)
            if piece:
                yield piece

    def chat(
        self,
        messages: Sequence[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        temp = self.settings.temperature if temperature is None else temperature
        tokens = self.settings.max_tokens if max_tokens is None else max_tokens

        response = self._create_completion(messages, temp, tokens, stream=False)
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        return getattr(choices[0].message, "content", None) or ""
