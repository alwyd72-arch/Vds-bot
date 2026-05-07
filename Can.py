import telebot
from telebot import types
import sqlite3
import subprocess
import sys
import os
import threading
import time
import logging
import re
from datetime import datetime, timedelta
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

TOKEN = "8113976572:AAE2CZPJHRxz0tDMoAb9uEtzNh3o_qexsEk"
ADMIN_IDS = [8434939976]
PREMIUM_STARS = 25  # Kaç yıldız = premium

def is_admin(uid):
    return uid in ADMIN_IDS

from telebot import apihelper
apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(TOKEN, parse_mode=None)

# ================= DATABASE =================
db = sqlite3.connect("data.db", check_same_thread=False)
db_lock = threading.Lock()
sql = db.cursor()

sql.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT,
    premium     INTEGER DEFAULT 0,
    banned      INTEGER DEFAULT 0,
    joined_at   TEXT DEFAULT (datetime('now'))
)
""")

sql.execute("""
CREATE TABLE IF NOT EXISTS bots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    bot_name    TEXT,
    running     INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'pending',
    started_at  TEXT,
    start_count INTEGER DEFAULT 0
)
""")

# Migration: mevcut tablolara eksik sütunları ekle
def migrate():
    sql.execute("PRAGMA table_info(users)")
    ucols = [r[1] for r in sql.fetchall()]
    if "joined_at" not in ucols:
        sql.execute("ALTER TABLE users ADD COLUMN joined_at TEXT DEFAULT '2000-01-01 00:00:00'")

    sql.execute("PRAGMA table_info(bots)")
    bcols = [r[1] for r in sql.fetchall()]
    for col, definition in [
        ("status",      "TEXT DEFAULT 'pending'"),
        ("started_at",  "TEXT"),
        ("start_count", "INTEGER DEFAULT 0"),
    ]:
        if col not in bcols:
            sql.execute(f"ALTER TABLE bots ADD COLUMN {col} {definition}")
    db.commit()

migrate()

# ================= RUNTIME STATE =================
running_processes  = {}   # bot_id -> Popen
bot_logs           = {}   # bot_id -> [lines]
bot_start_time     = {}   # bot_id -> datetime
auto_restart       = {}   # bot_id -> True/False (kullanıcı otomatik yeniden başlatma isteği)
admin_step         = {}
support_wait       = {}
announce_wait      = {}

# Flood koruma: uid -> [timestamp, ...]
flood_tracker      = defaultdict(list)
FLOOD_LIMIT        = 8    # X mesaj
FLOOD_WINDOW       = 10   # N saniye içinde
flood_banned_until = {}   # uid -> datetime

# Tehlikeli komutlar (güvenlik taraması)
DANGEROUS_PATTERNS = [
    r"os\.system\s*\(",
    r"subprocess\.call\s*\(",
    r"subprocess\.Popen\s*\(",
    r"subprocess\.run\s*\(",
    r"subprocess\.check_output\s*\(",
    r"eval\s*\(",
    r"exec\s*\(",
    r"__import__\s*\(",
    r"open\s*\(.*['\"]w['\"]",
    r"shutil\.rmtree",
    r"os\.remove",
    r"os\.rmdir",
    r"socket\.connect",
    r"requests\.get\s*\(",
    r"requests\.post\s*\(",
    r"urllib",
    r"ftplib",
    r"paramiko",
]

# ================= YARDIMCI FONKSİYONLAR =================
def add_log(bot_id, text):
    if bot_id not in bot_logs:
        bot_logs[bot_id] = []
    bot_logs[bot_id].append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
    # Max 500 satır tut
    if len(bot_logs[bot_id]) > 500:
        bot_logs[bot_id] = bot_logs[bot_id][-500:]

def get_uptime(bot_id):
    if bot_id not in bot_start_time:
        return "—"
    delta = datetime.now() - bot_start_time[bot_id]
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}s {m}d {s}sn"

def scan_file(filepath):
    """Dosyayı tehlikeli komutlara karşı tara. Bulunan uyarıları döndür."""
    warnings = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, content):
                warnings.append(pattern.split("\\")[0].replace("s*\\(", "()"))
    except Exception as e:
        warnings.append(f"Tarama hatası: {e}")
    return warnings

def check_flood(uid):
    """True dönerse kullanıcı flood yapıyor."""
    if is_admin(uid):
        return False
    now = datetime.now()
    # Geçici ban kontrol
    if uid in flood_banned_until:
        if now < flood_banned_until[uid]:
            return True
        else:
            del flood_banned_until[uid]
    # Pencere temizle
    flood_tracker[uid] = [t for t in flood_tracker[uid] if (now - t).total_seconds() < FLOOD_WINDOW]
    flood_tracker[uid].append(now)
    if len(flood_tracker[uid]) >= FLOOD_LIMIT:
        flood_banned_until[uid] = now + timedelta(seconds=60)
        flood_tracker[uid].clear()
        return True
    return False

def get_name(bot_id):
    with db_lock:
        sql.execute("SELECT bot_name FROM bots WHERE id=?", (bot_id,))
        result = sql.fetchone()
    return result[0] if result else None

def notify_admins(text, parse_mode=None):
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text, parse_mode=parse_mode)
        except:
            pass

# ================= MENÜLER =================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📦 Modül Yükle", "📂 Dosya Yükle")
    kb.add("📂 Dosyalarım")
    kb.add("⭐ Premium Al", "📞 Destek & İletişim")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⭐ Premium Ver", "👤 Kullanıcı Yasakla / Aç")
    kb.add("🤖 Aktif Botlar", "📊 İstatistikler")
    kb.add("⛔ Bot Kapat", "🛑 Tüm Botları Kapat")
    kb.add("💻 Terminal", "📦 Paket Yükle")
    kb.add("📢 Duyuru Gönder")
    kb.add("⬅️ Çıkış")
    return kb

# ================= FLOOD MIDDLEWARE =================
@bot.middleware_handler(update_types=["message"])
def flood_middleware(bot_instance, message):
    uid = message.from_user.id
    if check_flood(uid):
        try:
            bot.send_message(uid, "⚠️ Çok hızlı mesaj gönderiyorsunuz. 60 saniye beklemeniz gerekiyor.")
        except:
            pass
        raise Exception("Flood detected")

# ================= START =================
@bot.message_handler(commands=["start"])
def start(message):
    u   = message.from_user
    uid = u.id

    with db_lock:
        sql.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        if not sql.fetchone():
            sql.execute("INSERT INTO users (user_id, name, joined_at) VALUES (?,?,?)",
                        (uid, u.first_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            db.commit()
        sql.execute("SELECT premium, banned FROM users WHERE user_id=?", (uid,))
        premium, banned = sql.fetchone()

    if banned:
        bot.send_message(uid, "🚫 Hesabınız yasaklandı.")
        return

    try:
        photos = bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count:
            bot.send_photo(uid, photos.photos[0][0].file_id)
    except:
        pass

    with db_lock:
        sql.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (uid,))
        count = sql.fetchone()[0]

    status = "⭐ Premium Kullanıcı" if premium else "🆓 Ücretsiz Kullanıcı"
    limit  = "Sınırsız" if premium else "3"

    bot.send_message(uid, f"""〽️ Hoş Geldiniz, {u.first_name}!

