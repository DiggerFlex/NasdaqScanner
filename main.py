import time
import datetime
import threading
import os
import requests
import feedparser
import pandas as pd
import numpy as np
from dateutil import parser
from concurrent.futures import ThreadPoolExecutor
from flask import Flask
from zoneinfo import ZoneInfo  # NASDAQ saat dilimi için

# ==========================================
# 1. RENDER UYANIK TUTMA (FLASK SUNUCUSU)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "⚡ NASDAQ SCANNER ACTIVE ⚡"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


# ==========================================
# 2. AYARLAR VE DİNAMİK DEĞİŞKENLER
# ==========================================
TELEGRAM_BOT_TOKEN = "8750813780:AAHKpVFsxqT6BgYbISMZhiAp-ryzNsZ8IZY"
TELEGRAM_CHAT_ID = "7743041008"
RENDER_DEPLOY_HOOK_URL = "https://api.render.com/deploy/srv-daemtan40ujc73ft425g?key=o1ghEoCwW10"
POLYGON_API_KEY = "2MCQinSzcjSGpa2NtrgML6xHrYCK4tLZ"
MAX_PRICE_LIMIT = 15.00  # Varsayılan limit $15.00 çekildi

bildirilen_fiyatlar = {}    # { 'SYMBOL': son_bildirilen_fiyat } -> Çoklu kırılım takibi için
gunluk_sinyaller = {}       # { 'SYMBOL': {'entry': price, 'tp1': val, 'tp2': val} }
gonderilen_haberler = set() 

# Zaman kontrol bayrakları
rapor_gonderildi_bugun = False
acilis_bildirildi_bugun = False
son_gun_str = ""

last_update_id = 0         
is_running = True

# Çakışmaları önlemek için kilit mekanizması
lock = threading.Lock()


# ==========================================
# 3. TELEGRAM İLETİŞİM FONKSİYONLARI
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": message, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Baglanti Hatasi: {e}")

