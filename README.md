# Dipnot

Kitap yazarken SharePoint arşivinden kaynak ve atıf. SharePoint'te bulunan dokümanları
Azure AI Search ile indeksledikten sonra, doğal dille soru sorulduğunda bu bilgi tabanını
tarayıp **kaynak referanslı** yanıt üreten Streamlit uygulaması.

Her yanıtta cevabın dayandığı dokümanlar `[1]`, `[2]` biçiminde metin içinde atıf olarak
verilir; atıflar tıklanabilir ve altta doküman kartları (başlık, tür, güncelleme tarihi,
alaka skoru, SharePoint bağlantısı, kullanılan metin parçası) listelenir.

Yayında ziyaretçi Azure / Search / OpenAI secret girmez: anahtarlar Streamlit Cloud
Secrets'ta (sunucu tarafında) durur. Giriş ekranı varsa yalnızca uygulama kullanıcı adı
ve şifresini ister.

## İki çalışma modu

Uygulama aynı bilgi tabanını iki farklı motorla kullanabilir. Motoru yan menüden veya
`.env` içindeki `RAG_BACKEND` ile seçersiniz.

| | `direct` (varsayılan) | `foundry` |
|---|---|---|
| Arama | Uygulama Azure AI Search'e sorar | Foundry agent, `AzureAISearchTool` ile sorar |
| Yanıt | Azure OpenAI chat deployment | Foundry model deployment |
| Atıflar | Dokümanları biz numaralandırıp modele verdiğimiz için birebir doğrulanabilir | Agent'ın döndürdüğü `url_citation` ek açıklamalarından |
| Yanıt akışı | Token token akar | Tamamlanınca gösterilir |
| Sohbet geçmişi | Uygulamada; takip soruları arama sorgusuna yeniden yazılır | Foundry thread'inde tutulur |

Atıf doğruluğu ve şeffaflık öncelikliyse `direct`, Foundry tarafındaki agent yönetimi /
izleme özelliklerini kullanmak istiyorsanız `foundry` modunu seçin.

## Gereksinimler

- Python 3.10+ (yerel); Streamlit Community Cloud için **Python 3.11** (`runtime.txt`)
- Azure AI Search servisi ve SharePoint indexer ile doldurulmuş bir indeks
- `direct` mod için: Azure OpenAI kaynağı + bir chat deployment (örn. `gpt-4o`)
- `foundry` mod için: Microsoft Foundry projesi, bir model deployment ve projede
  tanımlı bir Azure AI Search bağlantısı

## Kurulum

```powershell
# 1) Sanal ortam ve bağımlılıklar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) Yapılandırma
Copy-Item .env.example .env
notepad .env
```

`.env` içinde en az şunları doldurun (`direct` mod için):

```
AZURE_SEARCH_ENDPOINT=https://<arama-servisi>.search.windows.net
AZURE_SEARCH_INDEX=<indeks-adi>
AZURE_OPENAI_ENDPOINT=https://<aoai-kaynagi>.openai.azure.com
AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o
```

### Kimlik doğrulama

API anahtarı alanlarını **boş bırakırsanız** `DefaultAzureCredential` kullanılır; bu
önerilen yoldur. Bu durumda terminalde oturum açın:

```powershell
az login
```

ve kullanıcınıza (veya uygulama kaydına) şu rolleri verin:

| Kaynak | Rol |
|---|---|
| Azure AI Search | `Search Index Data Reader` |
| Azure OpenAI | `Cognitive Services OpenAI User` |
| Foundry projesi (`foundry` mod) | `Azure AI User` |

Alternatif olarak `AZURE_SEARCH_API_KEY` ve `AZURE_OPENAI_API_KEY` girebilirsiniz.
**Streamlit Community Cloud** üzerinde `az login` / `DefaultAzureCredential` çalışmaz;
orada bu iki API anahtarı zorunludur.

### Kurulumu doğrulama

Arayüzü açmadan bağlantıları test etmek için:

