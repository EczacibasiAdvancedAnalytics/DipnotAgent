"""İndeks tanımını ayrıntılı yazdırır: alanlar, semantik yapılandırma, vektör ayarları.

Alan eşlemesi beklenmedik çıktığında bu araçla indeksin gerçek yapısını görün.

    python scripts/inspect_index.py
    python scripts/inspect_index.py --sample     # örnek bir dokümanın alan değerleri
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import get_settings  # noqa: E402
from src.schema import _credential, list_index_names  # noqa: E402


def main() -> int:
    settings = get_settings()
    show_sample = "--sample" in sys.argv

    from azure.search.documents.indexes import SearchIndexClient

    client = SearchIndexClient(endpoint=settings.search_endpoint, credential=_credential(settings))

    print(f"Servis  : {settings.search_endpoint}")
    print(f"İndeksler: {', '.join(list_index_names(settings))}")
    print(f"Seçili  : {settings.search_index}\n")

    index = client.get_index(settings.search_index)

    print("ALANLAR")
    print("-" * 78)
    for f in index.fields:
        flags = []
        for attr, label in (
            ("key", "key"),
            ("searchable", "searchable"),
            ("filterable", "filterable"),
            ("sortable", "sortable"),
            ("facetable", "facetable"),
        ):
            if getattr(f, attr, False):
                flags.append(label)
        if getattr(f, "hidden", False):
            flags.append("hidden")
        dims = getattr(f, "vector_search_dimensions", None)
        if dims:
            flags.append(f"vector({dims})")
        type_name = getattr(f.type, "value", f.type)
        print(f"  {f.name:<34} {str(type_name):<28} {', '.join(flags)}")

    print("\nSEMANTİK YAPILANDIRMA")
    print("-" * 78)
    semantic = getattr(index, "semantic_search", None)
    if not semantic:
        print("  yok")
    else:
        print(f"  varsayılan: {getattr(semantic, 'default_configuration_name', None)}")
        for cfg in getattr(semantic, "configurations", None) or []:
            pf = getattr(cfg, "prioritized_fields", None)
            title = getattr(getattr(pf, "title_field", None), "field_name", None)
            contents = [x.field_name for x in (getattr(pf, "content_fields", None) or [])]
            keywords = [x.field_name for x in (getattr(pf, "keywords_fields", None) or [])]
            print(f"  ad: {cfg.name}")
            print(f"     başlık alanı   : {title}")
            print(f"     içerik alanları: {contents}")
            print(f"     anahtar kelime : {keywords}")

    print("\nVEKTÖR ARAMA")
    print("-" * 78)
    vector = getattr(index, "vector_search", None)
    if not vector:
        print("  yok")
    else:
        for p in getattr(vector, "profiles", None) or []:
            vname = getattr(p, "vectorizer_name", None) or getattr(p, "vectorizer", None)
            print(f"  profil {p.name}: algoritma={p.algorithm_configuration_name} vectorizer={vname}")
        for v in getattr(vector, "vectorizers", None) or []:
            print(f"  vectorizer: {getattr(v, 'vectorizer_name', None) or getattr(v, 'name', None)}")

    client.close()

    if show_sample:
        print("\nÖRNEK DOKÜMAN")
        print("-" * 78)
        from azure.search.documents import SearchClient

        search_client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=settings.search_index,
            credential=_credential(settings),
        )
        results = search_client.search(search_text="*", top=1)
        for doc in results:
            for key, value in doc.items():
                if key.startswith("@search"):
                    continue
                text = str(value)
                if len(text) > 300:
                    text = text[:300] + f" … (toplam {len(str(value))} karakter)"
                print(f"  {key:<34} {text}")
        search_client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
