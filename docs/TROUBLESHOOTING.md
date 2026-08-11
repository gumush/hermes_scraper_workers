# Sorun giderme

Her madde: **belirti → nasıl teşhis edilir → sebep → çözüm**. Teşhis kısmı
tahmin ettirmez, baktıracak bir yer gösterir.

---

## VM'ler açılmıyor

**Belirti:** VM kartları `failed`, sebep alanında gcloud mesajı.

**Teşhis:**
```bash
gcloud compute instances list
gcloud compute project-info describe --format="value(quotas)" | tr ';' '\n' | grep -i -A2 "CPUS\|INSTANCES\|ADDRESSES"
```

**Sık sebepler:**

| sebep | belirti | çözüm |
|---|---|---|
| kota doldu | `QUOTA_EXCEEDED` | daha az VM, ya da bölgeleri yay |
| spot kapasitesi yok | `ZONE_RESOURCE_POOL_EXHAUSTED` | başka bölge seç, ya da spot'u kapat |
| disk uyarısı | `disk size under 200GB` | **hata değil**, performans uyarısı; arayüz bunu ayıklar |

Sağlayıcı hataları ham çıktı olarak değil, ayıklanmış Türkçe cümle olarak
gösterilir (`_gcloud_reason`) — 200 GB disk uyarısı gerçek hatanın önüne
geçiyordu.

---

## Worker hiç başlamıyor

**Belirti:** VM `provisioning`da takılı, sonra `lost`.

**Teşhis:** VM'e bağlanıp servise ve loga bak:
```bash
gcloud compute ssh <vm> --zone=<zone> --command="systemctl status hermes-worker; tail -50 ~/scraper/worker.log"
```

**Bilinen sebep:** `systemd-run` bir kabuk değil, komut çalıştırır. Başlatma
satırında `cd X && python …` yazarsa hiç başlamaz — `--working-directory`
kullanılmalı ve doğrudan python komutu verilmelidir.

Ayrıca `nohup … &` **kullanılmaz**: SSH kanalını açık tutar, bağlantı 45+
saniye asılı kalır (ölçüldü; systemd-run 1.9 sn).

---

## Koordinatör öldü, VM'ler ayakta

**Belirti:** arayüz açılmıyor, `gcloud compute instances list` hâlâ VM
gösteriyor. Fatura işliyor.

**Sebep:** SSH tünelleri koordinatör sürecinde yaşar. Süreç ölünce VM'lere
ulaşan kimse kalmaz; kendi kendilerine kapanmazlar.

**Çözüm:**
```bash
gcloud compute instances list --filter="name~hermes" --format="value(name,zone)" |
  while read n z; do gcloud compute instances delete "$n" --zone="$z" --quiet & done; wait
gcloud compute instances list          # boş olmalı
```

O anda transfer edilmekte olan paketler kaybolur; teslim edilmiş olanlar
diskte güvendedir. Eksikleri toplamak için OPERATIONS.md'deki diskten türetme
yöntemini kullan.

**Önleme:** süreçleri desene göre toplu öldürme. Bu tam olarak böyle bir
kazaya yol açtı — `pkill -f "orchestrator.coordinator"` test sürecini
kapatırken çalışan run'ı da öldürdü. Porta göre kapat:
```bash
kill $(lsof -nP -iTCP:8140 -sTCP:LISTEN -t)
```

---

## Mekan teslim edilmiyor: `reviews_undetermined`

**Belirti:** iş 3 denemede de aynı kodla düşüyor.