```powershell
python scripts\check_setup.py
python scripts\check_setup.py "satın alma onay limitleri nedir"
```

Bu komut indeks şemasını, doküman sayısını, örnek arama sonuçlarını ve model
erişimini raporlar.

### Çalıştırma

```powershell
streamlit run app.py
```

Tarayıcıda `http://localhost:8501` açılır.

## Alan eşlemesi otomatik keşfedilir

SharePoint indexer'ın ürettiği indekste alanlar `metadata_spo_item_name`,
`metadata_spo_item_weburi`, `content` gibi adlar taşır; entegre vektörleştirme ile
kurulan indekslerde ise `chunk`, `text_vector`, `title` gibi adlar kullanılır.
Uygulama açılışta indeks tanımını okuyup hangi alanın içerik, başlık, adres, tarih,
dosya türü ve vektör alanı olduğunu kendisi belirler. **Tanılama** sekmesinde bulunan
eşlemeyi görebilirsiniz.

Otomatik tahmin yanlışsa `.env` içinden sabitleyebilirsiniz:

```
SEARCH_FIELD_CONTENT=content
SEARCH_FIELD_TITLE=metadata_spo_item_name
SEARCH_FIELD_URL=metadata_spo_item_weburi
SEARCH_FIELD_LAST_MODIFIED=metadata_spo_item_last_modified
SEARCH_FIELD_FILE_TYPE=metadata_spo_item_content_type
SEARCH_FIELD_VECTOR=contentVector
```

## Arama kalitesi

Uygulama indeksin yeteneklerine göre en iyi arama biçimini seçer:

- **Semantik sıralama**: indekste bir *semantic configuration* varsa otomatik açılır.
  Extractive caption'lar referans kartlarındaki özet metinlerde kullanılır.
- **Hibrit arama**: indekste vektör alanı varsa anahtar kelime + vektör araması
  birlikte yapılır. İndekste *vectorizer* tanımlıysa sorgu vektörü Azure AI Search
  tarafında üretilir; tanımlı değilse `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` ayarlayarak
  uygulamanın üretmesini sağlayabilirsiniz.
- **Parça birleştirme**: bölünmüş dokümanların birden fazla parçası tek referans
  kartında toplanır, böylece aynı dosya listede tekrar etmez.
- **Reranker eşiği**: yan menüden alakasız sonuçları eleyebilirsiniz.
- **Takip soruları**: "peki süresi ne kadar?" gibi sorular, sohbet geçmişi kullanılarak
  bağımsız bir arama sorgusuna çevrilir (`RAG_REWRITE_QUERY`).
- **E-postadaki web linkleri**: `.msg` gibi ince içerikli kaynaklarda asıl makale yerine
  yalnızca bir URL olabilir. `direct` motorda (retrieve sonrası, modele gitmeden önce)
  metni kısa ve link ağırlıklı dokümanlardaki açık http(s) sayfaları çekilip kaynağa
  eklenir. Abonelik/paywall siteleri (WSJ, NYT, FT, Economist, Bloomberg, HBR, Medium,
  LinkedIn, Facebook, X, Instagram), özel IP/localhost (SSRF), 401/403 ve PDF/Office
  indirme adresleri atlanır. Kapatmak için `WEB_FETCH_ENABLED=false`. Soru başına üst
  sınır `WEB_FETCH_MAX_PER_QUESTION` (varsayılan 6); kaynak başına en fazla 2 URL.

Kalite için en etkili iki adım: indekse **semantic configuration** eklemek ve
dokümanları **chunk**'lara bölerek vektörleştirmek.

Bu projedeki varsayılan indeks `fe-partial-data-vector-index`: kaynak
`fe-partial-data-index` (yalnızca metin) silinmeden yanında vektör kopyası kurulur.
`snippet_vector` (3072 boyut, HNSW) ve Azure OpenAI `text-embedding-3-large`
vectorizer ile sorgu tipi **hibrit + semantik** olur.

```powershell
python scripts\build_vector_index.py
# kaynak: fe-partial-data-index  →  hedef: fe-partial-data-vector-index
```

