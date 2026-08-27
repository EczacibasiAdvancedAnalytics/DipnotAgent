"""Terminalden soru sorma aracı (arayüz açmadan).

    python scripts/ask.py "siber güvenlikte insan faktörü neden önemli"
    python scripts/ask.py --backend foundry "sorunuz"
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402
from src.engines import build_engine  # noqa: E402
from src.models import linkify_citations  # noqa: E402


def main() -> int:
    args = list(sys.argv[1:])

    if "--verbose" in args:
        args.remove("--verbose")
        logging.basicConfig(level=logging.INFO, format="  [log] %(message)s")
        # Azure/HTTP istemcilerinin ayrıntılı istek dökümü çıktıyı okunmaz hale getiriyor.
        for noisy in ("azure", "httpx", "httpcore", "openai", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    backend = None
    if "--backend" in args:
        index = args.index("--backend")
        backend = args[index + 1]
        del args[index : index + 2]

    if not args:
        print('Kullanım: python scripts/ask.py "sorunuz"')
        return 2

    question = " ".join(args)
    settings = get_settings()
    backend = backend or settings.backend

    missing = settings.missing_for(backend)
    if missing:
        print(f"Eksik ayarlar: {', '.join(missing)}")
        return 1

    engine = build_engine(settings, backend)
    print(f"Motor : {backend}")
    print(f"Soru  : {question}\n")

    result = engine.ask(question)

    if result.error:
        print(f"HATA: {result.error}")
        engine.close()
        return 1

    print("YANIT")
    print("-" * 70)
    print(result.answer)
    print()
    print(f"REFERANSLAR ({len(result.cited_sources)} kullanıldı / {len(result.sources)} bulundu)")
    print("-" * 70)
    for doc in result.sources:
        mark = "*" if doc.ordinal in result.cited_ordinals else " "
        score = f"{doc.best_score:.2f}" if doc.best_score is not None else "-"
        print(f" {mark} [{doc.ordinal}] {doc.display_title}  (skor {score}, {doc.chunk_count} parça)")
        print(f"       {doc.url or doc.extra.get('yol') or '(adres yok)'}")

    print(f"\nSüre: {result.latency_ms} ms")
    for key, value in (result.debug or {}).items():
        print(f"  {key}: {value}")

    print("\nMOTOR DURUMU")
    print("-" * 70)
    for key, value in engine.health().items():
        print(f"  {key}: {value}")

    engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