def edit_telegram_msg(message_id, new_text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Mesaj guncelleme hatasi: {e}")

def check_telegram_commands():
    global MAX_PRICE_LIMIT, last_update_id, is_running
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    
    try:
        start_req = time.time()
        res = requests.get(url, params={"offset": last_update_id + 1, "timeout": 2}).json()
        latency = (time.time() - start_req) * 1000
        
        if "result" in res:
            for update in res["result"]:
                last_update_id = update["update_id"]
                if "message" in update and "text" in update["message"]:
                    text = update["message"]["text"].strip()
                    
                    if text == "/stop":
                        if is_running:
                            is_running = False
                            send_telegram_msg("🔴 *BOT DURDURULDU*\n_Tarama donduruldu._")
                        else:
                            send_telegram_msg("⚠️ _Tarama zaten pasif._")

                    elif text == "/start":
                        if not is_running:
                            is_running = True
                            send_telegram_msg("🟢 *BOT DEVREDE*\n_Piyasa taranıyor..._")
                        else:
                            send_telegram_msg("⚠️ _Tarama zaten aktif._")

                    elif text in ["/ping", "/pingms"]:
                        send_telegram_msg(f"⚡ *Gecikme:* `{latency:.0f} ms`")

                    elif text == "/test":
                        process_symbol("ISPC", force_send=True)

                    elif text in ["/status", "/durum"]:
                        status_badge = "🟢 AKTİF" if is_running else "🔴 PASİF"
                        durum_msg = (
                            f"🤖 *TERMINAL DURUMU*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"🌐 *Sistem:* {status_badge}\n"
                            f"💵 *Max Fiyat:* `${MAX_PRICE_LIMIT:.2f}`\n"
                            f"📊 *Sinyal:* `{len(gunluk_sinyaller)}`\n"
                            f"⚡ *Gecikme:* `{latency:.0f} ms`"
                        )
                        send_telegram_msg(durum_msg)

                    elif text in ["/stats", "/ozet"]:
                        if not gunluk_sinyaller:
                            send_telegram_msg("📈 *PORTFÖY:* _Bugün henüz sinyal yok._")
                        else:
                            ozet_msg = f"📊 *GÜNLÜK SİNYALLER ({len(gunluk_sinyaller)}):*\n"
                            ozet_msg += "━━━━━━━━━━━━━━━━━━━━━\n"
                            for sym in gunluk_sinyaller.keys():
                                ozet_msg += f"• *#{sym}* ➔ Giriş: `${gunluk_sinyaller[sym]['entry']:.2f}`\n"
                            send_telegram_msg(ozet_msg)

                    elif text in ["/help", "/yardim"]:
                        yardim_msg = (
                            "⚡ *KOMUTLAR*\n"
                            "━━━━━━━━━━━━━━━━━━━━━\n\n"
                            "▶️ `/start` - Başlatır\n"
                            "⏸️ `/stop` - Durdurur\n"
                            "⚡ `/ping` - Gecikme ölçer\n"
                            "🖥️ `/status` - Durum\n"
                            "📊 `/stats` - Sinyaller\n"
                            "⚙️ `/limit [değer]` - Limit ayarlar\n"
                            "🔄 `/render` - Yeniden başlatır"
                        )
                        send_telegram_msg(yardim_msg)

                    elif text == "/render":
                        if RENDER_DEPLOY_HOOK_URL:
                            init_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                            init_res = requests.post(init_url, json={
                                "chat_id": TELEGRAM_CHAT_ID,
                                "text": "🌀 *Render Başlatılıyor...*",
                                "parse_mode": "Markdown"
                            }).json()
                            msg_id = init_res.get("result", {}).get("message_id")
                            try:
                                requests.post(RENDER_DEPLOY_HOOK_URL, timeout=10)
                                
                                animasyon_kareleri = [
                                    "🔄 *Render Bağlantısı Kuruluyor...* ⏳",
                                    "🔄 *Render Bağlantısı Kuruluyor...*\n\n📡 _Sunucuya erişildi..._",
                                    "⚙️ *Sistem Çekirdeği Yükleniyor...*\n\n[▓░░░░░░░░░] *%10* — _Bağlantı kuruldu_",
                                    "⚙️ *Mumlar Yakılıyor...*\n\n[▓▓▓░░░░░░░] *%30* — _Hisseler tarandı_",
                                    "🔥 *Midas Motoru Aktifleştiriliyor...*\n\n[▓▓▓▓▓░░░░░] *%50* — _Para kazanma modu devrede_ 💵",
                                    "📊 *NASDAQ Veri Akışı Bağlanıyor...*\n\n[▓▓▓▓▓▓▓░░░] *%70* — _Nasdaq taranıyor_",
                                    "🛡️ *Risk Kontrolleri Yapılıyor...*\n\n[▓▓▓▓▓▓▓▓▓░] *%90* — _Best Scanner created by Dipper_",
                                    "🚀 *İŞLEM TAMAMLANDI!*\n\n[▓▓▓▓▓▓▓▓▓▓] *%100*\n\n✨ *Nasdaq Scanner Renderlandı!*"
                                ]
                                
                                for kare in animasyon_kareleri:
                                    time.sleep(1.2)
                                    if msg_id:
                                        edit_telegram_msg(msg_id, kare)
                            except Exception as e:
                                if msg_id:
                                    edit_telegram_msg(msg_id, f"⚠️ *Hata:* {e}")
                        else:
                            send_telegram_msg("⚠️ URL eksik.")

                    elif text.startswith("/limit"):
                        parts = text.split()
                        if len(parts) == 1:
                            send_telegram_msg(f"ℹ️ *Mevcut Limit:* `${MAX_PRICE_LIMIT:.2f}`")
                        elif len(parts) == 2:
                            try:
                                new_limit = float(parts[1])
                                if 0.1 <= new_limit <= 50.0:
                                    MAX_PRICE_LIMIT = new_limit
                                    send_telegram_msg(f"✅ *Limit Güncellendi:* `${MAX_PRICE_LIMIT:.2f}`")
                                else:
                                    send_telegram_msg("⚠️ $0.10 ile $50.00 arası girin.")
                            except ValueError:
                                send_telegram_msg("⚠️ Örnek: `/limit 15`")
    except Exception:
        pass


# ==========================================
# 4. TÜM NASDAQ LİSTESİ
# ==========================================
def get_penny_stocks():
    while True:
        try:
            url = "https://old.nasdaqtrader.com/dynamic/symdir/nasdaqtraded.txt"
            df = pd.read_csv(url, sep="|", timeout=15)
            df = df[(df['NASDAQ Symbol'].notnull()) & (df['ETF'] == 'N') & (df['Test Issue'] == 'N')]
            symbols = df['NASDAQ Symbol'].str.strip().tolist()
            clean_symbols = [s.replace('.', '-') for s in symbols if isinstance(s, str) and len(s) <= 5]
            
            if len(clean_symbols) > 100:
                return clean_symbols
        except Exception as e:
            print(f"Borsa listesi çekilemedi, 5 sn sonra tekrar deneniyor... Hata: {e}")
            time.sleep(5)


# ==========================================
# 5. POLYGON.IO VERİ ÇEKME FONKSİYONU
# ==========================================
def get_polygon_history(symbol):
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/2026-01-01/2026-12-31"
        params = {
            "adjusted": "true",
            "sort": "desc",
            "limit": 50,
            "apiKey": POLYGON_API_KEY
        }
        res = requests.get(url, params=params, timeout=5).json()
        
        if "results" in res and res["results"]:
            df = pd.DataFrame(res["results"])
            df = df.iloc[::-1].reset_index(drop=True)
            df.rename(columns={
                'c': 'Close',
                'o': 'Open',
                'h': 'High',
                'l': 'Low',
                'v': 'Volume'
            }, inplace=True)
            return df
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# ==========================================
# 6. HEDEF VE KIRILIM MOTORU
# ==========================================
def calculate_dynamic_targets(df, last_price):
    try:
        high_low = df['High'] - df['Low']
        high_close = np.abs(df['High'] - df['Close'].shift())
        low_close = np.abs(df['Low'] - df['Close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        atr = true_range.rolling(14).mean().iloc[-1]

        if pd.isna(atr) or atr == 0:
            atr = last_price * 0.03

        tp1_dyn = max(last_price * 1.05, last_price + (1.5 * atr))
        tp1_pct = ((tp1_dyn - last_price) / last_price) * 100

        tp2_dyn = max(last_price * 1.15, last_price + (3.5 * atr))
        tp2_pct = ((tp2_dyn - last_price) / last_price) * 100

        return tp1_dyn, tp1_pct, tp2_dyn, tp2_pct
    except Exception:
        return last_price * 1.07, 7.0, last_price * 1.25, 25.0

def detect_breakout_type(df, vol_ratio, resistance, last_price):
    if len(df) < 3:
        return "Öncü Sinyal"

    c_curr = df['Close'].iloc[-1]
    o_curr = df['Open'].iloc[-1]
    l_prev1 = df['Low'].iloc[-2]

    if c_curr > o_curr and l_prev1 <= resistance:
        return "Dirence Sıkışma"
    elif vol_ratio >= 1.5:
        return "Hacim Toplama"
    else:
        return "Kırılım Adayı"


# ==========================================
# 7. CANLI TARAMA VE ALARM
# ==========================================
def process_symbol(symbol, force_send=False):
    try:
        df = get_polygon_history(symbol)

        if df.empty:
            return

        last_price = df['Close'].iloc[-1]
        open_price = df['Open'].iloc[-1]
        last_volume = df['Volume'].iloc[-1]
        
        resistance = df['High'][:-1].max() if len(df) > 1 else df['High'].max()
        avg_volume = df['Volume'][:-1].mean() if len(df) > 1 else last_volume

        if not force_send:
            if last_price >= MAX_PRICE_LIMIT or last_price <= 0.05:
                return

            # ESNETİLMİŞ DİRENÇ VE HACİM ŞARTLARI
            distance_to_resistance = (last_price - resistance) / resistance if resistance > 0 else 0.0
            vol_ratio = last_volume / avg_volume if avg_volume > 0 else 1.0

            # Dirence %6'ya kadar yaklaşmış veya direnci %6'ya kadar yeni kırmış olanları yakalar
            is_near_breakout = (-0.06 <= distance_to_resistance <= 0.06 and last_price >= open_price)
            is_volume_spike = (vol_ratio >= 1.2)

            if not (is_near_breakout or is_volume_spike):
                return
        else:
            vol_ratio = 3.0

        # ÇOKLU KIRILIM KONTROLÜ (TNON, TRUG vb. için)
        with lock:
            if symbol in bildirilen_fiyatlar:
                son_fiyat = bildirilen_fiyatlar[symbol]
                # Eğer yeni kırılım fiyatı önceki bildirimden en az %5 yukarıda değilse atla
                if last_price < son_fiyat * 1.05 and not force_send:
                    return
            
            bildirilen_fiyatlar[symbol] = last_price

        kirilim_adi = detect_breakout_type(df, vol_ratio, resistance, last_price)
        tight_stop = last_price * 0.98    
        tp1, tp1_pct, tp2, tp2_pct = calculate_dynamic_targets(df, last_price)
        
        tv_url = f"https://www.tradingview.com/chart/?symbol={symbol}"

        msg = (
            f"🚨 *SİNYAL: #{symbol}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 *Durum:* `{kirilim_adi}`\n"
            f"⚡ *Hacim:* `{vol_ratio:.1f}x`\n\n"
            f"💵 *Giriş:* `${last_price:.2f}`\n"
            f"🛡️ *Stop-Loss:* `${tight_stop:.2f}`\n\n"
            f"🎯 *1. Hedef (+%{tp1_pct:.1f}):* `${tp1:.2f}`\n"
            f"🎯 *2. Hedef (+%{tp2_pct:.1f}):* `${tp2:.2f}`\n\n"
            f"📈 [Grafik]({tv_url})"
        )
        
        send_telegram_msg(msg)
        
        if symbol not in gunluk_sinyaller:
            gunluk_sinyaller[symbol] = {
                'entry': last_price,
                'tp1': tp1,
                'tp2': tp2
            }
            
    except Exception:
        pass


# ==========================================
# 8. HABER MODÜLÜ
# ==========================================
def kritik_piyasa_etkisi_analiz_et(metin):
    metin_lower = metin.lower()
    olumsuz_kelimeler = ["war", "strike", "attack", "sanction", "tariff", "tariffs", "threat", "china", "russia", "ban"]
    if any(word in metin_lower for word in olumsuz_kelimeler):
        return "🔴 *Etki:* `NEGATİF`"
    else:
        return "🟢 *Etki:* `POZİTİF`"

def trump_ve_piyasa_haberleri_kontrol_et():
    global gonderilen_haberler
    rss_url = "https://news.google.com/rss/search?q=Trump+(war+OR+tariff+OR+sanction+OR+attack+OR+China+OR+strike)&hl=en-US&gl=US&ceid=US:en"
    kritik_kelimeler = ["war", "tariff", "tariffs", "sanction", "attack", "strike", "china", "russia", "military", "missile", "threat", "ban"]
    
    try:
        feed = feedparser.parse(rss_url)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        
        for entry in feed.entries[:5]:
            haber_id = entry.title
            baslik_lower = entry.title.lower()
            
            if haber_id in gonderilen_haberler:
                continue
                
            if hasattr(entry, 'published'):
                try:
                    pub_time = parser.parse(entry.published)
                    if pub_time.tzinfo is None:
                        pub_time = pub_time.replace(tzinfo=datetime.timezone.utc)
                    if (now_utc - pub_time).total_seconds() / 60.0 > 60:
                        gonderilen_haberler.add(haber_id)
                        continue
                except Exception:
                    pass

            if any(word in baslik_lower for word in kritik_kelimeler):
                etki = kritik_piyasa_etkisi_analiz_et(entry.title)
                msg = (
                    f"🌐 *HABER BÜLTENİ*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📌 *Başlık:* _{entry.title}_\n\n"
                    f"{etki}"
                )
                send_telegram_msg(msg)
                gonderilen_haberler.add(haber_id)
    except Exception as e:
        print(f"Haber hatasi: {e}")

def haber_tarama_loop():
    while True:
        trump_ve_piyasa_haberleri_kontrol_et()
        time.sleep(3600)


# ==========================================
# 9. GÜN SONU RAPORU
# ==========================================
def gun_sonu_raporu_gonder():
    global gunluk_sinyaller
    if not gunluk_sinyaller:
        send_telegram_msg("PARA KAZANMA SANATI\n📊 *GÜNÜN İŞLEMLERİ* 📊\n\n`Bugün sinyal oluşmadı.`")
        return

    rapor = "PARA KAZANMA SANATI\n📊 *GÜNÜN İŞLEMLERİ* 📊\n"
    toplam_kar = 0
    basarili_sayisi = 0

    for symbol, data in gunluk_sinyaller.items():
        try:
            df = get_polygon_history(symbol)
            
            entry = data['entry']
            tp1 = data.get('tp1', entry * 1.05)
            tp2 = data.get('tp2', entry * 1.15)
            
            zirve = df['High'].max() if not df.empty else entry
            
            if zirve >= tp2:
                kademe_str = "(TÜM kademeler tamam)"
                hedef_fiyat = tp2
            elif zirve >= tp1:
                kademe_str = "(1. kademe tamam)"
                hedef_fiyat = tp1
            else:
                kademe_str = "(takipte)"
                hedef_fiyat = zirve

            kar_pct = ((zirve - entry) / entry) * 100
            
            if kar_pct >= 50:
                emoji = "🚀🚀🚀"
            elif kar_pct >= 20:
                emoji = "🚀"
            else:
                emoji = "💰"

            rapor += f"🟢 *{symbol}* ➔ `{entry:.2f}` ➡️ `{hedef_fiyat:.2f}` {kademe_str} | `%{kar_pct:.2f}` kâr {emoji}\n"
            
            if zirve > tp2 * 1.05 and kar_pct > 15:
                rapor += f"└ Gün içi {zirve:.2f}'e yükseldi ➔ anlık %{kar_pct:.2f} 🔥\n"

            toplam_kar += kar_pct
            basarili_sayisi += 1

        except Exception as e:
            print(f"Rapor hatasi ({symbol}): {e}")
            continue

    if basarili_sayisi > 0:
        ort_kar = toplam_kar / basarili_sayisi
        rapor += f"\n📈 *Ortalama Kâr: %{ort_kar:.2f}*"
    
    send_telegram_msg(rapor)
    gunluk_sinyaller.clear()
    
    with lock:
        bildirilen_fiyatlar.clear()

def piyasa_zaman_kontrolu():
    global rapor_gonderildi_bugun, acilis_bildirildi_bugun, son_gun_str
    
    try:
        ny_now = datetime.datetime.now(ZoneInfo("America/New_York"))
        bugun_str = ny_now.strftime("%Y-%m-%d")
        
        if son_gun_str != bugun_str:
            son_gun_str = bugun_str
            rapor_gonderildi_bugun = False
            acilis_bildirildi_bugun = False

        if ny_now.weekday() < 5:
            if ny_now.hour == 9 and ny_now.minute >= 30:
                if not acilis_bildirildi_bugun:
                    send_telegram_msg("🔔 *NASDAQ AÇILDI!*\n_Piyasa işlemleri başladı, tarama aktif._")
                    acilis_bildirildi_bugun = True

            if ny_now.hour >= 16:
                if not rapor_gonderildi_bugun:
                    send_telegram_msg("🔔 *NASDAQ KAPANDI!*\n_Gün sonu raporu hazırlanıyor..._")
                    gun_sonu_raporu_gonder()
                    rapor_gonderildi_bugun = True
    except Exception as e:
        print(f"Zaman kontrol hatasi: {e}")


# ==========================================
# 10. ANA DÖNGÜ
# ==========================================
def canli_kesintisiz_tarama():
    piyasa_zaman_kontrolu()

    symbols = get_penny_stocks()
    if not symbols:
        return

    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(process_symbol, symbols)

def start_scanner_loop():
    welcome_msg = (
        "⚡ *NASDAQ TERMINAL ONLINE* ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 *Limit:* `$15.00 ve Altı`\n"
        "📊 *Kapsam:* `Tüm NASDAQ (Polygon.io Engine)`\n\n"
        "_Tarama başlatıldı..._"
    )
    send_telegram_msg(welcome_msg)
    
    while True:
        try:
            if is_running:
                canli_kesintisiz_tarama()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(10)

def telegram_komut_dinleme_loop():
    while True:
        try:
            check_telegram_commands()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(2)

if __name__ == '__main__':
    threading.Thread(target=haber_tarama_loop, daemon=True).start()
    threading.Thread(target=start_scanner_loop, daemon=True).start()
    threading.Thread(target=telegram_komut_dinleme_loop, daemon=True).start()
    run_flask()
