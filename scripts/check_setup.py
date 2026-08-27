"""Kurulum tanılama aracı.

Arayüzü açmadan Azure bağlantılarını ve indeks şemasını doğrular.

    python scripts/check_setup.py
    python scripts/check_setup.py "yıllık izin kaç gün"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402
from src.schema import discover_schema  # noqa: E402

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def section(title: str) -> None:
    print("\n" + title)
    print("-" * len(title))


def check_settings(settings) -> bool:
    section(f"1) Yapılandırma (RAG_BACKEND={settings.backend})")
    missing = settings.missing_for()
    if missing:
        print(f"{FAIL} Eksik ayarlar: {', '.join(missing)}")
        print("       .env.example dosyasını .env olarak kopyalayıp doldurun.")
        return False
    print(f"{OK} Seçili motor için gerekli tüm ayarlar tanımlı.")
    print(f"       Search kimlik doğrulama : {'Entra ID' if settings.search_uses_entra else 'API anahtarı'}")
    if settings.backend == "direct":
        print(f"       OpenAI kimlik doğrulama : {'Entra ID' if settings.aoai_uses_entra else 'API anahtarı'}")
    return True


def check_search(settings, sample_query: str | None):
    section("2) Azure AI Search")
    try:
        schema = discover_schema(settings)
    except Exception as exc:
        print(f"{FAIL} İndekse ulaşılamadı: {exc}")
        try:
            from src.schema import list_index_names

            names = list_index_names(settings)
            if names:
                print("       Bu serviste bulunan indeksler:")
                for name in names:
                    print(f"         - {name}")
                print("       Doğru adı .env içindeki AZURE_SEARCH_INDEX alanına yazın.")
        except Exception:
            pass
        return None

    print(f"{OK} İndeks okundu: {settings.search_index}")
    for key, value in schema.as_summary().items():
        print(f"       {key:<26}: {value}")
    for warning in schema.warnings:
        print(f"{WARN} {warning}")

    from src.retrieval import Retriever

    embedder = None
    if settings.aoai_endpoint and settings.aoai_embedding_deployment:
        try:
            from src.llm import LlmClient

            embedder = LlmClient(settings).embedder()
        except Exception as exc:
            print(f"{WARN} Embedding istemcisi kurulamadı: {exc}")

    retriever = Retriever(settings, schema, embedder=embedder)
    count = retriever.document_count()
    if count is None:
        print(f"{WARN} Doküman sayısı okunamadı.")
    else:
        print(f"{OK} İndekste {count} doküman var.")
        if count == 0:
            print(f"{WARN} İndeks boş. SharePoint indexer'ın çalıştığını kontrol edin.")

    if sample_query:
        try:
            docs, debug = retriever.search(sample_query)
        except Exception as exc:
            print(f"{FAIL} Örnek arama başarısız: {exc}")
            retriever.close()
            return schema
        print(f"{OK} Örnek arama ({debug.get('arama tipi')}): {len(docs)} sonuç")
        for doc in docs:
            print(f"       [{doc.ordinal}] {doc.display_title}")
            print(f"            {doc.url or '(link yok)'}")
            if doc.snippet:
                print(f"            {doc.snippet[:140]}")
    retriever.close()
    return schema


def check_openai(settings):
    section("3) Azure OpenAI")
    try:
        from src.llm import LlmClient

        llm = LlmClient(settings)
        answer = llm.chat(
            [{"role": "user", "content": "Sadece 'hazir' yaz."}], temperature=0.0, max_tokens=10
        )
    except Exception as exc:
        print(f"{FAIL} Sohbet modeli çağrılamadı: {exc}")
        return
    print(f"{OK} Sohbet deployment yanıt verdi ({settings.aoai_chat_deployment}): {answer.strip()[:40]}")

    if settings.aoai_embedding_deployment:
        try:
            vector = llm.embed("test")
            print(f"{OK} Embedding deployment çalışıyor, boyut: {len(vector or [])}")
        except Exception as exc:
            print(f"{FAIL} Embedding çağrılamadı: {exc}")


def check_foundry(settings, sample_query: str | None):
    section("3) Microsoft Foundry Agent Service")
    try:
        from src.engines.foundry import FoundryEngine

        engine = FoundryEngine(settings)
        client = engine._client()  # tanılama amaçlı
        connection = client.connections.get(name=settings.foundry_search_connection)
    except Exception as exc:
        print(f"{FAIL} Foundry projesine bağlanılamadı: {exc}")
        return
    print(f"{OK} Proje bağlantısı kuruldu.")
    print(f"       Search bağlantısı : {connection.name} ({connection.id})")

    if not sample_query:
        print("       Örnek soru vermek için: python scripts/check_setup.py \"sorunuz\"")
        engine.close()
        return

    try:
        result = engine.ask(sample_query)
    except Exception as exc:
        print(f"{FAIL} Agent çalıştırılamadı: {exc}")
        engine.close()
        return

    if result.error:
        print(f"{FAIL} {result.error}")
    else:
        print(f"{OK} Agent yanıt verdi ({result.latency_ms} ms), {len(result.sources)} kaynak.")
        print("       " + result.answer[:300].replace("\n", "\n       "))
        for doc in result.sources:
            print(f"       [{doc.ordinal}] {doc.display_title} -> {doc.url or '(link yok)'}")
    engine.close()


def main() -> int:
    sample_query = sys.argv[1] if len(sys.argv) > 1 else None
    settings = get_settings()

    print("=" * 62)
    print(" SharePoint Bilgi Asistanı - kurulum kontrolü")
    print("=" * 62)

    if not check_settings(settings):
        return 1

    if settings.search_endpoint and settings.search_index:
        check_search(settings, sample_query)

    if settings.backend == "foundry":
        check_foundry(settings, sample_query)
    else:
        check_openai(settings)

    print("\nTamamlandı. Arayüzü başlatmak için:  streamlit run app.py\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