👤 Durumunuz: {status}
📁 Dosya Sayınız: {count} / {limit}

🤖 Bu bot Python (.py) betiklerini çalıştırmak için tasarlanmıştır.

👇 Butonları kullanın.""", reply_markup=main_menu())

# ================= ADMIN PANEL =================
@bot.message_handler(commands=["adminpanel"])
def adminpanel(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(message.chat.id, "👑 Admin Panel", reply_markup=admin_menu())

@bot.message_handler(func=lambda m: m.text == "⬅️ Çıkış" and is_admin(m.from_user.id))
def exit_admin(message):
    bot.send_message(message.chat.id, "Çıkıldı.", reply_markup=main_menu())

# ================= 📊 İSTATİSTİKLER =================
@bot.message_handler(func=lambda m: m.text == "📊 İstatistikler" and is_admin(m.from_user.id))
def stats(message):
    with db_lock:
        sql.execute("SELECT COUNT(*) FROM users")
        total_users = sql.fetchone()[0]

        sql.execute("SELECT COUNT(*) FROM users WHERE premium=1")
        premium_users = sql.fetchone()[0]

        sql.execute("SELECT COUNT(*) FROM users WHERE banned=1")
        banned_users = sql.fetchone()[0]

        # Son 24 saat yeni kayıt
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        sql.execute("SELECT COUNT(*) FROM users WHERE joined_at >= ?", (since,))
        new_today = sql.fetchone()[0]

        sql.execute("SELECT COUNT(*) FROM bots")
        total_bots = sql.fetchone()[0]

        sql.execute("SELECT COUNT(*) FROM bots WHERE running=1")
        active_bots = sql.fetchone()[0]

        sql.execute("SELECT COUNT(*) FROM bots WHERE status='pending'")
        pending_bots = sql.fetchone()[0]

        # En çok bot çalıştıran kullanıcılar (top 5)
        sql.execute("""
            SELECT u.name, u.user_id, COUNT(b.id) as cnt
            FROM bots b JOIN users u ON b.user_id = u.user_id
            GROUP BY b.user_id ORDER BY cnt DESC LIMIT 5
        """)
        top_users = sql.fetchall()

    top_text = ""
    for i, (name, uid, cnt) in enumerate(top_users, 1):
        top_text += f"  {i}. {name} ({uid}) — {cnt} bot\n"

    # Aktif bot uptime özeti
    uptime_text = ""
    for bid, proc in list(running_processes.items()):
        uptime_text += f"  • Bot #{bid}: {get_uptime(bid)}\n"

    text = f"""📊 *Bot İstatistikleri*

