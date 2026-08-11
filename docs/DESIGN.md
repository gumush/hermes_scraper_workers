# Tasarım sözleşmesi — Hermes Orchestrator arayüzü

Bu proje **Hermes Operation Center tasarım sistemini** kullanır. Sistemin kendisi bu depoda
değil, çalışma alanının ortak klasöründedir:

- [`../../design_system/README.md`](../../design_system/README.md) — ilkeler ve kapsam
- [`../../design_system/tokens.md`](../../design_system/tokens.md) — renk, tipografi, boşluk,
  radius, kontrol ölçüleri

Son otorite çalışan arayüzdür (`localhost:8818` · Operation Center). Belge ile görünen
çelişirse önce çalışan arayüz doğrulanır, sonra kod ve belge aynı commit'te güncellenir.

## Bu projede nerede uygulanır

Tek yüzey var: `../orchestrator/static/index.html`. Stiller dosyanın içindedir (tek dosyalık,
bağımlılıksız bir kontrol paneli olması kasıtlı — koordinatör tek bir `FileResponse` ile
servis eder). Tokenlar `:root` içinde birebir sistemdeki adlarla tanımlıdır.

Logo: `orchestrator/static/hermes_logo.svg`, Operation Center'dan alınmıştır.

## Uyulacak kurallar

**Token icat etme.** Renk, radius veya boyut gerektiğinde önce `tokens.md`'ye bak. Orada
karşılığı yoksa, en yakın mevcut değeri kullan. Yeni bir `--x` tanımlamak, sistemi bu
projede çatallamak demektir.

**Renk bütçesi dar.** Mavi eylem ve seçim, yeşil doğrulanmış başarı, amber müdahale
gerekebilecek durum, kırmızı gerçek hata veya yıkıcı eylem. "İşlem bitti" yeşil değildir;
"doğrulandı" yeşildir.

**Renk tek başına anlam taşımaz.** Her durum renginin yanında metin veya sayı bulunur.
Bölge tablosundaki başarı çubuğunun yanında yüzdenin yazması bu yüzdendir.

**Kontrol yüksekliği 34px.** Kompakt nav 32, ikon 30, badge 22. Genişlik içeriğe göre
değişir; bir buton satırın kalanını doldurmaz.

**Normal kartta gölge yok.** Gölge yalnız gerçekten üst katmanda duran geçici yüzeylerde
(lightbox, dialog, toast).

**Panel radius'u 9px'i aşmaz.**

**Uzun kimlikler monospace.** `place_id` gibi değerler `.pid` sınıfıyla, küçük punto ve
monospace gösterilir; kolon genişliğini zorlamaz.

## Bu arayüze özgü kararlar

Sistemde karşılığı olmayan, buradaki operasyonel ihtiyaçtan doğan üç kalıp:

**Metrik şeridi** (`.chips`) — 1px `--line` boşluklu grid, hücreler `--panel`. Sayaçlar
(done/failed/pending, hız, kalan süre) tek bakışta okunsun diye. Değer 18px, etiket 9px
uppercase.

**Yoğunluk grafiği** (`.pt-chart`) — Google'ın popular-times verisi. Çubuklar `--accent`,
canlı saat `--amber`. Süs değil: bir mekanın ne zaman dolu olduğunu gösterir.

**Bayrak rozeti** (`.flag`) — `--red-soft` zemin, uppercase. Eksik veya şüpheli paketleri
işaretler; üstüne gelince sebep, yanında `log` bağlantısı çıkar.

## Doğrulama

Değişiklikten sonra üç sekmeye de bak: Kontrol (canlı run varken ve yokken), Runlar (mekan
detayı açılmış halde), Bölgeler. `prefers-reduced-motion` açıkken geçişlerin kapandığını ve
klavye odağının `--accent` halkasıyla göründüğünü kontrol et.
