"""Mevcut bir indeksin yanına vektör destekli bir kopyasını kurar ve doldurur.

Kaynak indeks yalnızca okunur; hiçbir koşulda değiştirilmez veya silinmez.
Hedef indeks, kaynağın alanlarının birebir kopyası + `snippet_vector` alanıdır.
Alan adları korunduğu için uygulamanın otomatik alan keşfi (`src/schema.py`)
kodda değişiklik gerektirmeden çalışmaya devam eder.

Hedef indekse bir **AzureOpenAIVectorizer** bağlanır: sorgu vektörünü Azure AI
Search üretir, uygulama `VectorizableTextQuery` kullanır ve kendi tarafında
embedding çağrısı yapmaz.

    python scripts/build_vector_index.py --dry-run          # yalnızca maliyet tahmini
    python scripts/build_vector_index.py                    # kur ve doldur (kaldığı yerden)
    python scripts/build_vector_index.py --recreate         # hedefi silip sıfırdan kur
    python scripts/build_vector_index.py --source A --target B --embed-batch 16

Yeniden çalıştırıldığında hedef indekste zaten bulunan `uid` değerleri atlanır;
yarıda kesilen bir doldurma işi kaldığı yerden devam eder (`--no-resume` ile kapatılır).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.search.documents import SearchClient  # noqa: E402
from azure.search.documents.indexes import SearchIndexClient  # noqa: E402
from azure.search.documents.indexes.models import (  # noqa: E402
    AzureOpenAIModelName,
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)

from src.config import Settings, get_settings  # noqa: E402
from src.llm import build_client  # noqa: E402
from src.schema import _credential, build_schema  # noqa: E402

VECTOR_FIELD = "snippet_vector"
VECTOR_DIMENSIONS = 3072  # text-embedding-3-large
ALGORITHM_NAME = "hnsw-snippet"
PROFILE_NAME = "snippet-vector-profile"
VECTORIZER_NAME = "aoai-text-embedding-3-large"

# text-embedding-3-large girdi sınırı 8191 token. Karakter/token oranı Türkçe ve
# karışık dilli metinlerde ~2-3 olduğundan 24000 karakter güvenli bir üst sınır.
MAX_EMBED_CHARS = 24000

# Kota (429) hatalarında bekle-ve-tekrar-dene; ısrar ederse grup küçültülür.
MAX_EMBED_ATTEMPTS = 12
EMBED_BASE_WAIT = 5.0
MAX_EMBED_WAIT = 120.0
RATE_LIMIT_SHRINK_AFTER = 1  # ilk 429'da bekle, sonrakilerde grubu da böl

MAX_UPLOAD_ATTEMPTS = 6
UPLOAD_BASE_WAIT = 4.0

# Azure AI Search tek sayfada en fazla 1000 kayıt döndürür.
READ_PAGE_SIZE = 1000

# 1000 token ~ 4 karakter/token varsayımı; yalnızca maliyet tahmini için.
CHARS_PER_TOKEN = 4.0


# ----------------------------------------------------------------------
# Yardımcılar
# ----------------------------------------------------------------------
def _is_rate_limit(exc: Exception) -> bool:
    if type(exc).__name__ in {"RateLimitError", "ServiceRequestError"}:
        return True
    return getattr(exc, "status_code", None) in {429, 503}


def _retry_after(exc: Exception) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    if not hasattr(headers, "get"):
        return None
    for key in ("retry-after", "Retry-After"):
        value = headers.get(key)
        if value:
            try:
                return float(str(value).rstrip("s"))
            except ValueError:
                continue
    return None


def _clip(text: str) -> str:
    return text if len(text) <= MAX_EMBED_CHARS else text[:MAX_EMBED_CHARS]


def default_target_name(source_name: str) -> str:
    """Kaynak `fe-partial-data-index` ise hedef `fe-partial-data-vector-index` olur.

    Eski `<kaynak>-vector-index` kuralı `fe-partial-data-index-vector-index`
    üretir; bu ad kullanılmaz.
    """
    if source_name.endswith("-index"):
        return f"{source_name[: -len('-index')]}-vector-index"
    return f"{source_name}-vector-index"


def _search_credential(settings: Settings):
    return _credential(settings)


# ----------------------------------------------------------------------
# Hedef indeks tanımı
# ----------------------------------------------------------------------
def build_target_index(
    name: str, source: SearchIndex, settings: Settings, content_field: str
) -> SearchIndex:
    """Kaynağın alanlarını birebir kopyalar, üzerine vektör alanını ekler."""
    fields: List[Any] = [f for f in (source.fields or []) if f.name != VECTOR_FIELD]
    fields.append(
        SearchField(
            name=VECTOR_FIELD,
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            # Vektör 3072 float; sorgu yanıtında dönmesine gerek yok.
            hidden=True,
            vector_search_dimensions=VECTOR_DIMENSIONS,
            vector_search_profile_name=PROFILE_NAME,
        )
    )

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=ALGORITHM_NAME,
                parameters=HnswParameters(
                    m=4,
                    ef_construction=400,
                    ef_search=500,
                    metric=VectorSearchAlgorithmMetric.COSINE,
                ),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=PROFILE_NAME,
                algorithm_configuration_name=ALGORITHM_NAME,
                vectorizer_name=VECTORIZER_NAME,
            )
        ],
        vectorizers=[
            AzureOpenAIVectorizer(
                vectorizer_name=VECTORIZER_NAME,
                parameters=AzureOpenAIVectorizerParameters(
                    resource_url=settings.aoai_endpoint,
                    deployment_name=settings.aoai_embedding_deployment,
                    model_name=AzureOpenAIModelName.TEXT_EMBEDDING3_LARGE,
                    api_key=settings.aoai_api_key,
                ),
            )
        ],
    )

    # Semantik yapılandırmayı kaynaktan kopyala; yoksa içerik alanı üzerine kur.
    semantic = getattr(source, "semantic_search", None)
    if not semantic or not (getattr(semantic, "configurations", None) or []):
        config_name = f"{name}-semantic-configuration"
        semantic = SemanticSearch(
            default_configuration_name=config_name,
            configurations=[
                SemanticConfiguration(
                    name=config_name,
                    prioritized_fields=SemanticPrioritizedFields(
                        content_fields=[SemanticField(field_name=content_field)]
                    ),
                )
            ],
        )

    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic,
        scoring_profiles=getattr(source, "scoring_profiles", None),
        default_scoring_profile=getattr(source, "default_scoring_profile", None),
        analyzers=getattr(source, "analyzers", None),
        tokenizers=getattr(source, "tokenizers", None),
        token_filters=getattr(source, "token_filters", None),
        char_filters=getattr(source, "char_filters", None),
        similarity=getattr(source, "similarity", None),
    )


# ----------------------------------------------------------------------
# Okuma
# ----------------------------------------------------------------------
def read_documents(
    client: SearchClient, select: Sequence[str], key_field: str, limit: Optional[int] = None
) -> Iterator[Dict[str, Any]]:
    """Sayfa sayfa tüm dokümanları okur.

    `skip` + `order_by` ile sayfalanır: sıralama deterministik olduğu için
    sayfalar arasında kayıt kaybı veya tekrarı olmaz. Sayfa başına en fazla
    1000 kayıt gelir.
    """
    read = 0
    skip = 0
    while True:
        page_size = READ_PAGE_SIZE
        if limit is not None:
            page_size = min(page_size, limit - read)
            if page_size <= 0:
                return
        results = client.search(
            search_text="*",
            select=list(select),
            order_by=[key_field],
            top=page_size,
            skip=skip,
            include_total_count=False,
        )
        count = 0
        for result in results:
            count += 1
            read += 1
            yield {k: v for k, v in dict(result).items() if not k.startswith("@search")}
        if count < page_size:
            return
        skip += count


def existing_keys(client: SearchClient, key_field: str) -> Set[str]:
    """Hedef indekste zaten bulunan anahtarlar (devam edebilmek için)."""
    keys: Set[str] = set()
    for doc in read_documents(client, [key_field], key_field):
        value = doc.get(key_field)
        if value:
            keys.add(str(value))
    return keys


# ----------------------------------------------------------------------
# Embedding
# ----------------------------------------------------------------------
def embed_batch(
    client: Any,
    deployment: str,
    texts: List[str],
    *,
    on_shrink: Optional[Any] = None,
) -> List[List[float]]:
    """Bir grup metni vektöre çevirir; 429'da bekler, ısrar ederse grubu küçültür."""
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_EMBED_ATTEMPTS):
        try:
            response = client.embeddings.create(model=deployment, input=texts)
            return [list(item.embedding) for item in response.data]
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit(exc):
                wait = _retry_after(exc) or EMBED_BASE_WAIT * (2 ** min(attempt, 5))
                wait = min(wait, MAX_EMBED_WAIT)
                print(
                    f"    kota sınırı (429) - {wait:.0f} sn beklenip tekrar denenecek "
                    f"({attempt + 1}/{MAX_EMBED_ATTEMPTS})",
                    flush=True,
                )
                time.sleep(wait)
                if attempt >= RATE_LIMIT_SHRINK_AFTER and len(texts) > 1:
                    middle = max(1, len(texts) // 2)
                    print(
                        f"    grup {len(texts)} -> {middle}+{len(texts) - middle} kayıta küçültülüyor",
                        flush=True,
                    )
                    if on_shrink is not None:
                        on_shrink(middle)
                    return embed_batch(
                        client, deployment, texts[:middle], on_shrink=on_shrink
                    ) + embed_batch(
                        client, deployment, texts[middle:], on_shrink=on_shrink
                    )
                continue
            # Girdi çok uzun / tek kayıt bozuk olabilir: grubu bölüp tekrar dene.
            if len(texts) > 1:
                middle = len(texts) // 2
                print(f"    grup hatası, {len(texts)} kayıt bölünüyor: {exc}", flush=True)
                if on_shrink is not None:
                    on_shrink(middle)
                return embed_batch(
                    client, deployment, texts[:middle], on_shrink=on_shrink
                ) + embed_batch(
                    client, deployment, texts[middle:], on_shrink=on_shrink
                )
            raise
    raise last_exc if last_exc else RuntimeError("Embedding üretilemedi.")


# ----------------------------------------------------------------------
# Yükleme
# ----------------------------------------------------------------------
def upload_batch(client: SearchClient, docs: List[Dict[str, Any]]) -> int:
    """Bir grup dokümanı hedef indekse yazar; başarısız kayıt sayısını döndürür."""
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_UPLOAD_ATTEMPTS):
        try:
            results = client.merge_or_upload_documents(documents=docs)
            return sum(1 for r in results if not r.succeeded)
        except Exception as exc:
            last_exc = exc
            if attempt == MAX_UPLOAD_ATTEMPTS - 1:
                raise
            wait = min(UPLOAD_BASE_WAIT * (2**attempt), 60.0)
            print(
                f"    yükleme hatası, {wait:.0f} sn sonra tekrar denenecek "
                f"({attempt + 1}/{MAX_UPLOAD_ATTEMPTS - 1}): {exc}",
                flush=True,
            )
            time.sleep(wait)
    raise last_exc if last_exc else RuntimeError("Yükleme yapılamadı.")


