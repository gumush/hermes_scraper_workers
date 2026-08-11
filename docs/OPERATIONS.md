# İşletme kılavuzu

Günlük kullanım: run başlatmak, izlemek, durdurmak, eksikleri toplamak.

---

## Ön koşullar

```bash
gcloud auth login
gcloud config set project <proje-id>
gcloud compute instances list        # yetki kontrolü
```

Arayüzün sağ üstünde proje adı yeşilse hazırsın. Kırmızıysa `gcloud` erişimi
yoktur — orada yazan sebep, gcloud'un ham çıktısından değil, ayıklanmış
hâlinden gelir (200 GB disk uyarısı gibi gürültü temizlenir).

### Kotalar

Bölge başına sınırlar vardır ve genelde ilk çarpılan bunlardır:

| kota | tipik değer | ne zaman vurur |
|---|---|---|
| `CPUS` | bölgeye göre | e2-standard-2 = 2 vCPU, yani bölge başına ~4 VM |
| `INSTANCES` | 24 | çok sayıda VM |
| `IN_USE_ADDRESSES` | 8 | her VM bir dış IP alır — **bölge başına 8 VM tavanı** |

Bu yüzden 13 VM'i 13 ayrı bölgeye dağıtmak, 13'ünü tek bölgeye yığmaktan hem
daha güvenli hem de daha hızlıdır.

---

## Run tanımı

`orchestrator/runs/<ad>.hermes-google-places-run.json`:

```json
{
  "payload": {
    "run": { "name": "sehir-ilce-kategori-2026-08-11" },
    "place_ids": ["ChIJ…", "ChIJ…"]
  }
}
```

Çıktı klasörü **`run.name`** değerinden gelir, dosya adından değil. Aynı adı
taşıyan farklı dosyalar bilerek aynı ağaca yazar — bir şehri parça parça
tamamlarken istenen budur. Runlar listesinde ayırt etmek için her run'ın
yanında tanım dosyasının adı gösterilir.

---

## Run başlatmak

**Ayarlar** sekmesi (bir kez ayarlanır, tarayıcıda saklanır):

- **bölgeler** — geçmiş run'lardan ölçülmüş başarı oranlarıyla listelenir.
  Hiç bölge seçilmeden run başlatılamaz.
- **makine tipi** — ölçüm: tek slotta e2-standard-4 (172 sn/mekan) ile
  e2-standard-2 (189 sn/mekan) arasında kayda değer fark yok. Verim için
  makineyi değil VM/bölge sayısını artır.
- **spot** — yaklaşık üçte bir fiyat. Kesinti riski var; kaybolan VM'in işleri
  kuyruğa döner, yerine yenisi açılır.
- **deneme sayısı** — bir mekan failed sayılmadan önce kaç ayrı VM'de denensin.
- **hata teşhisi** — hata anında ekran görüntüsü + DOM kaydeder.

**Kontrol** sekmesi (her run için):

- run dosyası, profil, provider
- **VM sayısı** ve **slot**

Slot, VM başına eşzamanlı tarayıcı sayısıdır. Aynı IP'den iki tarayıcı
engellenme riskini artırır; 2 makul bir üst sınırdır.

---

## İzlemek

Durum satırı (Kontrol sekmesi) baştan sona aynı kutuları gösterir, sayılar
dolar:

```
durum · done · hız · saatlik · eta · scraping · transfer · pending · failed
· vm · çalışan · elenen · deneme · profil · provider
```

Hız, son 30 teslimin ortalamasıdır — tüm run'ın değil. Uzun bir run'da
anlamlı olan, o anki koşullardır.

VM kartlarında her makinenin durumu, **sebebi**, bölgesi, çıkış IP'si ve o an
neyi kazıdığı yazar. Silinen ve kaybolan VM'ler varsayılan olarak gizlidir
(sayıları hep görünür); üstteki anahtarlarla açılır.

---

## Duraklatmak

