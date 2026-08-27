"""Model talimatları ve bağlam biçimlendirme."""

from __future__ import annotations

from typing import List

from .models import SourceDoc

SYSTEM_PROMPT = """Sen bir kurumsal doküman asistanısın. Görevin, SharePoint'ten \
indekslenmiş dokümanlardan oluşan bir bilgi tabanına dayanarak soruları yanıtlamaktır.

KURALLAR:
1. Yanıtını YALNIZCA sana verilen "KAYNAKLAR" bölümündeki bilgilere dayandır. \
Kendi genel bilgini kullanma, tahmin yürütme, bilgi uydurma.
2. Kullandığın her bilginin sonuna kaynak numarasını köşeli parantezle ekle: [1], [2]. \
Bir cümle birden fazla kaynaktan geliyorsa [1][3] biçiminde yaz.
3. Kaynaklarda cevap yoksa bunu açıkça söyle: "Bilgi tabanındaki dokümanlarda bu \
soruya dair bir bilgi bulamadım." Ardından varsa en yakın ilgili dokümanı belirt.
4. Kaynaklar birbiriyle çelişiyorsa çelişkiyi belirt ve dokümanların tarihlerine dikkat çek.
5. Soru hangi dilde sorulduysa o dilde yanıtla. Türkçe soruya Türkçe yanıt ver.
6. Kısa ve net yaz. Uygun olduğunda madde işaretleri ve başlıklar kullan. \
Prosedür/adım anlatıyorsan numaralı liste kullan.
7. Sayı, tarih, tutar, kişi adı, versiyon gibi kritik değerleri kaynaktan aynen aktar; yuvarlama yapma.
8. Yanıtın sonuna kaynak listesi YAZMA; kaynaklar arayüzde ayrıca gösteriliyor.
9. Alakasız kaynakları kullanma. Yalnızca soruyla doğrudan ilgili cümleleri aktar. \
Bir kaynak soruyu yanıtlamıyorsa onu atıfta kullanma.
10. Bir iddianın kaynakta geçtiğinden emin değilsen "bu kaynakta yok" de; boşluğu doldurma.
"""

NO_RESULTS_MESSAGE = (
    "Bilgi tabanında bu soruyla ilgili doküman bulamadım. Soruyu farklı kelimelerle "
    "ifade etmeyi ya da yan menüden filtreleri gevşetmeyi deneyebilirsiniz."
)

FOUNDRY_INSTRUCTIONS = SYSTEM_PROMPT + """
Bilgi tabanına Azure AI Search aracıyla erişiyorsun. Soruyu yanıtlamadan önce mutlaka \
bu araçla arama yap. Aramadan dönen dokümanlara atıf yap.
"""


def format_sources_block(sources: List[SourceDoc]) -> str:
    """Kaynakları modele verilecek numaralı bağlam bloğuna çevirir."""
    blocks: List[str] = []
    for doc in sources:
        meta = []
        if doc.file_type:
            meta.append(f"Tür: {doc.file_type}")
        if doc.last_modified:
            meta.append(f"Son değişiklik: {doc.last_modified}")
        header = f"[{doc.ordinal}] Doküman: {doc.display_title}"
        if doc.url:
            header += f"\nAdres: {doc.url}"
        if meta:
            header += "\n" + " | ".join(meta)
        blocks.append(f"{header}\nİçerik:\n{doc.content}")
    return "\n\n---\n\n".join(blocks)


def build_user_message(question: str, sources: List[SourceDoc]) -> str:
    return (
        "KAYNAKLAR:\n"
        f"{format_sources_block(sources)}\n\n"
        "---\n\n"
        f"SORU: {question}\n\n"
        "Yukarıdaki kaynaklara dayanarak yanıtla ve kullandığın kaynakları [numara] "
        "biçiminde belirt."
    )
