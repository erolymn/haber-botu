# Telegram Haber Botu

RSS kaynaklarından yeni haberleri alıp Telegram kanalına otomatik gönderir.
Sunucu yok: GitHub Actions her 10 dakikada bir çalıştırır, gönderilen haberlerin listesi
`state/posted.json` dosyasında tutulur.

## Kurulum (adım adım)

### 1. Telegram botu oluştur
1. Telegram'da **@BotFather**'ı aç, `/newbot` yaz.
2. Bota bir isim ver (örnek: `Teknoloji Haber Botu`), sonra kullanıcı adı ver (örnek: `teknohaber_bot`, sonu `bot` ile bitmeli).
3. BotFather sana `123456789:AAH...` gibi bir **token** verir. Bunu kopyala, kimseyle paylaşma.

### 2. Kanalı aç ve botu yönetici yap
1. Telegram'da **Yeni Kanal** oluştur, herkese açık yap ve bir kullanıcı adı seç (örnek: `@teknohaberler`).
2. Kanal ayarları → **Yöneticiler** → **Yönetici Ekle** → botunu ara ve ekle.
3. Botun **Mesaj gönderme** yetkisi açık olsun.

### 3. GitHub'a gizli değerleri ekle
1. GitHub'da bu depoya gir: **Settings → Secrets and variables → Actions → New repository secret**.
2. Bir secret ekle: ad `TELEGRAM_BOT_TOKEN`, değer BotFather'ın verdiği token.
3. Kanalın kullanıcı adını `feeds.json` içindeki `kanal` alanına yaz (örnek: `@gundemsondakik`).

### 4. Botu çalıştır
1. GitHub'da **Actions** sekmesine gir, soldan **Telegram haber botu**'nu seç.
2. **Run workflow** düğmesine bas. 1 dakika içinde kanalda ilk haberler görünür.
3. Bundan sonra her 10 dakikada bir kendi kendine çalışır. Aralık `ARALIK_DK` ile iş akışı dosyasında değiştirilir.

Not: Zamanlanmış çalışma yalnızca deponun **ana dalında** (main) çalışır. Bu klasör başka bir daldaysa önce ana dala birleştir.

## Ayarlar (`feeds.json`)

| Alan                      | Anlamı                                                     |
|---------------------------|------------------------------------------------------------|
| `kanal`                   | Gönderilecek kanalın kullanıcı adı, `@` ile                |
| `kaynak_gosterimi`        | `link`: kaynak adı tıklanabilir link, `ad`: sadece kaynak adı, `yok`: hiçbir şey |
| `garanti`                 | Her çalışmada belirli kaynaklardan en az N içerik, örnek `{"@kanal": 1}` |
| `kaynaklar`               | RSS listesi. İstediğin kadar ekle veya sil.                |
| `max_gonderi_per_calisma` | Her çalışmada en fazla kaç haber gönderilsin (önerilen 3-5)|
| `max_haber_yasi_saat`     | Bundan eski haberler atlanır                               |
| `engelli_kelimeler`       | Başlığında bu kelimeler geçen haberler gönderilmez         |

Kaynak değiştirmek için `feeds.json`'u düzenleyip kaydet, bot bir sonraki çalışmada yeni listeyi kullanır.

### Anlık modüller (`moduller`)

| Modül            | Ne yapar                                                                 | Ayarlar                          |
|------------------|--------------------------------------------------------------------------|----------------------------------|
| `deprem`         | Kandilli'den depremleri alır, eşik üstündekileri limitten bağımsız anında gönderir | `min_buyukluk` (önerilen 3.5)    |
| `google_trends`  | Google Trends Türkiye'de o an yükselen aramaları ilgili haber linkiyle gönderir | `min_arama` (önerilen 2000)      |
| `telegram_kanallar` | Herkese açık Telegram kanallarının yeni mesajlarını kaynak linkiyle paylaşır | `kanallar`, `max_uzunluk`, `max_yas_saat`; video ve fotoğraflar doğrudan gönderilir |
| `twitter_gundem` | Türkiye X/Twitter gündemindeki ilk N başlığı belirli aralıkla özet olarak gönderir | `her_kac_saatte`, `adet`         |

Her modül `"aktif": false` yapılarak kapatılabilir.

## Bilgisayarda deneme

```bash
pip install -r requirements.txt
python bot.py --dry-run                      # göndermez, sadece listeler
TELEGRAM_BOT_TOKEN=... python bot.py
```

## Sorun giderme

- **Kanalda hiçbir şey yok:** Actions sekmesinde son çalışmayı aç, kırmızı adımın çıktısına bak.
  `chat not found` → `feeds.json` içindeki `kanal` yanlış veya bot kanala yönetici olarak eklenmemiş.
  `Unauthorized` → token yanlış.
- **Aynı haber iki kez geldi:** `state/posted.json` commit edilememiş olabilir. Actions'ta son adımın hatasına bak.
- **Kanalı sıfırdan doldurmak istemiyorum:** İlk çalışmada yalnızca en yeni 5 haber gider, gerisi gönderilmiş sayılır. Bu bilinçli bir seçim.

## Sonraki adımlar (isteğe bağlı)

- Haberlere yapay zeka ile 2 cümlelik Türkçe özet ekleme.
- Birden fazla kanal (teknoloji, ekonomi, spor) için ayrı feeds dosyaları.
- Gün sonunda "bugünün en çok okunan 5 haberi" özeti.