**Durdur** yeni iş dağıtımını keser; süren işler biter, VM'ler ayakta kalır.
Aynı düğme devam ettirir. Fatura işlemeye devam eder.

## Kapatmak

**Tüm VM'leri kapat**:

1. Yeni iş dağıtımı durur.
2. VM'lerde hazır bekleyen paketler paralel olarak çekilir (drain, 90 sn).
3. Her VM silinir.
4. **VM'leri doğrula** ile `gcloud`'a sorup gerçekten kalmadığını gör.

Drain sırasında kaç paketin kurtarıldığı arayüzde yazar.

> **Koordinatör süreci ölürse VM'ler sahipsiz kalır ve fatura işlemeye devam
> eder.** SSH tünelleri o süreçte yaşar; kimse onları kapatamaz. Böyle bir
> durumda elle temizle:
>
> ```bash
> gcloud compute instances list --filter="name~hermes" --format="value(name,zone)" |
>   while read n z; do gcloud compute instances delete "$n" --zone="$z" --quiet & done; wait
> gcloud compute instances list        # boş olmalı
> ```
>
> Süreç kapatırken desene göre toplu öldürme (`pkill -f …`) kullanma; porta
> göre kapat:
> ```bash
> kill $(lsof -nP -iTCP:8140 -sTCP:LISTEN -t)
> ```

---

## Eksikleri toplamak

Runlar sekmesinde her exec satırında **↻ eksikler** vardır. Yanındaki VM ve
slot kutularından havuzu seçersin; onay ekranı ne kullanacağını yazar.

"Eksik" = o exec'te **paket üretmemiş her mekan** — sadece `failed` değil,
run durdurulduğunda `pending` ve `scraping` kalanlar da dahil.

Kapsam exec bazındadır. Tüm run'ın gerçek eksiğini istiyorsan diskten türet:

```python
import json, pathlib
run  = json.loads(pathlib.Path("orchestrator/runs/<dosya>.json").read_text())
want = set(run["payload"]["place_ids"])
out  = pathlib.Path("outputs") / run["payload"]["run"]["name"]
have = {d.name for d in out.iterdir() if d.is_dir() and (d/"info.json").exists()}
print(sorted(want - have))
```

Klasörün varlığına değil `info.json`'un varlığına bak: yarım kalmış bir
klasör de klasördür.

---

## Hataya bakmak

Runlar → exec → mekan satırına tıkla. Başarısız bir mekan için gelenler:

- hata kodu ve mesajı
- **Maps'te aç** — scraper'ın gerçekten açtığı adres
  (`/maps/search/?api=1&query_place_id=…`), tarayıcıda karşılaştırabilirsin
- her denemenin logu (açılır)
- her denemenin ekran görüntüsü ve kaydedilen DOM'u (açılır)

Kontrol sekmesindeki başarısız listesinde de her `place_id`'nin yanında Maps
bağlantısı vardır.

---

## Run silmek

Runlar → **sil**. Onay ekranı önce gerçek rakamları verir (kaç mekan teslim,
kaç MB çıktı, kaç exec, kaç MB exec verisi), sonra iki seçenek sunar:

- **JSON kalsın** — exec'ler ve çıktılar gider, run tanımı listede kalır,
  sıfırdan tekrar başlatılabilir.
- **JSON dahil hepsi** — run tamamen kaybolur.

Çalışan run silinemez.

---

## Bölge seçimi

**Bölgeler** sekmesi geçmiş run'lardan biriken istatistikleri gösterir:
teslim, boş, detay yok, VM kaybı, sağlama hatası, ortalama kazıma süresi ve
başarı oranı.

Ölçülmüş bir sonuç: Orta Doğu bölgeleri (`me-west1`, `me-central1`) ve ABD
bölgeleri Türkiye mekanlarında pratikte teslim etmedi; Avrupa bölgeleri
%40–86 arasında. Bu yüzden varsayılan seçim 13 Avrupa bölgesidir.
