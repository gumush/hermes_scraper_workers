# Tasarım tokenları

Bu dosya mevcut CSS değişkenlerini ve uygulamada gözlenen ortak ölçüleri belgeler. “Gözlenen ölçek”
olarak belirtilen değerler henüz CSS custom property değildir; yeni bir token icat edilmiş gibi
yorumlanmamalıdır.

## Renkler

| Token | Değer | Kullanım |
|---|---:|---|
| `--bg` | `#f5f6f8` | Uygulama zemini |
| `--panel` | `#ffffff` | Kart, tablo, form ve dialog yüzeyi |
| `--panel-muted` | `#f8f9fb` | İkincil yüzey, fact ve boş içerik zemini |
| `--line` | `#e3e6ea` | Normal sınır ve ayırıcı |
| `--line-strong` | `#d1d6dc` | Input, güçlü ayırıcı ve kontrol sınırı |
| `--text` | `#192027` | Ana metin |
| `--muted` | `#68727d` | Yardımcı metin ve ikincil değer |
| `--subtle` | `#89939e` | En düşük öncelikli açıklama |
| `--accent` | `#2367e8` | Birincil eylem, seçili durum, progress ve odak |
| `--accent-hover` | `#1857cb` | Birincil eylem hover |
| `--accent-soft` | `#eaf1ff` | Mavi badge veya seçili yüzey |
| `--green` | `#168565` | Başarı, bağlı ve doğrulanmış durum |
| `--green-soft` | `#e7f6f1` | Başarı yüzeyi |
| `--amber` | `#b56b08` | Uyarı, bekleme ve partial durum |
| `--amber-soft` | `#fff3dc` | Uyarı yüzeyi |
| `--red` | `#c33b43` | Hata, human-check ve yıkıcı eylem |
| `--red-soft` | `#fff0f1` | Hata yüzeyi |

### Renk kullanım sözleşmesi

- Mavi yalnız ana eylem, seçili durum ve aktif ilerleme için kullanılır.
- Yeşil yalnız doğrulanmış olumlu sonuç içindir; sırf işlem sona erdi diye kullanılmaz.
- Amber müdahale gerektirebilecek fakat veri kaybı anlamına gelmeyen durumları taşır.
- Kırmızı gerçek hata, erişim engeli veya geri dönüşü zor eylem içindir.
- Durum rengi yanında her zaman metin veya sayı bulunur.
- Yeni renk eklemeden önce mevcut semantik renklerden birinin yeterli olup olmadığı kontrol edilir.

## Tipografi

### Yazı aileleri

| Rol | Değer |
|---|---|
| UI | `Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` |
| Kod/kimlik | `ui-monospace, SFMono-Regular, Menlo, monospace` |
| Google harita içeriği | Gerektiğinde `Roboto, Arial, sans-serif` |

### Gözlenen boyut ölçeği

| Boyut | Tipik kullanım |
|---:|---|
| `8px` | Mikro telemetri ve en düşük öncelikli teknik değer |
| `9px` | Eyebrow, tablo başlığı, badge, fact etiketi |
| `10px` | Form label, buton, yardımcı açıklama |
| `11px` | Ana kompakt UI metni, input ve tablo satırı |
| `12px` | Paragraf, güçlü özet ve boş durum metni |
| `13px` | Kart başlığı |
| `14px` | Panel başlığı |
| `15–16px` | Marka ve dialog başlığı |
| `18px` | Ana metrik değeri |
| `24px` | Sayfa başlığı |

### Ağırlık ve harf aralığı

- Gövde: `500` veya normal.
- Label ve buton: `700`.
- Badge, eyebrow ve güçlü başlık: `750`.
- Sayfa başlığında yaklaşık `-0.035em`, markada `-0.02em` sıkı harf aralığı kullanılır.
- Uppercase mikro etiketlerde yaklaşık `.04em–.09em` harf aralığı kullanılır.
- Uzun kimlikler monospace, wrap veya ellipsis ile gösterilir; kolon genişliğini zorlamaz.

## Boşluk ölçeği

Uygulamada gözlenen boşluk değerleri:

`4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 18, 22px`

Kullanım ilkesi:

- `4–6px`: ikon/metin, badge içi ve sıkı satır içi boşluk;
- `7–10px`: kontrol, fact, tablo hücresi ve kompakt kart içi boşluk;
- `11–14px`: form grubu ve bölüm içi boşluk;
- `16–18px`: panel padding'i ve ekran kenarı;
- `22px`: yalnız güçlü bölüm ayrımı.

Yeni bir değer eklemek yerine en yakın mevcut ölçü tercih edilir.

## Radius

| Değer | Kullanım |
|---:|---|
| `5px` | Küçük içerik içi vurgu |
| `6px` | Thumbnail, kompakt fact ve küçük kontrol |
| `7px` | Input, buton, ikincil panel ve kart içi grup |
| `8px` | Toast ve medya yüzeyi |
| `9px` | Ana panel ve worker kartı |
| `10px` | Dialog |
| `999px` | Badge, progress ve pill |

Ana panellerin radius'u `9px`'i aşmaz; dekoratif büyük yuvarlak kartlar kullanılmaz.

## Kontrol ve yüzey ölçüleri

| Ölçü | Değer |
|---|---:|
| Üst çubuk | `58px` |
| Standart input/buton | `34px` |
| Kompakt nav butonu | `32px` |
| İkon butonu | `30px` |
| Worker enable/disable kontrolü | `26px` |
| Durum badge'i | `22px` |
| Task aç/kapat kontrolü | `24px` |
| Progress çizgisi | `7px` |
| Fotoğraf grid hücresi | `128–160px` |

Kontrol yüksekliği korunur; genişlik içerik veya yerleşim sözleşmesine göre belirlenir. Bir toolbar
butonu, kalan bütün satırı dolduracak şekilde kontrolsüz büyütülmez.

## Sınır, odak ve gölge

- Normal yüzey: `1px solid var(--line)`.
- Input ve güçlü kontrol: `1px solid var(--line-strong)`.
- Klavye odağı: `2px solid var(--accent)` ve `2px` offset.
- Form focus halkası, mevcut Google API yüzeyinde açık mavi sınır ve düşük opaklıklı mavi gölgeyle
  güçlendirilebilir.
- Normal kartlarda gölge kullanılmaz.
- Gölge yalnız dialog, toast veya gerçekten üst katmanda duran geçici yüzeylerde kullanılır.

## Katman ve yerleşim tokenları

| Token/değer | Kullanım |
|---|---|
| `z-index: 20` | Sticky üst çubuk |
| `z-index: 50` | Toast |
| `--sidebar: 296px` | Google API sol çalışma paneli |
| `--drawer: 384px` | Google API sağ sonuç/ayrıntı alanı |

Overlay ve lightbox, kendi dialog katmanında üst çubuk ve normal içerikten yukarıda kalmalıdır.

