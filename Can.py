# -*- coding: utf-8 -*-
import telebot
from telebot import types
import sqlite3
import subprocess
import sys
import os
import threading
from flask import Flask
app = Flask('')

@app.route('/')
def home():
    return "Bot Aktif!"

def run_web():    
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

TOKEN = "8113976572:AAHRacWZwOHk42rSn2JaU8WXvhVBWHEgYxs"
ADMIN_ID = 8434939976
bot = telebot.TeleBot(TOKEN)

# ================= DATABASE =================
db = sqlite3.connect("data.db", check_same_thread=False)
sql = db.cursor()

sql.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    premium INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0
)
""")

# ================= HATA DÜZELTİLDİ =================
# BOTS TABLOSU ÖNCE OLUŞTURULDU
sql.execute("""
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    bot_name TEXT,
    running INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending'
)
""")

# Sonra status sütunu kontrolü YAPILDI
sql.execute("PRAGMA table_info(bots)")
columns = [info[1] for info in sql.fetchall()]
if "status" not in columns:
    sql.execute("ALTER TABLE bots ADD COLUMN status TEXT DEFAULT 'pending'")

db.commit()

running_processes = {}
bot_logs = {}
admin_step = {}
support_wait = {}
announce_wait = {}  # <-- Duyuru sistemi için eklendi

# ================= MENÜLER =================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📦 Modül Yükle")
    kb.add("📂 Dosya Yükle")
    kb.add("📂 Dosyalarım")
    kb.add("📞 Destek & İletişim")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("⭐ Premium Ver", "👤 Kullanıcı Yasakla / Aç")
    kb.add("🤖 Aktif Botlar")
    kb.add("⛔ Bot Kapat")
    kb.add("🛑 Tüm Botları Kapat")
    kb.add("📢 Duyuru Gönder")  # <-- Buton eklendi
    kb.add("⬅️ Çıkış")
    return kb

# ================= LOG FONKSİYONU =================
def add_log(bot_id, text):
    if bot_id not in bot_logs:
        bot_logs[bot_id] = []
    bot_logs[bot_id].append(text)

# ================= START (DEĞİŞMEDİ!) =================
@bot.message_handler(commands=["start"])
def start(message):
    u = message.from_user
    uid = u.id

    sql.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    if not sql.fetchone():
        sql.execute("INSERT INTO users (user_id,name) VALUES (?,?)", (uid, u.first_name))
        db.commit()

    sql.execute("SELECT premium,banned FROM users WHERE user_id=?", (uid,))
    premium, banned = sql.fetchone()

    if banned:
        bot.send_message(uid, "🚫 Hesabınız yasaklandı.")
        return

    photos = bot.get_user_profile_photos(uid, limit=1)
    if photos.total_count:
        bot.send_photo(uid, photos.photos[0][0].file_id)

    sql.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (uid,))
    count = sql.fetchone()[0]

    status = "⭐ Premium Kullanıcı" if premium else "🆓 Ücretsiz Kullanıcı"
    limit = "Sınırsız" if premium else "3"

    text = f"""
〽️ Hoş Geldiniz, {u.first_name}!

👤 Durumunuz: {status}
📁 Dosya Sayınız: {count} / {limit}

🤖 Bu bot Python (.py) betiklerini çalıştırmak için tasarlanmıştır.

