"""Haber kaynaklarından yeni içerikleri alıp Telegram kanalına gönderir.

Kaynak tipleri:
  - rss            : klasik haber siteleri
  - deprem         : Kandilli son depremler (anlık, limitten bağımsız)
  - google_trends  : Google Trends Türkiye'de yükselen aramalar
  - twitter_gundem : Türkiye Twitter/X gündemi özeti (trends24 üzerinden)

Kullanım:
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=@kanal python bot.py
    python bot.py --dry-run      # göndermeden sadece ekrana yazar
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

BASE = Path(__file__).parent
CONFIG = BASE / "feeds.json"
STATE = BASE / "state" / "posted.json"
MAX_STATE = 3000  # hafızada tutulacak son içerik sayısı
MAX_IMZA = 600  # aynı haber tespiti için tutulacak son başlık sayısı
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
TR = timezone(timedelta(hours=3))
KAYNAK_GOSTERIMI = "link"  # feeds.json "kaynak_gosterimi": link | ad | yok


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def kimlik(*parcalar: str) -> str:
    return hashlib.sha1("|".join(p.strip() for p in parcalar).encode()).hexdigest()[:16]


def temizle(metin: str, limit: int = 300) -> str:
    """HTML etiketlerini sök, boşlukları sadeleştir, kısalt."""
    metin = re.sub(r"<[^>]+>", " ", metin or "")
    metin = html.unescape(metin)
    metin = re.sub(r"\s+", " ", metin).strip()
    if len(metin) > limit:
        metin = metin[:limit].rsplit(" ", 1)[0] + "…"
    return metin


def indir(url: str, timeout: int = 20) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r


def simdi() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------- RSS


def gorsel_bul(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    for alan in ("summary", "content"):
        icerik = entry.get(alan)
        if isinstance(icerik, list):
            icerik = icerik[0].get("value", "") if icerik else ""
        m = re.search(r'<img[^>]+src="([^"]+)"', icerik or "")
        if m:
            return m.group(1)
    return None


def tarih(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def rss_topla(kaynak: dict, esik: datetime, engelli: list[str]) -> list[dict]:
    feed = feedparser.parse(indir(kaynak["url"]).content)
    sonuc = []
    for entry in feed.entries:
        link = entry.get("link")
        baslik = temizle(entry.get("title", ""), 200)
        if not link or not baslik:
            continue
        t = tarih(entry)
        if t and t < esik:
            continue
        sonuc.append(
            {
                "id": kimlik(link),
                "tip": "rss",
                "kaynak": kaynak["ad"],
                "baslik": baslik,
                "ozet": temizle(entry.get("summary", "")),
                "link": link,
                "gorsel": gorsel_bul(entry),
                "tarih": t.isoformat() if t else None,
            }
        )
    return sonuc


# ----------------------------------------------------------------------------- Deprem (Kandilli)

KANDILLI_URL = "http://www.koeri.boun.edu.tr/scripts/lst0.asp"
KANDILLI_SATIR = re.compile(
    r"^(\d{4}\.\d{2}\.\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
    r"\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+?)\s{2,}(İlksel|REVIZE.*|.*)$"
)


def _buyukluk(*degerler: str) -> float:
    sayilar = []
    for d in degerler:
        try:
            sayilar.append(float(d))
        except ValueError:
            pass
    return max(sayilar) if sayilar else 0.0


def deprem_topla(ayar: dict, esik: datetime) -> list[dict]:
    metin = indir(KANDILLI_URL).content.decode("windows-1254", errors="replace")
    pre = re.search(r"<pre>(.*?)</pre>", metin, re.S)
    if not pre:
        return []
    min_buyukluk = float(ayar.get("min_buyukluk", 3.5))
    sonuc = []
    for satir in pre.group(1).splitlines():
        m = KANDILLI_SATIR.match(satir.strip())
        if not m:
            continue
        gun, saat, enlem, boylam, derinlik, md, ml, mw, yer, _ = m.groups()
        buyukluk = _buyukluk(ml, mw, md)
        if buyukluk < min_buyukluk:
            continue
        t = datetime.strptime(f"{gun} {saat}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=TR)
        if t < esik:
            continue
        yer = re.sub(r"\s+", " ", yer).strip().title()
        sonuc.append(
            {
                "id": kimlik("deprem", gun, saat, yer),
                "tip": "deprem",
                "oncelikli": True,
                "kaynak": "Kandilli Rasathanesi",
                "baslik": f"{buyukluk:.1f} büyüklüğünde deprem: {yer}",
                "ozet": f"Saat {saat[:5]} · Derinlik {float(derinlik):.0f} km",
                "link": f"https://www.google.com/maps?q={enlem},{boylam}",
                "gorsel": None,
                "tarih": t.astimezone(timezone.utc).isoformat(),
            }
        )
    return sonuc


# ----------------------------------------------------------------------------- Google Trends

GTRENDS_URL = "https://trends.google.com/trending/rss?geo=TR"
HT = "{https://trends.google.com/trending/rss}"


def _sayi(metin: str) -> int:
    rakam = re.sub(r"\D", "", metin or "")
    return int(rakam) if rakam else 0


_LATIN_DISI = re.compile(
    r"[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u0750-\u077F"  # Kiril, İbranice, Arapça
    r"\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u0E00-\u0E7F]"  # Çince, Japonca, Korece, Tayca
)


def latin_disi_mi(metin: str) -> bool:
    """Arapça, Kiril, Çince vb. alfabe içeren metinler (Türkçe olmayan aramalar) elenir."""
    return bool(_LATIN_DISI.search(metin or ""))


def google_trends_topla(ayar: dict) -> list[dict]:
    kok = ET.fromstring(indir(GTRENDS_URL).content)
    min_arama = int(ayar.get("min_arama", 2000))
    gun = simdi().astimezone(TR).strftime("%Y-%m-%d")
    sonuc = []
    for item in kok.iter("item"):
        konu = (item.findtext("title") or "").strip()
        trafik = _sayi(item.findtext(f"{HT}approx_traffic") or "")
        if not konu or trafik < min_arama or latin_disi_mi(konu):
            continue
        haber = item.find(f"{HT}news_item")
        h_baslik = temizle(haber.findtext(f"{HT}news_item_title") if haber is not None else "", 200)
        if latin_disi_mi(h_baslik):
            continue
        h_link = (haber.findtext(f"{HT}news_item_url") if haber is not None else "") or ""
        h_kaynak = (haber.findtext(f"{HT}news_item_source") if haber is not None else "") or ""
        gorsel = (item.findtext(f"{HT}picture") or "").strip() or None
        sonuc.append(
            {
                "id": kimlik("gtrends", gun, konu.lower()),
                "tip": "trend",
                "kaynak": "Google Trends",
                "baslik": konu,
                "ozet": h_baslik,
                "link": h_link or f"https://www.google.com/search?q={requests.utils.quote(konu)}",
                "haber_kaynak": h_kaynak,
                "trafik": trafik,
                "gorsel": gorsel,
                "tarih": simdi().isoformat(),
            }
        )
    return sonuc


# ----------------------------------------------------------------------------- Twitter gündemi

TRENDS24_URL = "https://trends24.in/turkey/"
GETDAYTRENDS_URL = "https://getdaytrends.com/turkey/"


def _trends24() -> list[str]:
    yanit = indir(TRENDS24_URL)
    yanit.encoding = "utf-8"
    # sayfadaki ilk liste en güncel saat dilimidir
    ilk_liste = re.search(r'<ol class="?trend-card__list"?>(.*?)</ol>', yanit.text, re.S)
    if not ilk_liste:
        return []
    return re.findall(r"class=trend-link>([^<]+)", ilk_liste.group(1))


def _getdaytrends() -> list[str]:
    yanit = indir(GETDAYTRENDS_URL)
    yanit.encoding = "utf-8"
    return re.findall(r'href="/turkey/trend/[^"]+"[^>]*>([^<]+)', yanit.text)


def twitter_gundem_topla(ayar: dict) -> list[dict]:
    saat_araligi = int(ayar.get("her_kac_saatte", 3))
    adet = int(ayar.get("adet", 10))
    su_an = simdi().astimezone(TR)
    dilim = f"{su_an:%Y-%m-%d}-{su_an.hour // saat_araligi}"
    basliklar: list[str] = []
    for kaynak_fn in (_trends24, _getdaytrends):
        try:
            basliklar = [html.unescape(b).strip() for b in kaynak_fn()]
        except Exception as e:  # noqa: BLE001
            print(f"[uyari] {kaynak_fn.__name__} okunamadi: {e}", file=sys.stderr)
        if basliklar:
            break
    # tekrarları at, sırayı koru
    gorulen: set[str] = set()
    basliklar = [b for b in basliklar if not latin_disi_mi(b)]
    basliklar = [b for b in basliklar if not (b.lower() in gorulen or gorulen.add(b.lower()))][:adet]
    if not basliklar:
        return []
    return [
        {
            "id": kimlik("twitter", dilim),
            "tip": "twitter",
            "kaynak": "X / Twitter Türkiye",
            "baslik": f"Twitter gündemi · {su_an:%H:%M}",
            "maddeler": basliklar,
            "ozet": "",
            "link": "https://x.com/explore/tabs/trending",
            "gorsel": None,
            "tarih": simdi().isoformat(),
        }
    ]


# ----------------------------------------------------------------------------- Telegram kanalları

TG_MESAJ_BLOK = re.compile(r'<div class="tgme_widget_message_wrap(.*?)(?=<div class="tgme_widget_message_wrap|$)', re.S)


def telegram_kanal_topla(ayar: dict, esik: datetime) -> list[dict]:
    """Herkese açık Telegram kanallarının web önizlemesinden (t.me/s/kanal) mesajları okur.

    Metinsiz (sadece foto/video) mesajlar da alınır; video adresi varsa video olarak gönderilir.
    """
    uzunluk = int(ayar.get("max_uzunluk", 700))
    if ayar.get("max_yas_saat"):
        esik = simdi() - timedelta(hours=float(ayar["max_yas_saat"]))
    sonuc = []
    for kanal in ayar.get("kanallar", []):
        ad = kanal.lstrip("@")
        try:
            sayfa = indir(f"https://t.me/s/{ad}").text
        except Exception as e:  # noqa: BLE001
            print(f"[uyari] @{ad} okunamadi: {e}", file=sys.stderr)
            continue
        for blok in TG_MESAJ_BLOK.findall(sayfa):
            m_post = re.search(r'data-post="([^"]+)"', blok)
            m_time = re.search(r'<time datetime="([^"]+)"', blok)
            if not (m_post and m_time):
                continue
            t = datetime.fromisoformat(m_time.group(1)).astimezone(timezone.utc)
            if t < esik:
                continue
            m_text = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', blok, re.S)
            metin = ""
            if m_text:
                metin = re.sub(r"<br\s*/?>", "\n", m_text.group(1))
                metin = html.unescape(re.sub(r"<[^>]+>", "", metin)).strip()
                metin = re.sub(r"[ \t]+", " ", metin)
                metin = re.sub(r"\n{3,}", "\n\n", metin)
                if len(metin) > uzunluk:
                    metin = metin[:uzunluk].rsplit(" ", 1)[0] + "…"
            m_img = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", blok)
            m_vid = re.search(r'<video[^>]+src="([^"]+)"', blok)
            if "Channel name was changed" in metin or "Channel photo" in metin:
                continue
            if not metin and not m_img and not m_vid:
                continue
            sonuc.append(
                {
                    "id": kimlik("tg", m_post.group(1)),
                    "tip": "telegram",
                    "kaynak": f"@{ad}",
                    "baslik": metin.split("\n", 1)[0][:200] if metin else "",
                    "ozet": metin,
                    "link": f"https://t.me/{m_post.group(1)}",
                    "gorsel": html.unescape(m_img.group(1)) if m_img else None,
                    "video": html.unescape(m_vid.group(1)) if m_vid else None,
                    "tarih": t.isoformat(),
                }
            )
    return sonuc


# ----------------------------------------------------------------------------- Aynı haber tespiti

_TR_MAP = str.maketrans("İIŞĞÜÖÇışğüöç", "iisguocisguoc")
_STOP = {
    "ve", "ile", "bir", "bu", "da", "de", "için", "icin", "son", "dakika", "sonra", "olan", "oldu",
    "etti", "dedi", "mi", "mu", "ne", "o", "ki", "ya", "ama", "daha", "cok", "çok", "var", "yok",
    "haber", "haberi", "haberleri", "iddia", "flas", "flaş",
}


def baslik_imza(baslik: str) -> frozenset[str]:
    """Başlığı karşılaştırma için kelime kümesine indirger."""
    metin = baslik.translate(_TR_MAP).lower()
    metin = re.sub(r"[^a-z0-9 ]+", " ", metin)
    kelimeler = {k for k in metin.split() if len(k) >= 3 and k not in _STOP}
    return frozenset(kelimeler)


def ayni_haber_mi(a: frozenset[str], b: frozenset[str], esik: float = 0.6) -> bool:
    if len(a) < 3 or len(b) < 3:
        return False
    ortak = len(a & b)
    return ortak / min(len(a), len(b)) >= esik


# ----------------------------------------------------------------------------- Toplama ve seçim


def hepsini_topla(cfg: dict, gonderilmis: set[str]) -> list[dict]:
    esik = simdi() - timedelta(hours=cfg.get("max_haber_yasi_saat", 12))
    engelli = [
        re.compile(r"(?<!\w)" + re.escape(k.lower()) + r"\w*")
        for k in cfg.get("engelli_kelimeler", []) if k.strip()
    ]
    moduller = cfg.get("moduller", {})
    toplam: list[dict] = []

    def dene(ad: str, fn, *args):
        try:
            toplam.extend(fn(*args))
        except Exception as e:  # noqa: BLE001
            print(f"[uyari] {ad} okunamadi: {e}", file=sys.stderr)

    for kaynak in cfg.get("kaynaklar", []):
        dene(kaynak["ad"], rss_topla, kaynak, esik, engelli)
    if moduller.get("deprem", {}).get("aktif"):
        dene("deprem", deprem_topla, moduller["deprem"], esik)
    if moduller.get("google_trends", {}).get("aktif"):
        dene("google_trends", google_trends_topla, moduller["google_trends"])
    if moduller.get("telegram_kanallar", {}).get("aktif"):
        dene("telegram_kanallar", telegram_kanal_topla, moduller["telegram_kanallar"], esik)
    if moduller.get("twitter_gundem", {}).get("aktif"):
        dene("twitter_gundem", twitter_gundem_topla, moduller["twitter_gundem"])

    def engelli_mi(h: dict) -> bool:
        harfler = [c for c in h.get("baslik", "") if c.isalpha()]
        if h.get("tip") == "rss" and len(harfler) >= 15 and sum(c.isupper() for c in harfler) / len(harfler) > 0.8:
            return True  # tamamı büyük harf: resmi ilan, duyuru
        metin = " ".join(
            str(h.get(k, "")) for k in ("baslik", "ozet", "link", "haber_kaynak")
        ).lower()
        if h.get("maddeler"):
            metin += " " + " ".join(h["maddeler"]).lower()
        return any(k.search(metin) for k in engelli)

    yeni = [h for h in toplam if h["id"] not in gonderilmis and not engelli_mi(h)]
    yeni.sort(key=lambda h: h["tarih"] or "")  # eskiden yeniye
    return yeni


def sec(
    adaylar: list[dict], limit: int, eski_imzalar: list[frozenset[str]], garanti: dict[str, int] | None = None
) -> list[dict]:
    """Öncelikliler her zaman gider; kalanlar kaynaklar arasında sırayla, limit kadar seçilir.

    Aynı haberin farklı kaynaklardan gelen kopyaları (başlık benzerliği) atlanır;
    hem bu çalışmada seçilenlerle hem de yakın geçmişte gönderilenlerle karşılaştırılır.
    """
    for h in adaylar:
        h["imza"] = baslik_imza(h["baslik"]) if h.get("tip") in ("rss", "telegram") else frozenset()
    oncelikli = [h for h in adaylar if h.get("oncelikli")]
    digerleri = [h for h in adaylar if not h.get("oncelikli")]
    if not limit:
        return adaylar
    kuyruklar: dict[str, list[dict]] = {}
    for h in reversed(digerleri):  # yeniden eskiye
        kuyruklar.setdefault(h["kaynak"], []).append(h)
    secilen: list[dict] = []
    gorulen: list[frozenset[str]] = list(eski_imzalar)

    def tekrar_mi(h: dict) -> bool:
        return bool(h["imza"]) and any(ayni_haber_mi(h["imza"], g) for g in gorulen)

    def kaynaktan_al(k: str) -> bool:
        """Kaynağın en yeni, tekrar olmayan içeriğini seçer; kaynak bu çalışmada bir daha kullanılmaz."""
        while kuyruklar.get(k):
            h = kuyruklar[k].pop(0)
            if tekrar_mi(h):
                h["tekrar"] = True
                continue
            secilen.append(h)
            gorulen.append(h["imza"])
            kuyruklar[k] = []  # kaynak başına tek içerik: daha eskisine dönülmez
            return True
        return False

    # Garantili kaynaklar önce
    for kaynak_adi in (garanti or {}):
        if len(secilen) < limit:
            kaynaktan_al(kaynak_adi)

    for k in list(kuyruklar):
        if len(secilen) >= limit:
            break
        kaynaktan_al(k)
    secilen = oncelikli + secilen
    secilen.sort(key=lambda h: h["tarih"] or "")
    return secilen


# ----------------------------------------------------------------------------- Mesaj ve gönderim


def mesaj_olustur(h: dict) -> str:
    e = html.escape
    tip = h.get("tip", "rss")
    if tip == "deprem":
        return (
            f"🚨 <b>DEPREM</b> · {e(h['baslik'])}\n{e(h['ozet'])}\n\n"
            f'📍 <a href="{e(h["link"])}">Haritada gör</a> · {e(h["kaynak"])}'
        )
    if tip == "trend":
        sayi = f"{h['trafik']:,}".replace(",", ".")
        satirlar = [f"🔥 <b>Gündemde yükseliyor:</b> {e(h['baslik'])} ({sayi}+ arama)"]
        if h["ozet"]:
            satirlar.append(f'📎 <a href="{e(h["link"])}">{e(h["ozet"])}</a>')
            if h.get("haber_kaynak"):
                satirlar[-1] += f" · {e(h['haber_kaynak'])}"
        return "\n\n".join(satirlar)
    if tip == "telegram":
        kaynak = f'📎 <a href="{e(h["link"])}">{e(h["kaynak"])}</a>'
        return f"{e(h['ozet'])}\n\n{kaynak}" if h["ozet"] else kaynak
    if tip == "twitter":
        liste = "\n".join(f"{i}. {e(m)}" for i, m in enumerate(h["maddeler"], 1))
        return f"🐦 <b>{e(h['baslik'])}</b>\n\n{liste}"
    parcalar = [f"<b>{e(h['baslik'])}</b>"]
    if h["ozet"] and h["ozet"].lower() != h["baslik"].lower():
        parcalar.append(e(h["ozet"]))
    gosterim = KAYNAK_GOSTERIMI
    if gosterim == "link":
        parcalar.append(f'📎 <a href="{e(h["link"])}">{e(h["kaynak"])}</a>')
    elif gosterim == "ad":
        parcalar.append(f"📎 {e(h['kaynak'])}")
    return "\n\n".join(parcalar)


def kanal_normalize(deger: str) -> str:
    """'@kanal', 'kanal', 't.me/kanal', 'https://t.me/kanal' veya sayısal id kabul eder."""
    d = deger.strip()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^(www\.)?t\.me/(s/)?", "", d).strip("/")
    if not d or d.lstrip("-").isdigit():
        return d
    return "@" + d.lstrip("@")


MAX_YUKLEME = 50 * 1024 * 1024  # Telegram bot API dosya yükleme sınırı


def medya_indir(url: str, sinir: int = MAX_YUKLEME) -> bytes | None:
    """Telegram'ın kendisi çekemediği adresler (ör. telesco.pe CDN) için dosyayı biz indiririz."""
    try:
        with requests.get(url, headers={"User-Agent": UA, "Referer": "https://t.me/"}, timeout=90, stream=True) as r:
            r.raise_for_status()
            boyut = int(r.headers.get("Content-Length") or 0)
            if boyut > sinir:
                print(f"[uyari] medya cok buyuk ({boyut // 1024 // 1024} MB), atlanıyor")
                return None
            veri = bytearray()
            for parca in r.iter_content(256 * 1024):
                veri.extend(parca)
                if len(veri) > sinir:
                    print("[uyari] medya cok buyuk, atlanıyor")
                    return None
            return bytes(veri)
    except Exception as e:  # noqa: BLE001
        print(f"[uyari] medya indirilemedi: {e}")
        return None