## Model uyumluluğu ve kota dayanıklılığı

Model aileleri parametre konusunda ayrışır ve uygulama bunu kendisi çözer:

| Biçim | Gönderilen parametreler | Tipik model |
|---|---|---|
| `standard` | `temperature` + `max_tokens` | gpt-4o, gpt-4.1 |
| `reasoning` | `max_completion_tokens` (temperature yok) | gpt-5 ailesi, o-serisi |
| `minimal` | hiçbiri | parametre kabul etmeyen dağıtımlar |

İlk istekte bu biçimler sırayla denenir, çalışan biçim hatırlanır ve sonraki isteklerde
doğrudan o kullanılır. Böylece her soruda başarısız bir deneme yapılmaz. Hangi biçimin
seçildiğini **Tanılama** sekmesinde "Model parametre biçimi" satırında görebilirsiniz.

Akıl yürütme modelleri, görünmeyen düşünme adımlarında da token harcar. `RAG_MAX_TOKENS`
yalnızca görünür yanıtı hedeflediğinden, `reasoning` biçiminde isteğe
`RAG_REASONING_BUDGET` kadar ek pay eklenir (`max_completion_tokens = RAG_MAX_TOKENS +
RAG_REASONING_BUDGET`). Bu pay olmazsa model düşünme aşamasında limiti tüketip **boş
yanıt** döndürebilir. Yanıtlar beklenmedik şekilde boş geliyorsa bu değeri artırın.

Azure OpenAI kota sınırına (`429`) takılan istekler, sunucunun bildirdiği `retry-after`
süresi beklenerek otomatik olarak yeniden denenir (en fazla 3 kez). Sürekli 429
alıyorsanız dağıtımın dakikalık token kotasını (TPM) yükseltmek kalıcı çözümdür;
geçici olarak `RAG_TOP_K` ve `RAG_MAX_CHARS_PER_DOC` değerlerini düşürmek de istek
boyutunu küçültür.

## Arayüz

Varsayılan görünüm sadedir: yalnızca **Sohbet** ekranı açılır, yan menüde "Yeni sohbet"
düğmesi ve geçmiş sohbetler listelenir. Teknik ayarların tamamı (motor seçimi, doküman
sayısı, sıralama anahtarları, eleme eşiği, yanıt çeşitliliği, OData filtresi) yan
menüdeki varsayılan olarak kapalı **Gelişmiş ayarlar** bölümündedir.

Teknik sekmeleri ileri kullanıcılar için açmak isterseniz `.env` içinde
`APP_SHOW_ADVANCED_TABS=true` yapın; o zaman şu iki sekme daha görünür:

- **Kaynak tarayıcı** — model devreye girmeden yalnızca indeksi sorgular. "Doğru
  dokümanlar geliyor mu?" sorusunu yanıtlamak için kullanın.
- **Tanılama** — motor durumu, keşfedilen indeks şeması, ortam değişkenleri, doküman
  sayısı ve uyarılar.

### Sohbet geçmişi

Sohbetler yerel bir SQLite dosyasında (varsayılan `data/chats.db`, `APP_CHAT_DB_PATH`
ile değiştirilebilir) saklanır; sayfa yenilense de kaybolmaz. Başlık ilk sorudan
üretilir, liste en son güncellenen sohbet başta olacak şekilde sıralanır ve eski bir
sohbete tıklandığında yanıtlar, kaynak kartları ve atıflar birlikte geri yüklenir.
`foundry` modunda her sohbetin Foundry thread'i de saklanır; eski sohbete dönüldüğünde
aynı thread kullanılır. `data/` klasörü `.gitignore` içindedir.

**Streamlit Community Cloud** diski kalıcı değildir: uygulama uyuyunca, yeniden
başlatılınca veya yeniden dağıtılınca `data/chats.db` silinir. Arayüz bu durumda bir
uyarı gösterir. Kalıcı sohbet için kendi sunucunuz veya harici bir veritabanı gerekir.

### Referans bağlantıları

