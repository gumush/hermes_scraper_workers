# Hermes Operation Center Design System

Durum: çalışan arayüzden çıkarılmış **as-built** görsel sözleşme  
Son kaynak kontrolü: 2026-08-10

Bu klasör yalnız Hermes Operation Center ve Google API çalışma alanının görsel tasarım sistemini
tanımlar. Ürün akışları, scraper davranışı, worker mimarisi, API sözleşmeleri ve veritabanı yapısı bu
klasörün kapsamı dışındadır.

## Kaynak otoritesi

Çalışan arayüz için son otorite aşağıdaki CSS dosyalarıdır:

- [Operation Center stilleri](../public/styles.css)
- [Google API ortak stilleri](../../google_api/ui/src/styles.css)
- [Google API map-first çalışma alanı](../../google_api/ui/src/google-api.css)

Bu belgeler söz konusu kaynakların yeniden kullanılabilir tasarım kararlarını açıklar. CSS ile belge
çelişirse önce çalışan görünüm doğrulanır; ardından kod ve bu klasör aynı commit içinde güncellenir.

## İçindekiler

1. [Tokenlar](tokens.md): renk, tipografi, boşluk, radius, boyut ve katmanlar.
2. [Bileşenler](components.md): ortak UI parçaları, varyantlar ve durum sözleşmeleri.
3. [Yerleşim ve erişilebilirlik](layout-accessibility.md): ekran kabukları, breakpoint'ler,
   klavye davranışı ve kalite kontrol listesi.

## Tasarım ilkeleri

- **Operasyon yoğunluğu:** Ekranlar pazarlama yüzeyi değil, hızlı karar verilen kontrol panelleridir.
- **Kanıt görünürlüğü:** Durum, sayı, progress ve hata nedeni süsleyici öğelerden önce gelir.
- **Sınırlı renk bütçesi:** Mavi eylem/seçim, yeşil başarı, amber uyarı, kırmızı hata veya yıkıcı
  eylem içindir.
- **Metinle desteklenen durum:** Renk tek başına anlam taşımaz; badge metni veya açıklama bulunur.
- **Kompakt ama okunabilir:** Kontroller çoğunlukla 34 px, içerik çoğunlukla 10–12 px'dir.
- **Doğal içerik yüksekliği:** Ayrıntı panelleri, kullanılabilir boş alan varken yapay küçük bir kutuya
  sıkıştırılmaz.
- **Medya bütünlüğü:** Fotoğraflar önizlemede `object-fit: contain` ile kırpılmadan gösterilir.
- **Az hareket:** Animasyon bilgi taşımaz; reduced-motion tercihi korunur.
- **Açık eylem dili:** Özellikle retry, arşiv ve silme gibi işlemler sonucu belirsiz genel etiketler
  kullanmaz.

## Kapsam sınırı

Bu klasöre eklenebilecek içerikler:

- görsel tokenlar;
- ortak bileşen ve durum kuralları;
- responsive yerleşim davranışı;
- erişilebilirlik ve görsel QA kontrol listeleri.

Bu klasöre eklenmemesi gereken içerikler:

- ürün veya veri akışı;
- endpoint ve API ayrıntıları;
- scraper, worker veya cloud mimarisi;
- veritabanı tabloları;
- operasyon prosedürleri.

