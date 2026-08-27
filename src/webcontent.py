"""İnce içerikli kaynaklardaki açık web linklerinden metin çekme.

SharePoint'teki .msg e-postalarında asıl makale yerine yalnızca bir URL olabilir.
Bu modül, abonelik/paywall gerektirmeyen http(s) sayfalarını çekip SourceDoc
içeriğine ekler. Foundry motoruna bağlanmaz; Direct retrieve sonrası çağrılır.

Güvenlik: yalnızca http/https, özel IP ve localhost yasak (SSRF), boyut/süre
sınırı, paywall host'ları ve 401/403 atlanır. Başarısız çekimler sessizce yok sayılır.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Settings
from .models import SourceDoc

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

THIN_CHAR_LIMIT = 800
MAX_URLS_PER_DOC = 2
MAX_FETCH_BYTES = 200 * 1024
FETCH_TIMEOUT_SEC = 8
WEB_TEXT_LIMIT = 4000
MAX_REDIRECTS = 3

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Bilinen abonelik / giriş duvarı host'ları (www. öneki ve alt alanlar dahil).
PAYWALL_HOSTS = frozenset(
    {
        "wsj.com",
        "nytimes.com",
        "ft.com",
        "economist.com",
        "bloomberg.com",
        "hbr.org",
        "medium.com",
        "linkedin.com",
        "facebook.com",
        "fb.com",
        "x.com",
        "twitter.com",
        "instagram.com",
    }
)

DOWNLOAD_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".rar",
        ".7z",
        ".exe",
        ".msg",
        ".eml",
        ".csv",
        ".rtf",
        ".odt",
        ".ods",
    }
)

_SKIP_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "metadata.google.internal",
    }
)

_PAYWALL_BODY_RE = re.compile(
    r"(class|id)\s*=\s*['\"][^'\"]*paywall"
    r"|subscribe to (continue|read|unlock)"
    r"|subscription required"
    r"|metered[- ]?(paywall|content)"
    r"|create an account to (continue|read)"
    r"|sign[- ]?in to (continue|read)"
    r"|log[- ]?in to (continue|read)"
    r"|members?[- ]only",
    re.IGNORECASE,
)

FetchFn = Callable[[str], Optional[str]]


def extract_urls(text: str) -> List[str]:
    """Metindeki http/https adreslerini sırayla, tekrarsız döndürür."""
    found: List[str] = []
    seen: Set[str] = set()
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:)]}>\"'")
        url = url.split("#", 1)[0]
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _hostname(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, blocked: Iterable[str]) -> bool:
    for name in blocked:
        if host == name or host.endswith("." + name):
            return True
    return False


def is_paywall_url(url: str) -> bool:
    host = _hostname(url)
    return bool(host) and _host_matches(host, PAYWALL_HOSTS)


def is_download_url(url: str) -> bool:
    """PDF / Office indirme adresleri; HTML sayfalar önceliklidir."""
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    path = path.split("?", 1)[0]
    for ext in DOWNLOAD_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def is_ssrf_unsafe(url: str, *, resolve: bool = True) -> bool:
    """Özel ağ, localhost veya http(s) dışı adresleri reddeder."""
    try:
        parsed = urlparse(url)
    except Exception:
        return True
    if parsed.scheme not in {"http", "https"}:
        return True
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return True
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host in _SKIP_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return _is_blocked_ip(ip)
    except ValueError:
        pass
    if not resolve:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return True
    if not infos:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            return True
        if _is_blocked_ip(ip):
            return True
    return False


def is_thin_or_link_heavy(content: str) -> bool:
    """İnce metin + en az bir URL, veya içerik büyük ölçüde URL/boşluk."""
    text = content or ""
    urls = extract_urls(text)
    if not urls:
        return False
    if len(text) < THIN_CHAR_LIMIT:
        return True
    remainder = "".join(URL_RE.sub(" ", text).split())
    if not remainder:
        return True
    return len(remainder) / max(len(text), 1) < 0.3 and len(remainder) < 400


def looks_like_paywall(status: int, body: str) -> bool:
    if status in {401, 403, 402}:
        return True
    snippet = (body or "")[:8000]
    return bool(_PAYWALL_BODY_RE.search(snippet))


class _VisibleTextParser(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "svg", "iframe", "head"})
    _BREAK = frozenset({"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """script/style atılarak görünür metin."""
    parser = _VisibleTextParser()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:
        stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", markup or "")
        stripped = re.sub(r"(?is)<[^>]+>", " ", stripped)
        raw = html_lib.unescape(stripped)
    else:
        raw = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _url_fetchable(url: str, *, resolve: bool = True) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    if is_paywall_url(url) or is_download_url(url):
        return False
    if is_ssrf_unsafe(url, resolve=resolve):
        return False
    return True


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        count = getattr(req, "_redirect_count", 0) + 1
        if count > MAX_REDIRECTS:
            raise URLError("too many redirects")
        if not _url_fetchable(newurl):
            raise URLError("redirect blocked")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req._redirect_count = count  # type: ignore[attr-defined]
        return new_req


def _decode_body(data: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset\s*=\s*([A-Za-z0-9._-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    return data.decode(charset, errors="replace")


def fetch_url(url: str) -> Optional[str]:
    """Sayfayı çeker; uygun değilse veya hata olursa None (sessiz)."""
    if not _url_fetchable(url):
        return None
    request = Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.1",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    opener = build_opener(_SafeRedirectHandler)
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SEC) as response:
            status = getattr(response, "status", None) or response.getcode() or 200
            if looks_like_paywall(int(status), ""):
                return None
            content_type = response.headers.get("Content-Type") or ""
            lowered = content_type.lower()
            if lowered and not any(token in lowered for token in ("html", "text/plain", "xml", "json")):
                return None
            data = response.read(MAX_FETCH_BYTES)
    except HTTPError as exc:
        if exc.code in {401, 403, 402}:
            return None
        logger.debug("Web fetch HTTP hatası (%s): %s", url, exc)
        return None
    except Exception as exc:
        logger.debug("Web fetch atlandı (%s): %s", url, exc)
        return None

    body = _decode_body(data, content_type)
    if looks_like_paywall(int(status), body):
        return None
    if "html" in lowered or "<html" in body[:800].lower() or "<body" in body[:800].lower():
        text = html_to_text(body)
    else:
        text = " ".join(body.split())
    text = text.strip()
    if not text:
        return None
    if len(text) > WEB_TEXT_LIMIT:
        text = text[:WEB_TEXT_LIMIT].rstrip() + " […]"
    return text


def _append_web(doc: SourceDoc, url: str, text: str) -> None:
    block = f"--- Web kaynağı ({url}) ---\n{text}"
    if doc.content:
        doc.content = f"{doc.content.rstrip()}\n\n{block}"
    else:
        doc.content = block
    extra = dict(doc.extra or {})
    pages = list(extra.get("web") or [])
    pages.append({"url": url, "chars": len(text)})
    extra["web"] = pages
    doc.extra = extra


def enrich_sources(
    docs: Sequence[SourceDoc],
    settings: Optional[Settings] = None,
    *,
    fetch_fn: Optional[FetchFn] = None,
) -> Tuple[List[SourceDoc], Dict[str, Any]]:
    """İnce/link ağırlıklı kaynaklardaki açık sayfaları çekip içeriğe ekler."""
    sources = list(docs)
    enabled = True if settings is None else bool(settings.web_fetch_enabled)
    limit = 6 if settings is None else max(int(settings.web_fetch_max_per_question), 0)
    debug: Dict[str, Any] = {
        "web çekme": "açık" if enabled else "kapalı",
        "web çekilen": 0,
        "web atlanan": 0,
    }
    if not enabled or limit <= 0 or not sources:
        return sources, debug

    getter = fetch_fn or fetch_url
    fetched_this_question = 0
    seen_urls: Set[str] = set()

    for doc in sources:
        if fetched_this_question >= limit:
            break
        content = doc.content or ""
        if not is_thin_or_link_heavy(content):
            continue
        taken = 0
        for url in extract_urls(content):
            if fetched_this_question >= limit or taken >= MAX_URLS_PER_DOC:
                break
            if url in seen_urls:
                debug["web atlanan"] += 1
                continue
            seen_urls.add(url)
            if fetch_fn is None and not _url_fetchable(url):
                debug["web atlanan"] += 1
                continue
            if fetch_fn is not None and (is_paywall_url(url) or is_ssrf_unsafe(url, resolve=False) or is_download_url(url)):
                debug["web atlanan"] += 1
                continue
            try:
                text = getter(url)
            except Exception as exc:
                logger.debug("Web fetch istisnası (%s): %s", url, exc)
                text = None
            if not text:
                debug["web atlanan"] += 1
                continue
            _append_web(doc, url, text)
            taken += 1
            fetched_this_question += 1
            debug["web çekilen"] += 1

    return sources, debug