def _medya_gonder(api: str, chat_id: str, yontem: str, alan: str, url: str, metin: str, dosya_adi: str) -> bool:
    """Önce URL ile dener; Telegram çekemezse dosyayı indirip yükler."""
    veri = {"chat_id": chat_id, "caption": metin, "parse_mode": "HTML"}
    if alan == "video":
        veri["supports_streaming"] = "true"
    r = requests.post(f"{api}/{yontem}", data={**veri, alan: url}, timeout=120)
    if r.ok:
        return True
    if "failed to get HTTP URL content" in r.text or "wrong file identifier" in r.text.lower():
        icerik = medya_indir(url)
        if icerik:
            r = requests.post(f"{api}/{yontem}", data=veri, files={alan: (dosya_adi, icerik)}, timeout=300)
            if r.ok:
                return True
    print(f"[uyari] {yontem} basarisiz: {r.text[:200]}")
    return False


_OG = (
    re.compile(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']', re.I),
    re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', re.I),
)


def kapak_gorseli_bul(link: str) -> str | None:
    """RSS görsel vermediyse haber sayfasının og:image etiketinden kapak görselini alır."""
    try:
        r = requests.get(link, headers={"User-Agent": UA}, timeout=15, stream=True)
        r.raise_for_status()
        parca = b""
        for chunk in r.iter_content(64 * 1024):
            parca += chunk
            if len(parca) > 400 * 1024 or b"</head>" in parca:
                break
        metin = parca.decode(r.encoding or "utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"[uyari] kapak gorseli alinamadi: {e}")
        return None
    for desen in _OG:
        m = desen.search(metin)
        if m:
            url = html.unescape(m.group(1).strip())
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url
    return None


def telegram_gonder(token: str, chat_id: str, h: dict) -> bool:
    api = f"https://api.telegram.org/bot{token}"
    metin = mesaj_olustur(h)
    if h.get("video") and len(metin) <= 1024:
        if _medya_gonder(api, chat_id, "sendVideo", "video", h["video"], metin, "video.mp4"):
            return True
    if not h.get("gorsel") and h.get("tip") == "rss":
        h["gorsel"] = kapak_gorseli_bul(h["link"])
    if h.get("gorsel") and len(metin) <= 1024:
        if _medya_gonder(api, chat_id, "sendPhoto", "photo", h["gorsel"], metin, "foto.jpg"):
            return True
    # Link önizlemesi kapalı: aksi halde başlık ve özet önizlemede ikinci kez görünür
    r = requests.post(
        f"{api}/sendMessage",
        json={"chat_id": chat_id, "text": metin, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}},
        timeout=30,
    )
    if not r.ok:
        print(f"[hata] gonderilemedi: {r.text[:300]}", file=sys.stderr)
    return r.ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Telegram'a göndermeden listele")
    args = ap.parse_args()

    cfg = load_json(CONFIG, None)
    if not cfg:
        print("feeds.json bulunamadi", file=sys.stderr)
        return 1
    global KAYNAK_GOSTERIMI
    KAYNAK_GOSTERIMI = cfg.get("kaynak_gosterimi", "link")
    state = load_json(STATE, {"gonderilen": []})
    gonderilmis = set(state["gonderilen"])

    eski_imzalar = [frozenset(i) for i in state.get("imzalar", [])]
    son_tarih: dict[str, str] = state.get("son_tarih", {})
    adaylar = hepsini_topla(cfg, gonderilmis)
    # Her kaynaktan yalnızca en son gönderilenden daha yeni içerik alınır; geriye dönülmez
    adaylar = [
        h for h in adaylar
        if not (h.get("tarih") and son_tarih.get(h["kaynak"]) and h["tarih"] <= son_tarih[h["kaynak"]])
    ]
    secilen = sec(adaylar, cfg.get("max_gonderi_per_calisma", 5), eski_imzalar, cfg.get("garanti"))
    tekrar = sum(1 for h in adaylar if h.get("tekrar"))
    print(f"{len(adaylar)} yeni icerik bulundu, {tekrar} tekrar atlandi, {len(secilen)} tanesi gonderilecek")

    if args.dry_run:
        for h in secilen:
            print(f"--- [{h['tip']}/{h['kaynak']}]\n{mesaj_olustur(h)}\n")
        for h in adaylar:
            if h.get("tekrar"):
                print(f"[tekrar atlandi] [{h['kaynak']}] {h['baslik']}")
        return 0

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = kanal_normalize(cfg.get("kanal") or os.environ.get("TELEGRAM_CHAT_ID") or "")
    if not token:
        print("TELEGRAM_BOT_TOKEN ortam degiskeni gerekli", file=sys.stderr)
        return 1
    if not chat_id:
        print("feeds.json icinde 'kanal' alani (ornek: @kanaladi) gerekli", file=sys.stderr)
        return 1

    basarili = 0
    for h in secilen:
        if telegram_gonder(token, chat_id, h):
            print(f"gonderildi: [{h['kaynak']}] {h['baslik']}")
            state["gonderilen"].append(h["id"])
            if h.get("imza"):
                state.setdefault("imzalar", []).append(sorted(h["imza"]))
            if h.get("tarih"):
                son_tarih[h["kaynak"]] = max(son_tarih.get(h["kaynak"], ""), h["tarih"])
            basarili += 1
            time.sleep(2)  # Telegram hız limiti
    # Tarihi olmayan içerikler zaman sırasıyla elenemez; seçilmeyenleri işaretle ki tekrar gelmesin
    secilen_idler = {h["id"] for h in secilen}
    for h in adaylar:
        if not h.get("tarih") and h["id"] not in secilen_idler and h["id"] not in state["gonderilen"]:
            state["gonderilen"].append(h["id"])
    state["son_tarih"] = son_tarih

    state["gonderilen"] = state["gonderilen"][-MAX_STATE:]
    state["imzalar"] = state.get("imzalar", [])[-MAX_IMZA:]
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")
    if secilen and basarili == 0:
        print(f"[hata] {len(secilen)} icerikten hicbiri gonderilemedi, kanal: {chat_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