Her referans kartında iki seçenek vardır: birincil **SharePoint'te aç** düğmesi dosyayı
SharePoint web arayüzünde, bulunduğu klasör açık ve dosya seçili olacak şekilde gösterir;
ikincil **Dosyayı indir** bağlantısı dosyanın kendi adresine gider. Metin içindeki `[1]`
atıfları `SHAREPOINT_LINK_MODE` ayarındaki biçimi kullanır (`browse` varsayılan,
`direct` doğrudan dosya adresi).

Web arayüzü adresi şu kalıpta üretilir:

```
{site}/{kitaplık}/Forms/AllItems.aspx?id={dosyanın sunucuya göreli yolu}&parent={üst klasör}
```

Bağlantı açılmıyorsa `SHAREPOINT_DOC_LIBRARY` değerini kontrol edin (Türkçe arayüzlü
sitelerde `Belgeler` olabilir) veya `SHAREPOINT_LINK_MODE=direct` yapın.

## Testler

Azure bağlantısı gerektirmeyen iki test seti vardır:

```powershell
python scripts\smoke_test.py   # şema keşfi, atıf, bağlantı, sohbet geçmişi, web çekme (paywall/SSRF)
python scripts\app_test.py     # arayüzün uçtan uca testi (Streamlit AppTest)
```

`app_test.py`, `app.py`'yi gerçekten çalıştırır ancak Azure çağrılarını sahte
nesnelerle değiştirir; soru gönderme, atıfların linke çevrilmesi, referans kartları,
sade yan menü, geçmiş bir sohbetin yeni oturumda geri yüklenmesi, sohbet silme ve hata ekranları
doğrulanır. Testler sohbet geçmişini geçici bir klasöre yazar, `data/chats.db`
dosyasına dokunmaz.

## Proje yapısı

```
app.py                    Streamlit arayüzü (sohbet, sade yan menü, sohbet geçmişi)
requirements.txt
runtime.txt               Streamlit Cloud Python sürümü (3.11)
.env.example              Tüm yapılandırma seçenekleri, açıklamalı (gerçek secret yok)
.streamlit/secrets.toml.example  Cloud Secrets şablonu (git'e gerçek değer koyma)
scripts/check_setup.py    Bağlantı ve şema tanılama aracı (CLI)
scripts/inspect_index.py  İndeks alanlarını ve örnek dokümanı ayrıntılı gösterir
scripts/build_vector_index.py  Metin indeksinin yanına vektör kopyası kurar ve doldurur
scripts/ask.py            Terminalden soru sorma (--verbose ile ayrıntılı log)
scripts/smoke_test.py     Bağlantısız birim testleri
scripts/app_test.py       Arayüz testi (Streamlit AppTest)
src/
  config.py               .env ve st.secrets okuma, ayar doğrulama
  schema.py               İndeks şemasının otomatik keşfi (alan eşlemesi)
  retrieval.py            Azure AI Search sorgulama, parça birleştirme, normalizasyon
  webcontent.py           İnce içerikteki açık web linklerinden metin çekme (SSRF/paywall)
  llm.py                  Azure OpenAI istemcisi (chat akışı + embedding)
  prompts.py              Sistem talimatı ve atıf kuralları, bağlam biçimlendirme
  models.py               SourceDoc / AnswerResult, atıf ayrıştırma ve linkleme
  history.py              Sohbet geçmişinin SQLite'ta saklanması (data/chats.db)
  engines/
    base.py               Ortak motor arayüzü ve AskOptions
    direct.py             Azure AI Search + Azure OpenAI RAG motoru
    foundry.py            Foundry Agent Service motoru (AzureAISearchTool)
  ui.py                   Referans kartları, CSS, akış yazımı, kurulum yardımı
```

## Sık karşılaşılan sorunlar

