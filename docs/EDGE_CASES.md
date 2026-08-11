# Edge case defteri

Normal akışı bozan, tek seferlik olmayan sayfa tipleri. Her kayıt şu sırayla:
**belirti → kanıt → kök neden → ne yapıldı → nasıl doğrulandı**. Kanıt kısmı
tahmin değil; ya kaydedilen DOM'dan ya canlı sayfadan ölçülmüş sayı içerir.

Yeni bir vaka eklerken kural: *yokluğa bakıp karar verme.* "Sekme yok, demek ki
yorumu yok" tipi çıkarım bu dosyadaki hataların çoğunun kaynağı.

---

## 1. Otel sayfaları — Odile Konak

**place_id** `ChIJ-3SMeg-QwxQR4VWYNSsgqhg` ·
[Maps'te aç](https://www.google.com/maps/search/?api=1&query=ChIJ-3SMeg-QwxQR4VWYNSsgqhg&query_place_id=ChIJ-3SMeg-QwxQR4VWYNSsgqhg&gl=tr&hl=en) ·
ftid `0x14c3900f7a8c74fb:0x18aa202b359855e1`

### Belirti

Antalya run'ında (`exec-20260811-085051`) üç denemenin üçü de düştü, kalıcı
`failed` oldu. Kodlar: `reviews_undetermined`, `reviews_undetermined`,
`wrong_place_on_details`.

### Kanıt

Sayfayı elle açtım — işletme sapasağlam: **4.4 ★, 97 yorum**, Reviews sekmesi
yerinde, tıklayınca kartlar geliyor.

Sekme şeridi normal işletmeden farklı:

| Normal işletme | Odile (otel) |
|---|---|
| Overview · Reviews · About | Overview · **Prices** · Reviews · About · Things to do · Transit · Airports |
| — | "Check availability", tarih seçiciler, "Compare prices", "Similar hotels nearby" karuseli |

Birinci denemede kaydedilen DOM'da sekmeler: `Overview, Prices, About` —
**Reviews henüz yok**, `data-review-id` sayısı 0, ama `F7nice` (puan bloğu)
gelmiş. Yani panel yarı kurulmuşken okunmuş.

Yorum kartlarının içi de farklı (canlı sayfada 20 kart üzerinde ölçüldü):

| Alan | Normal | Otel |
|---|---|---|
| puan | `span[role="img"][aria-label="5 stars"]` | `<span class="fontBodyLarge fzvQIb">5/5</span>` — aria-label yok, role yok |
| tarih | `3 years ago` | `3 years ago on Google` / `7 years ago on Tripadvisor` |

Ölçüm: mevcut iki puan selektörü **20 kartın 20'sinde de boş döndü** → her otel
yorumu `rating = 0.0` olarak kaydedilirdi. Tarih ve metin selektörleri 20/20
çalışıyor.

Kaynak dağılımı (görünen 10 tarihli kart): **Google 3, Tripadvisor 7**. Otel
sayfasındaki yorum listesi yalnızca Google'ın değil.

### Kök neden

Üç ayrı sebep üst üste bindi:

1. **Panel geç kuruluyor.** Otel düzeni rezervasyon modülü, fiyat karşılaştırma
   ve benzer oteller karuselini önce yüklüyor; Reviews sekmesi en sona kalıyor.
   `_review_availability` "tab şeridi var mı" diye bekliyordu — şerit
   Overview/Prices ile hemen doluyor, cevabı taşımıyor. Puan var + Reviews
   sekmesi yok → çelişki → `unknown` → iş düşüyor.
2. **Puan metin olarak yazılmış.** aria-label arayan selektörler bulamıyor.
3. **Gezinme başka mekana düşmüş.** Üçüncü denemede ilk gezinme
   `0x14c39100313dcbfb…` (Türkisches Ice Cream) sayfasına inmiş, kimlik
   referansı oradan alınmış; detay dönüşünde Odile'e gelinince
   `wrong_place_on_details`. Kimlik kontrolü doğru çalıştı, ama karşılaştırdığı
   referans zaten yanlış sayfadan gelmişti.

### Ne yapıldı

- `_review_availability` artık **kesin cevaba kadar** yokluyor (`1c08637`).
  `has` pozitif kanıt, anında kabul. `none` bir yokluk — hâlâ yüklenen panelle
  birebir aynı görünür — bu yüzden `REVIEWLESS_CONFIRM` (3 sn) boyunca sabit
  kalmadan sayılmıyor. 12 sn'de oturmayan sayfa `unknown`.
- `RawReview._rating_from_text` eklendi (`6d20399`): yalnız yaprak span'lerde
  tam `N/5` eşleşmesi. Gevşek eşleşme aynı karttaki "41 reviews" ve "6 photos"
  değerlerini puan sanardı.
- `RawReview.source` eklendi; tarih satırındaki `… ago on X` okunuyor. Şema
  değişmedi — `raw_date` kolonu zaten tüm metni kaynağıyla birlikte saklıyor.

### Nasıl doğrulandı

- `tests/test_hotel_reviews.py` — 20 test. Puanın metinden okunması, ondalık ve
  virgüllü biçim, "41 reviews"/"6 photos"/"1/10"/"6/5" gibi tuzakların puan
  sanılmaması, kaynak ayrıştırma, ve kaynak ekiyle birlikte göreli tarihin hâlâ
  parse edilmesi.
- Selektörler canlı Odile sayfasında ölçüldü (yukarıdaki 0/20 ve 20/20 sayıları).
- Tüm takım: 355 test geçiyor.

### Açık kalan

**Tripadvisor yorumları listede duruyor.** Şu an hepsi indiriliyor, kaynağı
kaydediliyor ama filtrelenmiyor. Ürün "Google yorumları" ise bunların
ayıklanması ya da ayrı tutulması gerekir — karar verilmedi. Maps'in başlıkta
yazdığı 97 sayısının hangi kaynakları kapsadığı da doğrulanmadı; doğrulanmadan
"97 bekliyorduk, 97 geldi" denmemeli.

---

## 2. Yorumsuz işletmeler

**Belirti:** Reviews sekmesi hiç yok, yorum kartı yok.

**Kanıt:** Kaydedilen ekran görüntüleri, gerçekten yorumu olmayan işletmelerin
sayfasının tam da böyle geldiğini gösterdi — fotoğraf, adres, saatler, site,
telefon, plus code hepsi yerinde, sekme şeridi hiç yok. Bu bozuk sayfa değil,
normal bir durum.

**Kök neden:** Yokluğu kanıt saymak. "Reviews sekmesi yok" hem yorumsuz
işletmede hem yarı yüklenmiş sayfada aynı görünüyor.

**Ne yapıldı:** İki durum yerine üç: `has` / `none` / `unknown`. `none` yalnız
**pozitif kanıtla** dönüyor — panel gerçekten render olmuş (adres tek başına
yeterli, yoksa 2+ alan) **ve** işletmenin kendi puanı yok. Puan `div.F7nice`
içinde ve içinde rakam olmak zorunda; sayfanın tamamında yıldız aramak
komşu işletmelerin puanlarını buluyordu (bir sayfada 4 tane).

**Nasıl doğrulandı:** `tests/test_scraper_tab_detection.py`; Kuman Pide Döner
gibi yalnız adresi olan mekanlarla saha kontrolü.

---

## 3. Aynı place_id farklı işletmeye çözülüyor

**Belirti:** İzmir place_id'si ile açılan sayfada Brüksel'de bir işletme.

**Kök neden:** Datacenter IP'sinden `place_id` URL'i bazen bambaşka, yerel bir
mekana çözülüyor. Ayrıca `_extract_place_name` sorgu dizesini isim sanıyordu.

**Ne yapıldı:** Maps URLs API ile gezinme
(`/maps/search/?api=1&query=<pid>&query_place_id=<pid>`), sonra canonical
URL'den hex **ftid** okunup her yeniden yüklemede kimlik doğrulanıyor.
Sayfa kaynağında ChIJ id'si aramak işe yaramıyor — elle açılan tarayıcıda var,
Selenium'da yok.

**Açık kalan:** Kimlik referansının kendisi yanlış sayfadan alınırsa kontrol
yanlış şeyi doğrular (bkz. vaka 1, üçüncü deneme). Referans alınmadan önce de
bir doğrulama gerekiyor.

---

## 4. Otel içi mekanlar — "Resort" içindeki "sort"

**Örnekler** Buddha-Bar Beach (Mykonos) `ChIJtaWldY2-ohQRq9ErCoLNLIY` ·
Delos Lounges & Bar `ChIJeWVaMBW_ohQRiC_xLa5-SbY` · Odile Konak

### Belirti

İki mekan günlerce alınamadı. 10 Avrupa bölgesinde 10 denemenin 10'unda,
sonra normal (spot olmayan) makinelerde, sonra yerelde — hep aynı.

### Kanıt

Burada **iki ayrı sorun** üst üste binmişti ve tek tek ayrıştırılana kadar
ikisi de yanlış teşhis edildi. Ayrıştırma deney tablosuyla yapıldı:

| deney | headless | ısınma turu | sort düzeltmesi | sonuç |
|---|---|---|---|---|
| A | hayır | ✗ | ✗ | `reviews_withheld` |
| B | evet | ✓ | ✗ | yanlış sayfa (ana otel), 0 kart |
| C | evet | ✓ | ✓ | **5 yorum, red flag yok** |
| D | evet | ✗ | ✓ | `reviews_withheld` |
| E | evet | ✓ | ✓ | **öteki mekan da temiz** |

D ile C'nin farkı yalnızca ısınma turu; B ile C'nin farkı yalnızca sort
düzeltmesi. İkisi de gerekli.

### Kök neden 1 — soğuk oturuma yorum gönderilmiyor

Geçmişsiz, çerezsiz bir oturum doğrudan mekan sayfasına gittiğinde Maps
puanı veriyor ama **yorum sayısını ve Reviews sekmesini göndermiyor**.
Yakalanan 20 DOM'un 20'sinde `F7nice` içeriği `4.6` — parantezli sayı yok;
`data-review-id` 0, `>Reviews<` 0. Aynı sayfa sıradan bir tarayıcıda
`4.6(16)` ve yorumlarıyla geliyor.

Bu **IP meselesi değil**: aynı makinede, aynı adresten, tarayıcıda yorumlar
gelirken scraper alamıyordu. Önce datacenter IP'sine, sonra headless'a
bağlandı — ikisi de yanlıştı, deney tablosu ayırdı.

### Kök neden 2 — `sort` kelimesi `Resort` içinde saklanıyor

Sıralama düğmesi `button[aria-label*="Sort" i]` ile aranıyordu. Otel içi bir
mekanda Maps kontrollere işletmenin adını yazıyor:

```
aria-label="Located in: Santa Marina, a Luxury Collection Resort, Mykonos"
```

`Re·sort` eşleşiyor, düğme sıralama kontrolü sanılıyor, tıklanınca **ana
otelin sayfasına gidiliyor**. Yorum aşaması o otelin panelini okuyup "kart
yok" diyor. Yakalanan sayfada 131 düğme, 68 aria-label, **5 aday** vardı —
beşi de otelin kendi kontrolleri.

Aynı tuzağın ters yönü de var: `Backyard by Olde` gerçek bir mekan ve
sıralama düğmesinin etiketi `"Sort reviews for Backyard by Olde"` oluyor;
negatif listedeki `back` yüzünden **çalışan düğme atılıyordu**.

### Ne yapıldı

- `_has_sort_word` / `_has_any_word`: kelime sınırıyla eşleşme. Latin
  kelimeler `(?<![a-zçğıöşü])…(?![a-zçğıöşü])` ile; CJK ve Tayca'da sınır
  kavramı olmadığı için onlar substring kaldı.
- Adaylar seçilmeden **toplanıyor**. Sıralama kontrolü sayfada tek bir şey;
  birden fazla aday kuralın başka bir şeyi yakaladığı anlamına geliyor. En
  iyi puanlı seçiliyor (sınıf +2, dropdown +1) ve `sort_button_ambiguous`
  bayrağı adayların etiketleriyle üretiliyor.
- `extended_warmup` profil alanı: Maps'e gitmeden önce iki rastgele kelimeyle
  Google araması, sonuçlardan birine giriş. Mekan başına ~10 sn eklediği için
  varsayılan değil; `İnatçılar - Warmup` profili bunun için hazır.

### Nasıl doğrulandı

- `tests/test_sort_button.py` — 30 test: gerçek etiketler eşleşiyor, otelin
  beş kontrolü eşleşmiyor, negatif kelimeler kendi kontrollerini hâlâ eliyor
  ama mekan adlarını elemiyor.
- **1.225 gerçek işletme adı** ve yakalanan DOM'lardan çıkarılan **678
  aria-label** üzerinde ölçüm: eski kural 10 yanlış eşleşme, yeni kural 0.
- Sahada: iki mekan da C ve E denemelerinde yorumlarıyla geldi.

### Açık kalan

Isınma turunun neden işe yaradığı ölçüldü ama **nedeni bilinmiyor** —
çerez, arama geçmişi, referrer ya da zamanlama olabilir. Hangi bileşenin
belirleyici olduğu ayrıştırılmadı; şimdilik ampirik bir çözüm.