👇 Butonları kullanın.
"""
    bot.send_message(uid, text, reply_markup=main_menu())

# ================= ADMIN PANEL =================
@bot.message_handler(commands=["adminpanel"])
def adminpanel(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "👑 Admin Panel", reply_markup=admin_menu())

@bot.message_handler(func=lambda m: m.text == "⬅️ Çıkış" and m.from_user.id == ADMIN_ID)
def exit_admin(message):
    bot.send_message(message.chat.id, "Çıkıldı.", reply_markup=main_menu())

# ================= DUYURU SİSTEMİ (YENİ) =================
@bot.message_handler(func=lambda m: m.text == "📢 Duyuru Gönder" and m.from_user.id == ADMIN_ID)
def announce_prompt(message):
    announce_wait[message.from_user.id] = True
    bot.send_message(message.chat.id, "📢 Göndermek istediğiniz duyuruyu yazın:")

@bot.message_handler(func=lambda m: m.from_user.id in announce_wait)
def announce_send(message):
    try:
        del announce_wait[message.from_user.id]
    except:
        pass

    duyuru_text = message.text

    sql.execute("SELECT user_id FROM users")
    rows = sql.fetchall()
    sent = 0
    for (uid,) in rows:
        try:
            bot.send_message(uid, f"📢 *Duyuru*\n\n{duyuru_text}", parse_mode="Markdown")
            sent += 1
        except Exception:
            pass

    bot.send_message(ADMIN_ID, f"📢 Duyuru gönderildi. Toplam gönderim: {sent}")

# ================= PREMIUM VER =================
@bot.message_handler(func=lambda m: m.text == "⭐ Premium Ver" and m.from_user.id == ADMIN_ID)
def premium_prompt(message):
    admin_step[message.from_user.id] = "premium"
    bot.send_message(message.chat.id, "🆔 Kullanıcı ID gir (premium verilecek):")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "premium")
def premium_set(message):
    try:
        uid = int(message.text)
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
    admin_step.clear()

# ================= KULLANICI BAN =================
@bot.message_handler(func=lambda m: m.text == "👤 Kullanıcı Yasakla / Aç" and m.from_user.id == ADMIN_ID)
def ban_prompt(message):
    admin_step[message.from_user.id] = "ban"
    bot.send_message(message.chat.id, "🆔 Kullanıcı ID gönder:")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "ban")
def ban_user(message):
    try:
        uid = int(message.text)
        sql.execute("SELECT banned FROM users WHERE user_id=?", (uid,))
        row = sql.fetchone()
        if not row:
            bot.send_message(message.chat.id, "❌ Kullanıcı yok.")
        else:
            new = 0 if row[0] == 1 else 1
            sql.execute("UPDATE users SET banned=? WHERE user_id=?", (new, uid))
            db.commit()
            bot.send_message(message.chat.id, f"✅ Kullanıcı {'açıldı' if new==0 else 'yasaklandı'}.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata: {e}")
    admin_step.clear()

# ================= AKTİF BOTLAR =================
@bot.message_handler(func=lambda m: m.text == "🤖 Aktif Botlar" and m.from_user.id == ADMIN_ID)
def active_bots(message):
    sql.execute("SELECT id,user_id,bot_name FROM bots WHERE running=1")
    rows = sql.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "Aktif bot yok.")
        return
    text = "🔥 Aktif Botlar:\n\n"
    for r in rows:
        text += f"Bot ID: {r[0]}\nKullanıcı ID: {r[1]}\nDosya: {r[2]}\n\n"
    bot.send_message(message.chat.id, text)

# ================= BOT KAPAT =================
@bot.message_handler(func=lambda m: m.text == "⛔ Bot Kapat" and m.from_user.id == ADMIN_ID)
def stop_bot_prompt(message):
    admin_step[message.from_user.id] = "stopbot_full"
    bot.send_message(message.chat.id, "🆔 Kullanıcı ID ve Dosya Adı girin (örnek: 12345678 dosya.py)")

@bot.message_handler(func=lambda m: admin_step.get(m.from_user.id) == "stopbot_full")
def stop_bot_full(message):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            return bot.send_message(message.chat.id, "❌ Lütfen KullanıcıID ve DosyaAdı şeklinde girin.")
        uid = int(parts[0])
        filename = parts[1]
        sql.execute("SELECT id FROM bots WHERE user_id=? AND bot_name=?", (uid, filename))
        row = sql.fetchone()
        if not row:
            return bot.send_message(message.chat.id, "❌ Bot bulunamadı.")
        bot_id = row[0]
        proc = running_processes.get(bot_id)
        if proc:
            proc.terminate()
            del running_processes[bot_id]
        sql.execute("UPDATE bots SET running=0 WHERE id=?", (bot_id,))
        db.commit()
        add_log(bot_id, "Bot admin tarafından durduruldu ⏸️")
        bot.send_message(message.chat.id, f"✅ {filename} durduruldu.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata: {e}")
    admin_step.clear()

# ================= TÜM BOTLARI KAPAT =================
@bot.message_handler(func=lambda m: m.text == "🛑 Tüm Botları Kapat" and m.from_user.id == ADMIN_ID)
def stop_all(message):
    for p in running_processes.values():
        try:
            p.terminate()
        except:
            pass
    running_processes.clear()
    sql.execute("UPDATE bots SET running=0")
    db.commit()
    bot.send_message(message.chat.id, "✅ Tüm botlar durduruldu.")

# ================= MODÜL YÜKLE =================
@bot.message_handler(func=lambda m: m.text == "📦 Modül Yükle")
def mod_prompt(message):
    msg = bot.send_message(message.chat.id, "📦 pip modül adı gir:")
    bot.register_next_step_handler(msg, mod_install)

def mod_install(message):
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", message.text])
        bot.send_message(message.chat.id, "✅ Modül yüklendi.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Hata:\n{e}")

# ================= DOSYA YÜKLE =================
@bot.message_handler(func=lambda m: m.text == "📂 Dosya Yükle")
def upload_prompt(message):
    bot.send_message(message.chat.id, ".py dosyanızı gönderin")

@bot.message_handler(content_types=["document"])
def upload(message):
    if not message.document.file_name.endswith(".py"):
        return bot.reply_to(message, "❌ Sadece .py dosya kabul edilir")

    uid = message.from_user.id
    sql.execute("SELECT premium FROM users WHERE user_id=?", (uid,))
    premium = sql.fetchone()[0]
    sql.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (uid,))
    c = sql.fetchone()[0]

    if not premium and c >= 3:
        return bot.reply_to(message, "❌ Limit dolu. Premium alın.")

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

    sql.execute("INSERT INTO bots (user_id, bot_name, status) VALUES (?, ?, ?)", (uid, filename, 'pending'))
    db.commit()
    bot_id = sql.lastrowid

    bot.reply_to(message, "✅ Dosya yüklendi. Admin onayı bekleniyor.")

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Onayla", callback_data=f"approve_{bot_id}"),
        types.InlineKeyboardButton("❌ Reddet", callback_data=f"reject_{bot_id}")
    )
    with open(filename, "rb") as f:
        bot.send_document(
            ADMIN_ID,
            f,
            caption=f"📂 Yeni Dosya Yüklendi\n👤 Kullanıcı: {message.from_user.first_name}\n🆔 {uid}\n📄 Dosya: {filename}",
            reply_markup=kb
        )

# ================= DOSYALARIM =================
@bot.message_handler(func=lambda m: m.text == "📂 Dosyalarım")
def files(message):
    uid = message.from_user.id
    sql.execute("SELECT id, bot_name, running, status FROM bots WHERE user_id=?", (uid,))
    rows = sql.fetchall()
    if not rows:
        return bot.send_message(uid, "📂 Dosya yok.")

    for bot_id, bot_name, running, status in rows:
        if status == 'pending':
            durum = "⏳ Onay Bekliyor"
        elif status == 'rejected':
            durum = "❌ Reddedildi"
        else:
            durum = "Çalışıyor ✅" if running else "Duruyor ⏸️"

        kb = types.InlineKeyboardMarkup()
        if status == 'approved':
            kb.row(
                types.InlineKeyboardButton("▶️ Başlat", callback_data=f"start_{bot_id}"),
                types.InlineKeyboardButton("⛔ Durdur", callback_data=f"stop_{bot_id}")
            )
            kb.row(
                types.InlineKeyboardButton("❌ Sil", callback_data=f"delete_{bot_id}"),
                types.InlineKeyboardButton("📄 Log", callback_data=f"log_{bot_id}")
            )
        else:
            kb.row(
                types.InlineKeyboardButton("ℹ️ Onay Bekliyor", callback_data=f"info_{bot_id}"),
                types.InlineKeyboardButton("❌ Sil", callback_data=f"delete_{bot_id}")
            )
        bot.send_message(uid, f"📄 {bot_name}\n🆔 ID: {bot_id}\nDurum: {durum}", reply_markup=kb)

# ================= CALLBACK =================
def run_bot_with_log(bot_id, filename):
    def target():
        try:
            proc = subprocess.Popen(
                [sys.executable, filename],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            running_processes[bot_id] = proc
            sql.execute("UPDATE bots SET running=1, status='approved' WHERE id=?", (bot_id,))
            db.commit()
            add_log(bot_id, "Bot başlatıldı ✅")
            for line in proc.stdout:
                add_log(bot_id, line.strip())
            for line in proc.stderr:
                add_log(bot_id, line.strip())
        except ModuleNotFoundError as e:
            missing_module = str(e).split("'")[1]
            add_log(bot_id, f"Başlatılamadı ❌ Eksik modül: {missing_module}")
        except Exception as e:
            add_log(bot_id, f"Hata: {e}")
    threading.Thread(target=target, daemon=True).start()

def get_name(bot_id):
    sql.execute("SELECT bot_name FROM bots WHERE id=?", (bot_id,))
    result = sql.fetchone()
    return result[0] if result else None

@bot.callback_query_handler(func=lambda c: True)
def cb(call):
    try:
        action, bot_id_str = call.data.split("_", 1)
        bot_id = int(bot_id_str)
    except:
        return

    if action == "approve":
        if call.from_user.id != ADMIN_ID:
            return
        sql.execute("SELECT user_id, bot_name FROM bots WHERE id=? AND status='pending'", (bot_id,))
        row = sql.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "Bu işlem zaten tamamlanmış.", show_alert=True)
            return
        uid, filename = row
        sql.execute("UPDATE bots SET status='approved' WHERE id=?", (bot_id,))
        db.commit()
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="✅ DOSYA ONAYLANDI\n" + call.message.caption.replace("📂 Yeni Dosya Yüklendi", "")
        )
        bot.send_message(uid, f"✅ Dosyanız onaylandı ve çalıştırılmaya hazır: `{filename}`", parse_mode="Markdown")

    elif action == "reject":
        if call.from_user.id != ADMIN_ID:
            return
        sql.execute("SELECT user_id, bot_name FROM bots WHERE id=? AND status='pending'", (bot_id,))
        row = sql.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "Bu işlem zaten tamamlanmış.", show_alert=True)
            return
        uid, filename = row
        if os.path.exists(filename):
            os.remove(filename)
        sql.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        db.commit()
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption="❌ DOSYA REDDEDİLDİ\n" + call.message.caption.replace("📂 Yeni Dosya Yüklendi", "")
        )
        bot.send_message(uid, f"❌ Dosyanız reddedildi: `{filename}`", parse_mode="Markdown")

    elif action == "info":
        bot.answer_callback_query(call.id, "Bu dosya admin onayı bekliyor.", show_alert=True)

    else:
        sql.execute("SELECT status FROM bots WHERE id=?", (bot_id,))
        res = sql.fetchone()
        if not res:
            bot.answer_callback_query(call.id, "Dosya bulunamadı.", show_alert=True)
            return
        status = res[0]

        if action in ("start", "stop") and status != "approved":
            bot.answer_callback_query(call.id, "❌ Bu dosya admin tarafından onaylanmadı.", show_alert=True)
            return

        if action == "start":
            filename = get_name(bot_id)
            if not filename or not os.path.exists(filename):
                bot.send_message(call.from_user.id, "❌ Dosya bulunamadı.")
                return
            run_bot_with_log(bot_id, filename)
            bot.send_message(call.from_user.id, "✅ Bot başlatıldı veya başlatılıyor. Hatalar log’a düşecektir.")

        elif action == "stop":
            p = running_processes.get(bot_id)
            if p:
                p.terminate()
                del running_processes[bot_id]
            sql.execute("UPDATE bots SET running=0 WHERE id=?", (bot_id,))
            db.commit()
            bot.send_message(call.from_user.id, "✅ Bot durduruldu.")
            add_log(bot_id, "Bot durduruldu ⏸️")

        elif action == "delete":
            p = running_processes.get(bot_id)
            if p:
                p.terminate()
                del running_processes[bot_id]
            sql.execute("SELECT bot_name FROM bots WHERE id=?", (bot_id,))
            row = sql.fetchone()
            if row:
                filename = row[0]
                if os.path.exists(filename):
                    os.remove(filename)
            sql.execute("DELETE FROM bots WHERE id=?", (bot_id,))
            db.commit()
            bot.send_message(call.from_user.id, "✅ Dosya silindi.")
            add_log(bot_id, "Dosya silindi ❌")

        elif action == "log":
            logs = bot_logs.get(bot_id, [])
            if not logs:
                bot.send_message(call.from_user.id, "📄 Log bulunamadı.")
            else:
                bot.send_message(call.from_user.id, "📄 Loglar:\n" + "\n".join(logs[-50:]))

# ================= DESTEK =================
@bot.message_handler(func=lambda m: m.text == "📞 Destek & İletişim")
def support(message):
    support_wait[message.from_user.id] = True
    bot.send_message(message.chat.id, "✍️ Lütfen mesajınızı yazın. Bu mesaj doğrudan admine iletilecek.")

@bot.message_handler(func=lambda m: m.from_user.id in support_wait)
def support_msg(message):
    del support_wait[message.from_user.id]
    bot.send_message(
        ADMIN_ID,
        f"📩 *Destek Mesajı*\n\n👤 {message.from_user.first_name}\n🆔 {message.from_user.id}\n\n{message.text}",
        parse_mode="Markdown"
    )
    bot.send_message(message.chat.id, "✅ Mesajınız iletildi.")

# ================= RUN =================
if __name__ == "__main__":
    keep_alive() # Web sunucusunu başlatır
    print("BOT ÇALIŞIYOR...")
    bot.infinity_polling()
