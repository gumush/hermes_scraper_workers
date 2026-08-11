# Hermes — dağıtık Google Maps yorum toplayıcı

Bir mekan listesi verirsin; sistem geçici GCP VM'leri açar, her mekanın
yorumlarını, işletme bilgilerini ve fotoğraflarını toplar, paketleri yerel
diske indirir ve VM'leri kapatır. Süreç bir web arayüzünden izlenir.

İki taraf vardır ve **birbirinin koduna bağımlı değildirler**:

| | nerede çalışır | ne yapar |
|---|---|---|
| **orchestrator** | senin makinen | VM açar, işi dağıtır, paketleri toplar, VM'leri kapatır, arayüzü sunar |
| **workers** | geçici VM'ler | tek bir mekanı kazır, paketler, orchestrator'ın çekmesini bekler |

Orchestrator kazıma kodunu hiç import etmez; worker da orchestrator'ı bilmez.
Aralarındaki tek bağ, SSH tüneli üzerinden konuşulan küçük bir HTTP API'sidir.

---

## Hızlı başlangıç

```bash
./start.sh                     # orchestrator + arayüz → http://localhost:8140
```

İlk çalıştırmada sanal ortamı kurar ve bağımlılıkları yükler. Arayüz açılınca:

1. **Ayarlar** sekmesinden bölgeleri seç (varsayılan 13 Avrupa bölgesi),
   makine tipini ve deneme sayısını ayarla.
2. **Kontrol** sekmesinden run dosyasını, profili, VM ve slot sayısını seç.
3. **Start**.

GCP tarafı için `gcloud` kurulu ve giriş yapılmış olmalı:

```bash
gcloud auth login
gcloud config set project <proje-id>
```

Arayüz sağ üstte projeyi gösterir; kırmızıysa `gcloud` erişimi yok demektir.

### Yerel deneme (VM açmadan)

Kontrol sekmesinde provider'ı `local (test)` seç. Her "VM" ayrı bir portta
yerel bir worker sürecidir. GCP gerekmez.

```bash
./start.sh worker              # tek bir worker'ı elle kaldırmak istersen
```

`SPOT_API_KEY` zorunludur; orchestrator kendi worker'larına rastgele bir
anahtar üretir.

### Test

```bash
./start.sh test                # 259 test
./start.sh test -k hotel       # sadece bir kısmı
```

---

## Klasör düzeni

```
orchestrator/          yerel taraf
  coordinator.py         iş dağıtımı, VM ömrü, paket toplama, HTTP API
  providers.py           GCP ve yerel provider — VM açma/kapama
  static/index.html      tek dosyalık web arayüzü
  runs/                  run tanımları (girdi; versiyonlanmaz)
  state/                 exec durumu, loglar, hata paketleri (versiyonlanmaz)
  profiles.json          kazıma profilleri

workers/               VM tarafı
  server.py              iş kuyruğu + paketleme + indirme API'si
  engine/                kazıma motoru (tarayıcı sürüşü, ayrıştırma, veritabanı)

outputs/               teslim edilen veri (versiyonlanmaz)
  <run adı>/<place_id>/  info.json · reviews.json · images.json · images/

docs/                  ayrıntılı dokümantasyon
tests/                 testler
```

`outputs/`, `orchestrator/runs/`, `orchestrator/state/` ve `spot_out/`
`.gitignore` kapsamındadır — veri deposu değil, çalışma alanıdır.

---

## Teslim edilen veri

Her mekan için bir klasör:

```
outputs/<run adı>/<place_id>/
├── info.json        işletme bilgileri: ad, adres, telefon, site, kategori,
│                    çalışma saatleri, fiyat aralığı, yoğunluk saatleri, puan
├── reviews.json     {place_id, count, scraped_at, reviews:[…]}
├── images.json      {owner:{available,count,items}, review:{count,items}}
└── images/
    ├── ownerimages/     işletmenin kendi yüklediği fotoğraflar
    └── reviewimages/    yorumlara eklenmiş fotoğraflar
```

Aynı mekan yeniden kazınırsa klasörü **silinip baştan yazılır**, üstüne
eklenmez. Aynı `run.name` değerini taşıyan farklı run tanımları bilerek aynı
çıktı ağacında toplanır — bir şehri parça parça tamamlarken işe yarar.

---

## Dokümantasyon

| dosya | ne anlatır |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | iki tarafın iç işleyişi, iş yaşam döngüsü, paket biçimi, API |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | run başlatma, izleme, duraklatma, kapatma, eksikleri toplama |
| [docs/EDGE_CASES.md](docs/EDGE_CASES.md) | normal akışı bozan sayfa tipleri ve nasıl çözüldükleri |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | sık karşılaşılan hatalar ve teşhis yolları |
| [docs/DESIGN.md](docs/DESIGN.md) | arayüz tasarım sözleşmesi |
| [docs/design_system/](docs/design_system/) | tasarım sisteminin kendisi: ilkeler ve tokenlar |

---

## Lisans

Bkz. [LICENSE](LICENSE). Bu ağaç, `google-reviews-scraper-pro` projesinden
türetilmiş; kazıma motoru oradan gelir, dağıtık koşum katmanı bu depoya özgüdür.