👥 *Kullanıcılar*
├ Toplam: {total_users}
├ Premium: {premium_users}
├ Yasaklı: {banned_users}
└ Son 24s yeni: {new_today}

🤖 *Botlar*
├ Toplam dosya: {total_bots}
├ Şu an çalışan: {active_bots}
└ Onay bekleyen: {pending_bots}

🏆 *En Aktif Kullanıcılar*
{top_text if top_text else "  Veri yok"}

⏱ *Çalışan Bot Süreleri*
{uptime_text if uptime_text else "  Aktif bot yok"}"""

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# ================= DUYURU =================
@bot.message_handler(func=lambda m: m.text == "📢 Duyuru Gönder" and is_admin(m.from_user.id))
def announce_prompt(message):
    announce_wait[message.from_user.id] = True
    bot.send_message(message.chat.id, "📢 Göndermek istediğiniz duyuruyu yazın:")

@bot.message_handler(func=lambda m: m.from_user.id in announce_wait)
def announce_send(message):
    announce_wait.pop(message.from_user.id, None)
    duyuru_text = message.text
    with db_lock:
        sql.execute("SELECT user_id FROM users")
        rows = sql.fetchall()
    sent = 0
    for (uid,) in rows:
        try:
            bot.send_message(uid, f"📢 *Duyuru*\n\n{duyuru_text}", parse_mode="Markdown")
            sent += 1
        except:
            pass
    notify_admins(f"📢 Duyuru gönderildi. Toplam: {sent} kullanıcı")

# ================= PREMIUM VER =================
@bot.message_handler(func=lambda m: m.text == "⭐ Premium Ver" and is_admin(m.from_user.id))
def premium_prompt(message):
    admin_step[message.from_user.id] = "premium"
    bot.send_message(message.chat.id, "🆔 Kullanıcı ID gir:")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "premium")
def premium_set(message):
    try:
        uid = int(message.text)
        with db_lock:
            sql.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            if not sql.fetchone():
                bot.send_message(message.chat.id, "❌ Kullanıcı bulunamadı.")
            else:
                sql.execute("UPDATE users SET premium=1 WHERE user_id=?", (uid,))
                db.commit()
                bot.send_message(message.chat.id, f"✅ Kullanıcı {uid} artık Premium.")
                bot.send_message(uid, "⭐ Tebrikler! Artık Premium kullanıcı oldunuz.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata: {e}")
    admin_step.pop(message.from_user.id, None)

# ================= KULLANICI BAN =================
@bot.message_handler(func=lambda m: m.text == "👤 Kullanıcı Yasakla / Aç" and is_admin(m.from_user.id))
def ban_prompt(message):
    admin_step[message.from_user.id] = "ban"
    bot.send_message(message.chat.id, "🆔 Kullanıcı ID gönder:")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "ban")
def ban_user(message):
    try:
        uid = int(message.text)
        with db_lock:
            sql.execute("SELECT banned FROM users WHERE user_id=?", (uid,))
            row = sql.fetchone()
        if not row:
            bot.send_message(message.chat.id, "❌ Kullanıcı yok.")
        else:
            new = 0 if row[0] == 1 else 1
            with db_lock:
                sql.execute("UPDATE users SET banned=? WHERE user_id=?", (new, uid))
                db.commit()
            bot.send_message(message.chat.id, f"✅ Kullanıcı {'açıldı' if new == 0 else 'yasaklandı'}.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata: {e}")
    admin_step.pop(message.from_user.id, None)

# ================= AKTİF BOTLAR =================
@bot.message_handler(func=lambda m: m.text == "🤖 Aktif Botlar" and is_admin(m.from_user.id))
def active_bots(message):
    with db_lock:
        sql.execute("SELECT id, user_id, bot_name FROM bots WHERE running=1")
        rows = sql.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "Aktif bot yok.")
        return
    text = "🔥 *Aktif Botlar:*\n\n"
    for r in rows:
        bid, uid, name = r
        text += f"🤖 Bot #{bid}\n👤 Kullanıcı: {uid}\n📄 Dosya: {name}\n⏱ Süre: {get_uptime(bid)}\n\n"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# ================= BOT KAPAT (Admin) =================
@bot.message_handler(func=lambda m: m.text == "⛔ Bot Kapat" and is_admin(m.from_user.id))
def stop_bot_prompt(message):
    admin_step[message.from_user.id] = "stopbot_full"
    bot.send_message(message.chat.id, "🆔 Kullanıcı ID ve Dosya Adı girin (örnek: 12345678 dosya.py)")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "stopbot_full")
def stop_bot_full(message):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            return bot.send_message(message.chat.id, "❌ Format: KullanıcıID DosyaAdı")
        uid, filename = int(parts[0]), parts[1]
        with db_lock:
            sql.execute("SELECT id FROM bots WHERE user_id=? AND bot_name=?", (uid, filename))
            row = sql.fetchone()
        if not row:
            return bot.send_message(message.chat.id, "❌ Bot bulunamadı.")
        bot_id = row[0]
        _stop_process(bot_id)
        bot.send_message(message.chat.id, f"✅ {filename} durduruldu.")
        bot.send_message(uid, f"⛔ Botunuz admin tarafından durduruldu: `{filename}`", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata: {e}")
    admin_step.pop(message.from_user.id, None)

# ================= TÜM BOTLARI KAPAT =================
@bot.message_handler(func=lambda m: m.text == "🛑 Tüm Botları Kapat" and is_admin(m.from_user.id))
def stop_all(message):
    count = len(running_processes)
    for bid in list(running_processes.keys()):
        _stop_process(bid)
    bot.send_message(message.chat.id, f"✅ {count} bot durduruldu.")

# ================= MODÜL YÜKLE =================
@bot.message_handler(func=lambda m: m.text == "📦 Modül Yükle")
def mod_prompt(message):
    msg = bot.send_message(message.chat.id, "📦 pip modül adı gir (örn: requests):")
    bot.register_next_step_handler(msg, mod_install)

def mod_install(message):
    module = message.text.strip()
    # Basit güvenlik: sadece harf, rakam, tire, alt çizgi, nokta
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', module):
        return bot.send_message(message.chat.id, "❌ Geçersiz modül adı.")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", module],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bot.send_message(message.chat.id, f"✅ `{module}` yüklendi.", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata:\n{e}")

# ================= DOSYA YÜKLE =================
@bot.message_handler(func=lambda m: m.text == "📂 Dosya Yükle")
def upload_prompt(message):
    bot.send_message(message.chat.id, "📂 .py dosyanızı gönderin:")

@bot.message_handler(content_types=["document"])
def upload(message):
    if not message.document.file_name.endswith(".py"):
        return bot.reply_to(message, "❌ Sadece .py dosya kabul edilir.")

    uid = message.from_user.id
    with db_lock:
        sql.execute("SELECT premium FROM users WHERE user_id=?", (uid,))
        row = sql.fetchone()
        if not row:
            return bot.reply_to(message, "❌ Önce /start yazın.")
        premium = row[0]
        sql.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (uid,))
        c = sql.fetchone()[0]

    if not premium and c >= 3:
        return bot.reply_to(message, "❌ Dosya limitiniz doldu (3/3). Premium alın.")

    file = bot.get_file(message.document.file_id)
    data = bot.download_file(file.file_path)
    filename = message.document.file_name

    base, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(filename):
        filename = f"{base}_{counter}{ext}"
        counter += 1

    with open(filename, "wb") as f:
        f.write(data)

    # 🔒 GÜVENLİK TARAMASI
    warnings = scan_file(filename)

    # Admin yüklüyorsa direkt onaylı kaydet, değilse pending
    initial_status = 'approved' if is_admin(uid) else 'pending'

    with db_lock:
        sql.execute("INSERT INTO bots (user_id, bot_name, status) VALUES (?,?,?)",
                    (uid, filename, initial_status))
        db.commit()
        bot_id = sql.lastrowid

    # ======= ADMİN YÜKLÜYORSA: OTOMATİK ONAYLA =======
    if is_admin(uid):
        warn_text = ""
        if warnings:
            warn_text = "\n\n⚠️ *Güvenlik uyarıları var:*\n" + "\n".join(f"• `{w}`" for w in warnings)

        bot.reply_to(
            message,
            f"✅ Dosya yüklendi ve *otomatik onaylandı* (Admin).\n"
            f"📄 `{filename}`{warn_text}\n\n"
            f"Dosyalarım menüsünden başlatabilirsiniz.",
            parse_mode="Markdown"
        )
        return  # Admin için butonlu bildirim gönderme, işlem tamam

    # ======= NORMAL KULLANICI: ADMIN ONAY BEKLESİN =======
    bot.reply_to(message, "✅ Dosya yüklendi. Admin onayı bekleniyor.")

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Onayla", callback_data=f"approve_{bot_id}"),
        types.InlineKeyboardButton("❌ Reddet", callback_data=f"reject_{bot_id}")
    )

    warn_text = ""
    if warnings:
        warn_text = "\n\n⚠️ *GÜVENLİK UYARILARI:*\n" + "\n".join(f"• `{w}`" for w in warnings)

    caption = (f"📂 Yeni Dosya Yüklendi\n"
               f"👤 {message.from_user.first_name} ({uid})\n"
               f"📄 {filename}{warn_text}")

    for admin_id in ADMIN_IDS:
        try:
            with open(filename, "rb") as f:
                bot.send_document(admin_id, f, caption=caption,
                                  reply_markup=kb, parse_mode="Markdown")
        except:
            pass

# ================= DOSYALARIM =================
@bot.message_handler(func=lambda m: m.text == "📂 Dosyalarım")
def files(message):
    uid = message.from_user.id
    with db_lock:
        sql.execute("SELECT id, bot_name, running, status, start_count FROM bots WHERE user_id=?", (uid,))
        rows = sql.fetchall()
    if not rows:
        return bot.send_message(uid, "📂 Henüz dosya yok.")

    for bot_id, bot_name, running, status, start_count in rows:
        if status == 'pending':
            durum = "⏳ Onay Bekliyor"
        elif status == 'rejected':
            durum = "❌ Reddedildi"
        else:
            durum = "🟢 Çalışıyor" if running else "🔴 Durdu"

        uptime_str = f"\n⏱ Süre: {get_uptime(bot_id)}" if running else ""
        info = (f"📄 *{bot_name}*\n"
                f"🆔 ID: {bot_id}\n"
                f"📊 Durum: {durum}\n"
                f"🔁 Toplam Başlatma: {start_count}{uptime_str}")

        kb = types.InlineKeyboardMarkup()
        if status == 'approved':
            kb.row(
                types.InlineKeyboardButton("▶️ Başlat",   callback_data=f"start_{bot_id}"),
                types.InlineKeyboardButton("⛔ Durdur",   callback_data=f"stop_{bot_id}"),
                types.InlineKeyboardButton("🔄 Yeniden",  callback_data=f"restart_{bot_id}"),
            )
            kb.row(
                types.InlineKeyboardButton("❌ Sil",      callback_data=f"delete_{bot_id}"),
                types.InlineKeyboardButton("📄 Log",      callback_data=f"log_{bot_id}"),
                types.InlineKeyboardButton("📋 Canlı Log",callback_data=f"livelog_{bot_id}"),
            )
        else:
            kb.row(
                types.InlineKeyboardButton("ℹ️ Onay Bekliyor", callback_data=f"info_{bot_id}"),
                types.InlineKeyboardButton("❌ Sil",           callback_data=f"delete_{bot_id}")
            )
        bot.send_message(uid, info, reply_markup=kb, parse_mode="Markdown")

# ================= PROCESS YÖNETİMİ =================
def _stop_process(bot_id):
    auto_restart[bot_id] = False
    proc = running_processes.get(bot_id)
    if proc:
        try:
            proc.terminate()
        except:
            pass
        running_processes.pop(bot_id, None)
    bot_start_time.pop(bot_id, None)
    with db_lock:
        sql.execute("UPDATE bots SET running=0 WHERE id=?", (bot_id,))
        db.commit()
    add_log(bot_id, "Bot durduruldu")

def run_bot_with_log(bot_id, filename, owner_id=None):
    def target():
        attempt = 0
        while True:
            attempt += 1
            proc = None
            try:
                kwargs = {}
                if sys.platform != "win32":
                    kwargs["start_new_session"] = True

                proc = subprocess.Popen(
                    [sys.executable, "-u", filename],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    **kwargs
                )
                running_processes[bot_id] = proc
                bot_start_time[bot_id] = datetime.now()

                with db_lock:
                    sql.execute(
                        "UPDATE bots SET running=1, status='approved', started_at=?, start_count=start_count+1 WHERE id=?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), bot_id)
                    )
                    db.commit()

                if attempt == 1:
                    add_log(bot_id, f"Bot baslatildi (PID: {proc.pid})")
                else:
                    add_log(bot_id, f"Yeniden baslatildi #{attempt} (PID: {proc.pid})")
                    if owner_id:
                        try:
                            bot.send_message(owner_id, f"Bot yeniden baslatildi: {filename} (#{attempt})")
                        except:
                            pass

                for line in proc.stdout:
                    stripped = line.strip()
                    if stripped:
                        add_log(bot_id, stripped)

                proc.wait()
                exit_code = proc.returncode

            except Exception as e:
                add_log(bot_id, f"Popen hatasi: {e}")
                exit_code = -1

            finally:
                running_processes.pop(bot_id, None)
                bot_start_time.pop(bot_id, None)

            add_log(bot_id, f"Bot durdu (exit={exit_code})")

            if not auto_restart.get(bot_id, False):
                with db_lock:
                    sql.execute("UPDATE bots SET running=0 WHERE id=?", (bot_id,))
                    db.commit()
                break

            if not os.path.exists(filename):
                add_log(bot_id, "Dosya yok, watchdog durdu.")
                auto_restart.pop(bot_id, None)
                with db_lock:
                    sql.execute("UPDATE bots SET running=0 WHERE id=?", (bot_id,))
                    db.commit()
                break

            add_log(bot_id, "10 saniye sonra yeniden baslatiliyor...")
            time.sleep(10)

    threading.Thread(target=target, daemon=True).start()

# ================= CALLBACK =================
@bot.callback_query_handler(func=lambda c: True)
def cb(call):
    try:
        action, bot_id_str = call.data.split("_", 1)
        bot_id = int(bot_id_str)
    except:
        return

    caller_id = call.from_user.id

    # --- APPROVE ---
    if action == "approve":
        if not is_admin(caller_id):
            return
        with db_lock:
            sql.execute("SELECT user_id, bot_name FROM bots WHERE id=? AND status='pending'", (bot_id,))
            row = sql.fetchone()
        if not row:
            return bot.answer_callback_query(call.id, "Zaten tamamlanmış.", show_alert=True)
        uid, filename = row
        with db_lock:
            sql.execute("UPDATE bots SET status='approved' WHERE id=?", (bot_id,))
            db.commit()
        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption="✅ ONAYLANDI\n" + (call.message.caption or "")
            )
        except:
            pass
        bot.send_message(uid, f"✅ Dosyanız onaylandı: `{filename}`\nDosyalarım menüsünden başlatabilirsiniz.", parse_mode="Markdown")

    # --- REJECT ---
    elif action == "reject":
        if not is_admin(caller_id):
            return
        with db_lock:
            sql.execute("SELECT user_id, bot_name FROM bots WHERE id=? AND status='pending'", (bot_id,))
            row = sql.fetchone()
        if not row:
            return bot.answer_callback_query(call.id, "Zaten tamamlanmış.", show_alert=True)
        uid, filename = row
        if os.path.exists(filename):
            os.remove(filename)
        with db_lock:
            sql.execute("DELETE FROM bots WHERE id=?", (bot_id,))
            db.commit()
        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption="❌ REDDEDİLDİ\n" + (call.message.caption or "")
            )
        except:
            pass
        bot.send_message(uid, f"❌ Dosyanız reddedildi: `{filename}`", parse_mode="Markdown")

    elif action == "info":
        bot.answer_callback_query(call.id, "Bu dosya admin onayı bekliyor.", show_alert=True)

    else:
        with db_lock:
            sql.execute("SELECT status, user_id FROM bots WHERE id=?", (bot_id,))
            res = sql.fetchone()
        if not res:
            return bot.answer_callback_query(call.id, "Dosya bulunamadı.", show_alert=True)
        status, owner_id = res

        if caller_id != owner_id and not is_admin(caller_id):
            return bot.answer_callback_query(call.id, "❌ Bu dosya size ait değil.", show_alert=True)

        if action in ("start", "stop", "restart") and status != "approved":
            return bot.answer_callback_query(call.id, "❌ Admin onayı bekleniyor.", show_alert=True)

        # --- START ---
        if action == "start":
            if bot_id in running_processes:
                return bot.answer_callback_query(call.id, "Bot zaten çalışıyor!", show_alert=True)
            filename = get_name(bot_id)
            if not filename or not os.path.exists(filename):
                return bot.send_message(caller_id, "❌ Dosya bulunamadı.")
            auto_restart[bot_id] = True
            run_bot_with_log(bot_id, filename, owner_id=owner_id)
            bot.send_message(caller_id, f"▶️ {filename} baslatiliyor...\n\nBot kapanirsa otomatik yeniden baslatilacak.")

        # --- STOP ---
        elif action == "stop":
            auto_restart[bot_id] = False
            time.sleep(0.5)
            _stop_process(bot_id)
            bot.send_message(caller_id, "Bot durduruldu.")

        # --- RESTART ---
        elif action == "restart":
            auto_restart[bot_id] = False
            time.sleep(0.5)
            _stop_process(bot_id)
            time.sleep(1)
            filename = get_name(bot_id)
            if not filename or not os.path.exists(filename):
                return bot.send_message(caller_id, "❌ Dosya bulunamadı.")
            auto_restart[bot_id] = True
            run_bot_with_log(bot_id, filename, owner_id=owner_id)
            bot.send_message(caller_id, f"Bot yeniden baslatildi: {filename}")

        # --- DELETE ---
        elif action == "delete":
            _stop_process(bot_id)
            with db_lock:
                sql.execute("SELECT bot_name FROM bots WHERE id=?", (bot_id,))
                row = sql.fetchone()
            if row:
                fn = row[0]
                if os.path.exists(fn):
                    os.remove(fn)
            with db_lock:
                sql.execute("DELETE FROM bots WHERE id=?", (bot_id,))
                db.commit()
            bot_logs.pop(bot_id, None)
            bot.send_message(caller_id, "🗑 Dosya silindi.")

        # --- LOG ---
        elif action == "log":
            logs = bot_logs.get(bot_id, [])
            if not logs:
                bot.send_message(caller_id, "📄 Log henüz yok.")
            else:
                chunk = "\n".join(logs[-50:])
                for i in range(0, len(chunk), 4000):
                    bot.send_message(caller_id, f"```\n{chunk[i:i+4000]}\n```", parse_mode="Markdown")

        # --- CANLI LOG ---
        elif action == "livelog":
            logs = bot_logs.get(bot_id, [])
            running = bot_id in running_processes
            status_str = "🟢 Çalışıyor" if running else "🔴 Durdu"
            uptime_str = get_uptime(bot_id) if running else "—"
            last_lines = logs[-20:] if logs else ["Log yok"]
            text = (f"📋 *Canlı Log — Bot #{bot_id}*\n"
                    f"📊 {status_str} | ⏱ {uptime_str}\n\n"
                    f"```\n" + "\n".join(last_lines) + "\n```")
            bot.send_message(caller_id, text, parse_mode="Markdown")

# ================= ⭐ PREMIUM AL (TELEGRAM STARS) =================
@bot.message_handler(func=lambda m: m.text == "⭐ Premium Al")
def premium_buy(message):
    uid = message.from_user.id
    with db_lock:
        sql.execute("SELECT premium FROM users WHERE user_id=?", (uid,))
        row = sql.fetchone()
    if row and row[0] == 1:
        return bot.send_message(uid, "✅ Zaten Premium kullanıcısısınız!\n\n🎉 Sınırsız bot yükleme & çalıştırma hakkınız aktif.")
    bot.send_message(uid,
        f"⭐ *Premium Üyelik*\n\nPremium ile şunları kazanırsınız:\n"
        f"• Sınırsız dosya yükleme (ücretsiz: 3)\n• Öncelikli destek\n• Özel rozet ⭐\n\n"
        f"💰 Fiyat: *{PREMIUM_STARS} Telegram Yıldızı*\n\nAşağıdaki butona basarak ödemeyi tamamlayın:",
        parse_mode="Markdown")
    prices = [types.LabeledPrice(label="Premium Uyelik", amount=PREMIUM_STARS)]
    bot.send_invoice(chat_id=uid, title="VDS Bot Premium",
        description=f"Sinırsiz bot yukleme & calistirma hakki. Tek seferlik {PREMIUM_STARS} yildiz.",
        invoice_payload="premium_stars", provider_token="", currency="XTR",
        prices=prices, start_parameter="premium")

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)

@bot.message_handler(content_types=["successful_payment"])
def payment_done(message):
    uid     = message.from_user.id
    payload = message.successful_payment.invoice_payload
    stars   = message.successful_payment.total_amount
    if payload == "premium_stars":
        with db_lock:
            sql.execute("UPDATE users SET premium=1 WHERE user_id=?", (uid,))
            db.commit()
        bot.send_message(uid,
            f"*Odeme Alindi!*\n\n{stars} yildiz odemeniz basariyla alindi.\n"
            f"Hesabiniz simdi *Premium*!\n\nArtik sinırsiz dosya yukleyebilirsiniz.",
            parse_mode="Markdown", reply_markup=main_menu())
        notify_admins(
            f"*Yeni Satin Alma!*\n\nKullanici: {message.from_user.first_name}\n"
            f"ID: {uid}\n{stars} Yildiz - Premium verildi.", parse_mode="Markdown")

# ================= TERMINAL =================
@bot.message_handler(func=lambda m: m.text == "💻 Terminal" and is_admin(m.from_user.id))
def terminal_prompt(message):
    admin_step[message.from_user.id] = "terminal"
    bot.send_message(message.chat.id,
        "💻 Terminal\n\nCalistirmak istediginiz komutu gonderin.\n"
        "Ornek: ls -la veya df -h\n\nDikkatli kullanin - bu gercek bir terminal!")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "terminal")
def terminal_run(message):
    admin_step.pop(message.from_user.id, None)
    cmd = message.text.strip()
    blocked = ["rm -rf /", "mkfs", "dd if=", ":(){:|:&};:", "shutdown", "reboot", "halt"]
    if any(b in cmd for b in blocked):
        return bot.send_message(message.chat.id, "Bu komut engellenmistir.")
    bot.send_message(message.chat.id, "Calistiriliyor: " + cmd)
    try:
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=30)
        output = result.stdout.strip() or "(Cikti yok)"
        if len(output) > 4000:
            output = output[-4000:]
        bot.send_message(message.chat.id, output)
    except subprocess.TimeoutExpired:
        bot.send_message(message.chat.id, "Komut zaman asimina ugradi (30s).")
    except Exception as e:
        bot.send_message(message.chat.id, "Hata: " + str(e))

# ================= PAKET YUKLE ADMIN =================
@bot.message_handler(func=lambda m: m.text == "📦 Paket Yükle" and is_admin(m.from_user.id))
def admin_pkg_prompt(message):
    admin_step[message.from_user.id] = "admin_pip"
    bot.send_message(message.chat.id, "Paket adini gonderin. Birden fazla icin bosluk birakin: requests flask numpy")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "admin_pip")
def admin_pkg_install(message):
    admin_step.pop(message.from_user.id, None)
    packages = message.text.strip().split()
    for pkg in packages:
        if not re.match(r"^[a-zA-Z0-9_.>=<!\\[\\]-]+$", pkg):
            return bot.send_message(message.chat.id, "Gecersiz paket adi: " + pkg)
    bot.send_message(message.chat.id, "Yukleniyor: " + " ".join(packages))
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "install"] + packages,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        output = result.stdout.strip()
        if len(output) > 4000:
            output = output[-4000:]
        status = "Basarili" if result.returncode == 0 else "Hata"
        bot.send_message(message.chat.id, status + "\n\n" + output)
    except subprocess.TimeoutExpired:
        bot.send_message(message.chat.id, "Yukleme zaman asimina ugradi.")
    except Exception as e:
        bot.send_message(message.chat.id, "Hata: " + str(e))

# ================= DESTEK =================
@bot.message_handler(func=lambda m: m.text == "📞 Destek & İletişim")
def support(message):
    support_wait[message.from_user.id] = True
    bot.send_message(message.chat.id, "✍️ Mesajınızı yazın:")

@bot.message_handler(func=lambda m: m.from_user.id in support_wait)
def support_msg(message):
    support_wait.pop(message.from_user.id, None)
    text = (f"📩 *Destek Mesajı*\n\n👤 {message.from_user.first_name}\n"
            f"🆔 {message.from_user.id}\n\n{message.text}")
    notify_admins(text, parse_mode="Markdown")
    bot.send_message(message.chat.id, "✅ Mesajınız iletildi.")

# ================= RAILWAY KORUMASI =================
def start_polling():
    while True:
        try:
            logging.info("BOT BAŞLATILIYOR...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            logging.error(f"POLLING HATASI: {e}")
            logging.info("10 saniye sonra yeniden başlatılıyor...")
            time.sleep(10)

if __name__ == "__main__":
    print("=" * 40)
    print("  VDS BOT v3.0 — Railway Korumalı")
    print("  Stars Ödeme Sistemi ✅")
    print("  Flood Koruma  ✅")
    print("  Güvenlik Taraması ✅")
    print("  İstatistikler ✅")
    print("  Otomatik Bildirim ✅")
    print("  Restart Butonu ✅")
    print("=" * 40)
    start_polling()