**Teşhis:** Runlar → exec → mekan satırı. Ekran görüntüsüne ve kaydedilen
DOM'a bak; sekme şeridinde `Reviews` var mı? Sonra aynı mekanı **kendi
tarayıcında** aç (satırdaki Maps bağlantısı scraper'ın açtığı adresi verir) ve
karşılaştır.

**İki farklı durum vardır ve ayırt etmek gerekir:**

1. **Panel geç kuruldu.** Maps paneli kademeli gelir; Reviews sekmesi en sona
   kalır. Kod bunu bekler (`TAB_STRIP_TIMEOUT`, 12 sn) ve genelde sonraki
   denemede düzelir — ölçüm: hata paketi üreten 15 mekanın 7'si sonraki
   denemede tam teslim edildi.
2. **Sayfa datacenter IP'sine kırpık geliyor.** Ölçülmüş bir örnek: aynı
   mekanın sayfası ev IP'sinde `Overview · Menu · Reviews · About` ve yorum
   kartlarıyla gelirken, VM'de 10 saniye boyunca sabit biçimde yalnız
   `Overview · About` geldi. Bu durumda deneme sayısını artırmak kurtarmaz.

Ayırt etmenin yolu: kalıcı düşenlerde hata **denemeden denemeye değişiyor
mu**. Değişmiyorsa ikinci durumdasın.

> **Not:** hata paketindeki `detail.place_id` alanı sayfa ne olursa olsun
> `0x…:0` biçiminde biter — bu scraper'ın iç kimlik alanıdır, bir sinyal
> değildir. Çözülmüş kimlik için `meta.json`'daki `url` alanındaki
> `!1s0x…:0x…` parçasına bak. İki farklı alanı karşılaştırmak yanlış bir
> "bulguya" yol açtı; aynı alanı iki tarafta okumak şart.

---

## Otel sayfalarında puanlar 0

**Belirti:** teslim edilmiş bir otelin tüm yorumlarında `rating: 0.0`.

**Sebep:** otel kartları puanı `aria-label` ile etiketlemez, düz metin yazar
(`<span class="fontBodyLarge fzvQIb">5/5</span>`).

**Çözüm:** `RawReview._rating_from_text` yaprak span'lerde tam `N/5`
eşleşmesi arar. Ayrıntı ve ölçüm için EDGE_CASES.md.

---

## Paket geldi ama yorum sayısı beklenenden az

**Önce alanı doğrula.** `reviews.json` bir **sözlüktür**:

```json
{"place_id": "...", "count": 250, "scraped_at": "...", "reviews": [...]}
```

`len(dosya)` = 4 verir (anahtar sayısı). Doğrusu `len(d["reviews"])`.
Aynı şekilde `images.json` içindeki `owner` ve `review` de sözlüktür,
sayı `["count"]` alanındadır.

Gerçekten az geldiyse bakılacaklar:

- profil `max_reviews` ve `max_reviews_cap` değerleri
- otel sayfalarında listede başka kaynaklar da olabilir; kaynak `raw_date`
  alanında saklanır (`"7 years ago on Tripadvisor"`)
- Maps'in başlıkta yazdığı sayı her zaman indirilebilir yorum sayısı değildir

---

## Aynı adı taşıyan iki run

**Belirti:** Runlar listesinde aynı isimden iki tane.

**Sebep değil, tasarım:** çıktı klasörü `run.name` değerinden gelir. Bir şehri
parça parça tamamlarken aynı adı vermek, verinin tek ağaçta toplanmasını
sağlar. Ayırt etmek için her run'ın yanında tanım dosyasının adı yazar.

Verinin karışmadığını doğrulamak istersen: çıktıdaki klasörlerin hepsi ana
run'ın `place_ids` listesinde olmalı ve yabancı klasör bulunmamalı.

---

## `jobs.json.tmp` çakışması

**Belirti (giderildi):** `[Errno 2] No such file or directory: 'spot_out/jobs.json.tmp'`

**Sebep:** birden çok iş parçacığı aynı geçici dosya adını kullanıyordu.
Artık ad süreç ve iş parçacığı kimliğini taşır. 12×40 eşzamanlı testte temiz.

---

## Testler yeni ağaçta çalışmıyor

`modules.` yerine `workers.engine.` kullanılır. `unittest.mock.patch`
hedeflerini de güncellemek gerekir:

```python
@patch("workers.engine.pipeline.S3Handler")     # "modules.pipeline.S3Handler" değil
```