| Belirti | Sebep / çözüm |
|---|---|
| `403 Forbidden` (Search) | `Search Index Data Reader` rolü yok ya da arama servisinde RBAC kapalı. Portal → Keys → "Role-based access control" |
| `401` (Azure OpenAI) | `Cognitive Services OpenAI User` rolü eksik veya `az login` yapılmamış |
| Referanslarda link yok | İndekste doküman adresi alanı yok. `SEARCH_FIELD_URL` ile belirtin (SharePoint indexer: `metadata_spo_item_weburi`) |
| Referans linki dosyayı indiriyor | `SHAREPOINT_LINK_MODE=browse` olmalı (varsayılan). Dosyayı bilerek indirmek isterseniz karttaki "Dosyayı indir" bağlantısını kullanın |
| "SharePoint'te aç" sayfa bulunamadı diyor | `SHAREPOINT_DOC_LIBRARY` yanlış. Kitaplığın SharePoint'teki gerçek adını yazın (Türkçe sitelerde `Belgeler`); geçici çözüm olarak `SHAREPOINT_LINK_MODE=direct` |
| "İlgili doküman bulunamadı" | İndeks boş olabilir (indexer çalışmamış), OData filtresi çok dar veya reranker eşiği yüksek |
| Semantik sıralama kapalı görünüyor | İndekste semantic configuration tanımlı değil; Azure portaldan ekleyin |
| Aynı dosya birçok kez listeleniyor | Parça birleştirmeyi açın (`RAG_MERGE_CHUNKS=true`) |
| `foundry` modda kaynak listesi boş | Model atıf üretmemiş olabilir. Indekste URL alanı bulunduğundan ve agent talimatının korunduğundan emin olun; `FOUNDRY_QUERY_TYPE` değerini `simple` ile deneyin |
| `Unsupported parameter: 'max_tokens'` | Akıl yürütme modeli; uygulama otomatik olarak `max_completion_tokens` biçimine geçer, işlem gerekmez |
| Yanıt boş geliyor | Akıl yürütme modeli limiti düşünme adımlarında tüketmiş olabilir; `RAG_REASONING_BUDGET` değerini artırın |
| `429 rate_limit_exceeded` | Dağıtımın dakikalık token kotası yetersiz. Uygulama bekleyip tekrar dener; kalıcı çözüm TPM kotasını yükseltmek |
| İndeks adı bulunamadı | Uygulama ve `check_setup.py`, servisteki mevcut indeksleri listeler; doğru adı `AZURE_SEARCH_INDEX` alanına yazın |

## Streamlit Community Cloud yayın

Ziyaretçi Azure / Search / OpenAI anahtarı **girmez.** Bu değerler sunucu tarafında
(Streamlit Cloud Secrets) durur; uygulama `st.secrets` okur. Giriş formu varsa yalnızca
uygulama kullanıcı adı ve şifresini ister — onlar da Secrets'tadır, tarayıcıda key
alanı yoktur. Siz Cloud'da Secrets'ı **bir kez** yapıştırırsınız; sonra uygulama
anahtarlar yüklü halde ayakta kalır.

