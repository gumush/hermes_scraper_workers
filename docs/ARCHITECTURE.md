# Mimari

## Neden iki taraf

Google Maps'ten yorum toplamak IP başına yavaştır ve tek bir adresten uzun
süre çekmek engellenmeye yol açar. Çözüm işi çoğaltmak değil, **adresi**
çoğaltmaktır: farklı bölgelerde geçici VM'ler açılır, her biri kendi çıkış
IP'siyle birkaç mekan kazır, işi bitince silinir.

Bu yüzden kod iki tarafa ayrılmıştır ve ayrım koddan da okunur:

```
orchestrator/   →  workers/  ✗   (import yok)
workers/        →  orchestrator/ ✗ (import yok)
```

Aralarındaki tek bağ, SSH tüneli üzerinden konuşulan bir HTTP API'sidir.
Bu ayrımı bozmadan tarafları ayrı ayrı çalıştırabilir, test edebilir,
değiştirebilirsin.

---

## Bir işin yaşam döngüsü

```
    orchestrator                         worker (VM)
    ────────────                         ───────────
 1  VM oluştur (gcloud)          ──▶
 2  kodu kopyala, venv kur       ──▶     $HOME/scraper
 3  worker'ı systemd ile başlat  ──▶     python -m workers.server
 4  SSH tüneli aç                ◀─▶     localhost:925x ↔ vm:8100
 5  /health yokla                ──▶     {"ok": true}
 6  POST /jobs {place_id, …}     ──▶     kuyruğa al
 7                                       kazı → paketle
 8  GET /jobs/{id} yokla         ──▶     {"state": "transfer_queued"}
 9  GET /jobs/{id}/download      ──▶     <place_id>_<job>.tar.gz
10  outputs/<run>/<place_id>/ altına aç
11  iş bitince VM'i sil          ──▶     (kaynak serbest)
```

Adım 3'te `nohup … &` **kullanılmaz**. Ölçüldü: SSH kanalını açık tutuyor ve
bağlantı 45 saniyeden fazla asılı kalıyordu; `systemd-run` 1.9 saniyede
dönüyor çünkü systemd süreci devraldığı anda çıkıyor.

---

## Orchestrator

### Durumlar

Bir **exec** (bir run'ın bir çalıştırması) şu durumlardan geçer:

```
starting → provisioning → running → closing → closed
                              ↓
                            error
```

Bir **iş** (bir mekan):

```
pending → scraping → transferring → done
             ↓
          failed          (deneme hakkı bittiğinde)
```

Bir **VM**:

```
pending → provisioning → ready → deleted        (normal)
                           ↓
                         lost                   (cevap vermiyor / elendi)
                           ↓
                        failed                  (hiç açılamadı)
```

Her durum değişimi **sebebiyle** kaydedilir (`state_reason`). "lost" tek
başına makinenin mi durduğunu yoksa üst üste hata verdiği için mi elendiğini
söylemez; izleyen kişi için sebep etiketin kendisinden daha değerlidir.

### İş dağıtımı

- Her VM'e `slots` kadar eşzamanlı iş verilir, üstüne **bir tane fazla**
  (`QUEUE_BUFFER`) kuyruğa konur ki bir slot boşaldığında beklemeden başlasın.
  Bu yüzden arayüzde bir VM'in "aktif" sayısı `slots + 1` görünebilir; aynı
  anda kazıyan tarayıcı sayısı `slots` kadardır.
- Bir iş başarısız olursa **başka bir VM'e** verilir (`_take_job`, o işte daha
  önce başarısız olmuş VM'i tercih etmez). Deneme sayısı run başına ayarlanır
  (Ayarlar → deneme sayısı, varsayılan 3).
- Kuyruk azaldıkça fazla VM'ler **drain** edilip silinir (`DRAIN_SPARE` kadarı
  ihtiyat olarak bırakılır).

### VM sağlığı

Üst üste `VM_FAIL_LIMIT` (3) başarısız iş, VM'i emekli eder ve yerine başka
bölgede yenisi açılır — amaç farklı bir adrestir.

Ama **her hata VM'in suçu değildir.** Hata paketinin içindeki kod okunur
(`_bundle_codes`) ve sayfayı tarif eden kodlar (`PAGE_FAULT_CODES`) ardışık
sayaca yazılmaz. Ölçüm: sayfa hatalarını VM'e yazmak bir run'da üç sağlam
makineyi elemişti; aynı hataların 18'inden 14'ü sonraki denemede tam teslim
edildi.

### Kapanış

`/api/shutdown` çağrıldığında VM'ler hemen silinmez: önce **drain** edilir —
üzerlerinde hazır bekleyen paketler `DRAIN_TIMEOUT` (90 sn) içinde paralel
olarak çekilir, sonra hepsi silinir. Arayüz drain sırasında kaç paketin
kurtarıldığını yazar; sessiz bir "closing" durumu, kimsenin ne beklediğini
bilmediği bir durumdur.

`/api/verify` `gcloud`'a sorup gerçekten instance kalmadığını doğrular —
kendi kaydına değil, sağlayıcının cevabına bakar.

---

## Worker

Tek bir VM'de çalışan küçük bir FastAPI uygulaması. Durumu diskte
(`spot_out/jobs.json`) tutar, böylece süreç yeniden başlarsa işler kaybolmaz.

### İş durumları

```
queued → scraping → packaging → transfer_queued → transferring → done
                          ↓                              ↓
                        error                      transfer_error
                          ↓
                      cancelled
```

`SPOT_SCRAPE_WORKERS` kadar kazıma iş parçacığı çalışır; orchestrator bunu
`slots` değerinden ayarlar.