# ----------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vektör destekli indeks kurar ve mevcut indeksten doldurur."
    )
    parser.add_argument("--source", default=None, help="Kaynak indeks (varsayılan: .env)")
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "Hedef indeks (varsayılan: kaynak `...-index` ise `...-vector-index`; "
            "ör. fe-partial-data-index → fe-partial-data-vector-index)"
        ),
    )
    parser.add_argument(
        "--embed-batch",
        type=int,
        default=16,
        help="Tek embedding isteğindeki metin sayısı (429'da otomatik küçülür)",
    )
    parser.add_argument("--upload-batch", type=int, default=200, help="Tek yükleme isteğindeki doküman sayısı")
    parser.add_argument("--limit", type=int, default=None, help="Yalnızca ilk N dokümanı işle (test)")
    parser.add_argument("--progress-every", type=int, default=500, help="Kaç dokümanda bir ilerleme yazılsın")
    parser.add_argument("--recreate", action="store_true", help="Hedef indeksi silip yeniden kur")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Hedefte mevcut kayıtları atlamak yerine hepsini yeniden yaz",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hiçbir şey yazmaz; doküman sayısı ve embedding maliyet tahminini raporlar",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    settings = get_settings()

    source_name = args.source or settings.search_index
    target_name = args.target or default_target_name(source_name)

    if not source_name:
        print("Kaynak indeks belirtilmedi (--source veya .env AZURE_SEARCH_INDEX).")
        return 2
    if source_name == target_name:
        print("Kaynak ve hedef indeks aynı olamaz.")
        return 2
    if target_name.endswith("-index-vector-index"):
        print(
            f"Hedef ad '{target_name}' hatalı (kaynak adına '-vector-index' eklenmiş). "
            f"Beklenen: {default_target_name(source_name)}"
        )
        return 2
    if not settings.aoai_embedding_deployment:
        print("AZURE_OPENAI_EMBEDDING_DEPLOYMENT tanımlı değil.")
        return 2

    print("=" * 70)
    print(" Vektör indeksi kurulumu")
    print("=" * 70)
    print(f"Servis            : {settings.search_endpoint}")
    print(f"Kaynak indeks     : {source_name}  (yalnızca okunur)")
    print(f"Hedef indeks      : {target_name}")
    print(f"Embedding modeli  : {settings.aoai_embedding_deployment} ({VECTOR_DIMENSIONS} boyut)")
    print()

    index_client = SearchIndexClient(
        endpoint=settings.search_endpoint, credential=_search_credential(settings)
    )
    source_index = index_client.get_index(source_name)

    # Alan eşlemesini uygulamanın kendi keşfiyle bul: içerik alanı hangisi?
    source_settings = Settings(search_index=source_name)
    schema = build_schema(source_index, source_settings)
    content_field = schema.content_field
    key_field = schema.key_field
    if not content_field or not key_field:
        print(f"Kaynak indekste içerik ({content_field}) veya anahtar ({key_field}) alanı bulunamadı.")
        return 1
    print(f"İçerik alanı      : {content_field}")
    print(f"Anahtar alan      : {key_field}")

    source_client = SearchClient(
        endpoint=settings.search_endpoint,
        index_name=source_name,
        credential=_search_credential(settings),
    )
    total = int(source_client.get_document_count())
    print(f"Kaynak doküman    : {total}")

    # Vektörlenecek metnin uzunluğundan kaba bir maliyet tahmini çıkar.
    sample_size = min(200, total)
    sample_chars = 0
    sampled = 0
    for doc in read_documents(source_client, [content_field], key_field, limit=sample_size):
        sample_chars += len(str(doc.get(content_field) or ""))
        sampled += 1
    avg_chars = (sample_chars / sampled) if sampled else 0.0
    est_tokens = total * avg_chars / CHARS_PER_TOKEN
    print(
        f"Tahmini maliyet   : ortalama {avg_chars:.0f} karakter/doküman, "
        f"yaklaşık {est_tokens / 1_000_000:.2f}M embedding token"
    )
    print()

    if args.dry_run:
        print("--dry-run: hiçbir değişiklik yapılmadı.")
        source_client.close()
        index_client.close()
        return 0

    # ---------------- hedef indeksi kur ----------------
    existing_names = set(index_client.list_index_names())
    if args.recreate and target_name in existing_names:
        if target_name == source_name:
            print("Kaynak indeks silinemez.")
            return 2
        print(f"'{target_name}' siliniyor (--recreate)...")
        index_client.delete_index(target_name)
        existing_names.discard(target_name)
        time.sleep(3)

    target_index = build_target_index(target_name, source_index, settings, content_field)
    index_client.create_or_update_index(target_index)
    print(f"Hedef indeks hazır: {target_name}")
    print(f"  vektör alanı    : {VECTOR_FIELD} ({VECTOR_DIMENSIONS} boyut, {ALGORITHM_NAME})")
    print(f"  vectorizer      : {VECTORIZER_NAME} -> {settings.aoai_embedding_deployment}")
    semantic_names = [
        c.name for c in (getattr(target_index.semantic_search, "configurations", None) or [])
    ]
    print(f"  semantik        : {', '.join(semantic_names) or '-'}")
    print()

    target_client = SearchClient(
        endpoint=settings.search_endpoint,
        index_name=target_name,
        credential=_search_credential(settings),
    )

    done: Set[str] = set()
    if not args.no_resume and target_name in existing_names:
        done = existing_keys(target_client, key_field)
        if done:
            print(f"Hedefte {len(done)} kayıt zaten var, bunlar atlanacak (devam modu).")

    # ---------------- doldur ----------------
    aoai = build_client(settings)
    deployment = settings.aoai_embedding_deployment
    select = sorted({key_field, content_field, *schema.selectable_fields})

    started = time.time()
    processed = 0
    skipped_empty = 0
    skipped_done = 0
    uploaded = 0
    failed = 0
    pending: List[Dict[str, Any]] = []
    batch_docs: List[Dict[str, Any]] = []
    batch_texts: List[str] = []
    embed_batch_size = max(1, args.embed_batch)

    def flush_upload(force: bool = False) -> None:
        nonlocal uploaded, failed, pending
        while pending and (force or len(pending) >= args.upload_batch):
            chunk = pending[: args.upload_batch]
            pending = pending[args.upload_batch :]
            failures = upload_batch(target_client, chunk)
            failed += failures
            uploaded += len(chunk) - failures

    def on_shrink(new_size: int) -> None:
        nonlocal embed_batch_size
        new_size = max(1, int(new_size))
        if new_size < embed_batch_size:
            embed_batch_size = new_size
            print(f"    sonraki embedding grupları {embed_batch_size} kayıt", flush=True)

    def flush_embed() -> None:
        nonlocal batch_docs, batch_texts
        if not batch_texts:
            return
        vectors = embed_batch(aoai, deployment, batch_texts, on_shrink=on_shrink)
        for doc, vector in zip(batch_docs, vectors):
            doc[VECTOR_FIELD] = vector
            pending.append(doc)
        batch_docs = []
        batch_texts = []
        flush_upload()

    print("Doldurma başlıyor...")
    for doc in read_documents(source_client, select, key_field, limit=args.limit):
        processed += 1
        key = str(doc.get(key_field) or "")
        if not key:
            skipped_empty += 1
            continue
        if key in done:
            skipped_done += 1
            continue

        text = str(doc.get(content_field) or "").strip()
        if not text:
            # Boş içerik vektörleştirilemez; kayıt atlanır ve sayılır.
            skipped_empty += 1
            continue

        batch_docs.append(dict(doc))
        batch_texts.append(_clip(text))

        if len(batch_texts) >= embed_batch_size:
            flush_embed()

        if processed % args.progress_every == 0:
            elapsed = time.time() - started
            rate = processed / elapsed if elapsed else 0.0
            remaining = (total - processed) / rate if rate else 0.0
            print(
                f"  {processed}/{total} okundu, {uploaded} yazıldı, "
                f"{skipped_empty} boş, {skipped_done} atlandı "
                f"({elapsed:.0f} sn, ~{rate:.1f} dok/sn, kalan ~{remaining / 60:.1f} dk)",
                flush=True,
            )

    flush_embed()
    flush_upload(force=True)

    elapsed = time.time() - started
    print()
    print("-" * 70)
    print(f"Okunan doküman     : {processed}")
    print(f"Yazılan doküman    : {uploaded}")
    print(f"Devam modunda atlan: {skipped_done}")
    print(f"Boş içerik (atlandı): {skipped_empty}")
    print(f"Başarısız kayıt    : {failed}")
    print(f"Süre               : {elapsed:.0f} sn ({elapsed / 60:.1f} dk)")

    # İndeksleme asenkron tamamlanır; sayım hemen güncellenmeyebilir.
    time.sleep(5)
    try:
        print(f"Hedef doküman sayısı: {target_client.get_document_count()}")
    except Exception as exc:
        print(f"Hedef doküman sayısı alınamadı: {exc}")

    source_client.close()
    target_client.close()
    index_client.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