Uygulamayı [share.streamlit.io](https://share.streamlit.io) üzerinde ücretsiz
barındırabilirsiniz. Streamlit sizin GitHub hesabınıza bağlanır; bu araç tarayıcıdan
sizin yerinize giriş yapamaz.

### 1) Kodu GitHub'a alın (private repo)

Depo adı: **DipnotAgent**. API anahtarları ve kurum içi indeks adları için **private**
depo kullanın. Public repoda kaynak kod görünür; secrets panele yazılsa bile
yanlışlıkla anahtar sızdırma riski artar.

```powershell
git init
git add .
git commit -m "Streamlit Cloud için secrets desteği ve yayın notları."
# GitHub CLI varsa:
gh repo create DipnotAgent --private --source=. --remote=origin --push
```

`gh` yoksa GitHub'da **New repository** (Private, ad: `DipnotAgent`) oluşturup:

```powershell
git remote add origin https://github.com/<kullanici>/DipnotAgent.git
git branch -M main
git push -u origin main
```

**Asla commit etmeyin:** `.env`, `.streamlit/secrets.toml`, `data/`. Şablonlar
(`.env.example`, `.streamlit/secrets.toml.example`) gerçek anahtar içermez.

### 2) Streamlit Cloud'da uygulama oluşturun

1. [https://share.streamlit.io](https://share.streamlit.io) adresine gidin ve GitHub ile giriş yapın.
2. **New app** (Create app) seçin.
3. Repository (`DipnotAgent`), branch (`main`) ve Main file path: `app.py` seçin.
4. Advanced settings:
   - Python version: **3.11** (repodaki `runtime.txt` de `python-3.11` der).
   - **Secrets:** yereldeki `.streamlit/secrets.toml` (gitignore'da; `.env`'den üretilir)
     içeriğini yapıştırın. Şablon için `.streamlit/secrets.toml.example`.
5. Deploy. Ziyaretçi bu panoyu görmez; yalnızca sizin hesabınız doldurur.

### 3) Cloud'da doldurulacak secret'lar

Değerleri buraya yazmayın; Streamlit Secrets panosuna yapıştırın. Cloud'da Entra ID
yoktur — Search ve OpenAI **API key** şarttır. Foundry kullanmayacaksanız Foundry
satırlarını boş bırakabilirsiniz.

| Secret | Zorunlu | Not |
|---|---|---|
| `AZURE_SEARCH_ENDPOINT` | evet | `https://….search.windows.net` |
| `AZURE_SEARCH_INDEX` | evet | indeks adı |
| `AZURE_SEARCH_API_KEY` | evet (Cloud) | Query key yeterli |
| `AZURE_OPENAI_ENDPOINT` | evet (`direct`) | kaynak kökü |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | evet (`direct`) | deployment adı |
| `AZURE_OPENAI_API_KEY` | evet (Cloud) | |
| `AZURE_OPENAI_API_VERSION` | hayır | varsayılan `2024-10-21` |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | hayır | hibrit arama için |
| `APP_AUTH_USER` | önerilir | giriş ekranı |
| `APP_AUTH_PASSWORD` | önerilir | doluysa login zorunlu |
| `RAG_BACKEND` | hayır | `direct` veya `foundry` |
| `FOUNDRY_PROJECT_ENDPOINT` | `foundry` ise | |
| `FOUNDRY_MODEL_DEPLOYMENT` | `foundry` ise | |
| `FOUNDRY_SEARCH_CONNECTION_NAME` | `foundry` ise | |
| `SHAREPOINT_SITE_URL` | hayır | browse linkleri için |
| diğer `RAG_*` / `APP_*` | hayır | yereldeki `.env` ile aynı adlar |

Tam liste: `.streamlit/secrets.toml.example`.

### 4) Sohbet geçmişi (SQLite) kalıcı değildir

Cloud konteynerindeki disk ephemeral'dır. `data/chats.db` uygulama restart, uyku veya
yeniden deploy sonrası **silinir**. Bu bir hata değil, platform sınırıdır. Uygulama
Cloud'da bu konuda bir uyarı gösterir.

### 5) Güvenlik

- Repoyu **private** tutun.
- API anahtarlarını ve `APP_AUTH_PASSWORD` değerini yalnızca Streamlit Secrets'a yazın;
  README, commit veya public URL'de paylaşmayın. Ziyaretçi bu panoyu görmez ve Azure
  anahtarı girmez.
- Login secret'larını boş bırakırsanız Cloud URL'sini bilen herkes indeksteki tüm
  dokümanları sorabilir.

## Güvenlik notu

Bu uygulama tek bir servis kimliğiyle indeksin tamamını okur; **kullanıcı bazlı SharePoint
izin kısıtlaması (security trimming) uygulamaz.** Dolayısıyla uygulamaya erişen herkes
indeksteki tüm dokümanların içeriğini görebilir. Farklı yetki seviyelerine sahip
kullanıcılar olacaksa ya indekse yalnızca herkese açık dokümanları alın, ya da indekse
izin grubu alanı ekleyip her sorguda kullanıcının gruplarına göre OData filtresi
uygulayın.