### Paket biçimi

```
<place_id>/
├── info.json
├── reviews.json
├── images.json
└── images/
    ├── ownerimages/
    └── reviewimages/
```

Paket **işletme bilgileri olmadan üretilmez**: eksik paket teslim edilmeye
değmez, iş orada düşer ve başka bir VM'de tekrar denenir.

### Hata paketi

`capture_failures` açıkken bir iş düştüğünde geriye kalan her şey ayrı bir
tarball'a konur:

```
<job_id>-failure/
├── request.json                       hangi iş, hangi ayarlarla
├── scrape.log                         o işin tüm logu
└── diagnostics/<saat>_<kod>/
    ├── screen.png                     ekran görüntüsü
    ├── page.html                      o andaki DOM
    └── meta.json                      url, başlık, sekmeler, yorum kartı sayısı
```

Bu, "muhtemelen şöyle olmuştur" ile "sayfa şuydu" arasındaki farktır. VM
dakikalar sonra silinir; o pencere kaçarsa geriye yalnızca tahmin kalır.

---

## Kazıma motoru (`workers/engine/`)

Aşamalar ve her birinin doğrulaması:

| aşama | ne yapar | doğrulama |
|---|---|---|
| `navigating` | Maps URLs API ile mekana git | çözülen URL'den **ftid** okunur, kimlik buna göre saklanır |
| `reviews_tab` | yorum durumunu belirle | üç durumlu: `has` / `none` / `unknown` (aşağıya bak) |
| `reviews` | yorumları topla | hedefe ulaşılamazsa red flag; `max_reviews_cap` tavanı |
| `details` | işletme bilgileri | her yeniden yüklemede ftid kimlik kontrolü |
| `photos` | owner galerisi | **en sona bırakılır** — galeri overlay'i tarayıcıyı sayfada bırakabiliyor |
| `post` | paketleme | işletme bilgisi yoksa paket üretilmez |

### Yorum durumu neden üç değerli

Yorumu olmayan bir işletmenin sayfasında Reviews sekmesi **yoktur** — bu
normal bir durumdur, hata değil. Ama yarı yüklenmiş bir sayfa da tıpatıp aynı
görünür. "Sekme yok, demek ki yorumu yok" demek, boş paketleri tam paket diye
kaydetmek olurdu.

Bu yüzden:

- `has` — **pozitif kanıt**: yorum kartı var ya da Reviews sekmesi var. Anında
  kabul edilir.
- `none` — yokluk. Yalnızca panelin gerçekten render olduğu kanıtlanırsa
  (adres tek başına yeterli, yoksa 2+ alan) **ve** işletmenin kendi puanı
  yoksa döner. Üstelik `REVIEWLESS_CONFIRM` saniye boyunca sabit kalmalıdır.
- `unknown` — hiçbiri kanıtlanamadı. Hata paketi üretilir, iş başka VM'de
  tekrar denenir.

Puan araması `div.F7nice` bloğuyla sınırlıdır ve içinde rakam olmalıdır.
Sayfanın tamamında yıldız aramak komşu işletmelerin puanlarını buluyordu —
ölçülen bir sayfada `role=main` içinde dört yabancı puan vardı.

---

## HTTP arayüzleri

### Orchestrator

| uç | ne yapar |
|---|---|
| `GET /` | web arayüzü |
| `GET /api/status` | çalışan exec'in tam anlık görüntüsü + son 40 log satırı |
| `POST /api/start` | run başlat |
| `POST /api/pause` | duraklat / devam et |
| `POST /api/shutdown` | drain + tüm VM'leri sil |
| `POST /api/restart` | koordinatörü yeniden yükle (çalışan run varken reddedilir) |
| `GET /api/verify` | gcloud'a sorup açık VM kalmadığını doğrula |
| `GET /api/runs` · `GET /api/browse/runs` | run listesi |
| `GET /api/runs/{file}/footprint` | silme öncesi ne gideceği |
| `DELETE /api/runs/{file}?keep_json=` | run verisini sil |
| `GET /api/zones` | bölge başarı istatistikleri |
| `GET /api/browse/{run}/{exec}/places` | teslim listesi |
| `GET /api/browse/{run}/{exec}/place/{id}` | tek mekanın detayı |
| `GET /api/browse/{run}/{exec}/failed` | teslim edilmemişler (retry girdisi) |
| `GET /api/profiles` · `POST` · `DELETE` | profiller |

### Worker

Hepsi `x-api-key` ister.

| uç | ne yapar |
|---|---|
| `GET /health` | canlılık + kuyruk sayıları |
| `POST /jobs` | iş kuyruğa al |
| `GET /jobs` · `GET /jobs/{id}` | durum |
| `DELETE /jobs/{id}` | iptal |
| `GET /jobs/{id}/download` | paketi indir |
| `GET /jobs/{id}/failure` | hata paketini indir |

---

## Ayarlar nereden gelir

| ayar | nerede | notu |
|---|---|---|
| makine tipi, spot, bölgeler, deneme sayısı, hata teşhisi | Ayarlar sekmesi | tarayıcıda saklanır |
| VM sayısı, slot | Kontrol sekmesi ve exec satırları | son değer saklanır |
| yorum sayısı, foto limitleri, mod | profil | `orchestrator/profiles.json` |

Bölgeler **sırayla** dağıtılır: 5 bölge seçip 10 VM istersen her bölgeye 2
düşer. Bölge seçilmeden run başlatılamaz — sağlayıcının varsayılan listesinde
ölçümde %0 teslim eden bölgeler var.
