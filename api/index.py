import io
import logging
import os
import time

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Update, Message, BusinessConnection, BusinessMessagesDeleted,
    BufferedInputFile, LabeledPrice, PreCheckoutQuery
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "@support")

FREE_LIMIT = 100
PRICE_STARS = 100
PERIOD_DAYS = 30
KEEP_DAYS = 30

bot = Bot(TOKEN)
dp = Dispatcher()
app = FastAPI()


# ---------- База данных (Postgres) ----------

async def q(sql, *args, fetch=None):
    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    try:
        if fetch == "one":
            return await conn.fetchrow(sql, *args)
        if fetch == "val":
            return await conn.fetchval(sql, *args)
        await conn.execute(sql, *args)
    finally:
        await conn.close()


_ready = False


async def ensure_schema():
    global _ready
    if _ready:
        return
    await q("""CREATE TABLE IF NOT EXISTS msgs2(
        owner BIGINT, chat_id BIGINT, msg_id BIGINT, sender_id BIGINT,
        sender TEXT, text TEXT, file_id TEXT, ftype TEXT, ts BIGINT,
        PRIMARY KEY(owner, chat_id, msg_id))""")
    await q("""CREATE TABLE IF NOT EXISTS conns(
        conn_id TEXT PRIMARY KEY, owner_chat BIGINT)""")
    await q("""CREATE TABLE IF NOT EXISTS users(
        owner BIGINT PRIMARY KEY, used INT DEFAULT 0,
        paid_until BIGINT DEFAULT 0, warned INT DEFAULT 0)""")
    await q("""CREATE TABLE IF NOT EXISTS payments(
        charge_id TEXT PRIMARY KEY, owner BIGINT, stars INT, ts BIGINT)""")
    await q("DELETE FROM msgs2 WHERE ts<$1", int(time.time()) - KEEP_DAYS * 86400)
    _ready = True


# ---------- Подписка и лимиты ----------

def is_admin(user_id):
    return user_id in ADMIN_IDS


async def get_user(owner):
    await q("INSERT INTO users(owner) VALUES($1) ON CONFLICT DO NOTHING", owner)
    r = await q("SELECT used, paid_until, warned FROM users WHERE owner=$1",
                owner, fetch="one")
    return r["used"], r["paid_until"], r["warned"]


async def send_invoice(owner):
    await bot.send_invoice(
        chat_id=owner,
        title="Подписка на 1 месяц",
        description=f"Доступ к боту на {PERIOD_DAYS} дней без ограничений",
        payload="sub_month",
        currency="XTR",
        prices=[LabeledPrice(label="1 месяц", amount=PRICE_STARS)],
    )


async def gate(owner):
    """True, если можно отправлять уведомление. Считает использование."""
    if is_admin(owner):
        return True
    used, paid_until, warned = await get_user(owner)
    if paid_until > time.time():
        return True
    if used < FREE_LIMIT:
        await q("UPDATE users SET used=used+1 WHERE owner=$1", owner)
        return True
    if not warned:
        await q("UPDATE users SET warned=1 WHERE owner=$1", owner)
        try:
            await bot.send_message(
                owner,
                f"Бесплатные {FREE_LIMIT} уведомлений закончились. "
                f"Чтобы продолжить, оформите подписку ({PRICE_STARS} ⭐ на {PERIOD_DAYS} дней).")
            await send_invoice(owner)
        except Exception as e:
            logging.error("Не удалось отправить счёт: %s", e)
    return False


@dp.message(CommandStart())
async def start(m: Message):
    await get_user(m.from_user.id)
    await m.answer(
        "Бот работает ✅\n"
        "Подключите его: Настройки → Telegram для бизнеса → Чат-боты.\n\n"
        f"Первые {FREE_LIMIT} уведомлений бесплатно, дальше {PRICE_STARS} ⭐ в месяц.\n"
        "/status — ваш остаток, /buy — купить подписку.")


@dp.message(Command("status"))
async def status(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("Вы администратор: доступ безлимитный и бесплатный ✅")
        return
    used, paid_until, _ = await get_user(m.from_user.id)
    if paid_until > time.time():
        date = time.strftime("%d.%m.%Y", time.localtime(paid_until))
        await m.answer(f"Подписка активна до {date} ✅")
    else:
        left = max(0, FREE_LIMIT - used)
        await m.answer(f"Подписки нет. Бесплатных уведомлений осталось: {left} из {FREE_LIMIT}.")


@dp.message(Command("buy"))
async def buy(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("Вам оплата не нужна, вы администратор ✅")
        return
    await send_invoice(m.from_user.id)


@dp.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer(f"По вопросам оплаты и возврата пишите: {SUPPORT_CONTACT}")


@dp.pre_checkout_query()
async def pre_checkout(pq: PreCheckoutQuery):
    await pq.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    p = m.successful_payment
    owner = m.from_user.id
    _, paid_until, _ = await get_user(owner)
    new_until = max(paid_until, int(time.time())) + PERIOD_DAYS * 86400
    await q("UPDATE users SET paid_until=$1, warned=0 WHERE owner=$2", new_until, owner)
    await q("INSERT INTO payments VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            p.telegram_payment_charge_id, owner, p.total_amount, int(time.time()))
    date = time.strftime("%d.%m.%Y", time.localtime(new_until))
    await m.answer(f"Оплата получена, спасибо! Подписка активна до {date} ✅")


# ---------- Админ-команды ----------

@dp.message(Command("grant"))
async def grant(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid, days = command.args.split()
        uid, days = int(uid), int(days)
    except Exception:
        await m.answer("Формат: /grant ID ДНИ\nНапример: /grant 123456789 30")
        return
    _, paid_until, _ = await get_user(uid)
    new_until = max(paid_until, int(time.time())) + days * 86400
    await q("UPDATE users SET paid_until=$1, warned=0 WHERE owner=$2", new_until, uid)
    date = time.strftime("%d.%m.%Y", time.localtime(new_until))
    await m.answer(f"Пользователю {uid} выдана подписка до {date} ✅")
    try:
        await bot.send_message(uid, f"Вам выдана подписка до {date} ✅")
    except Exception:
        pass


@dp.message(Command("revoke"))
async def revoke(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid = int(command.args)
    except Exception:
        await m.answer("Формат: /revoke ID")
        return
    await get_user(uid)
    await q("UPDATE users SET paid_until=0 WHERE owner=$1", uid)
    await m.answer(f"Подписка пользователя {uid} отключена.")


@dp.message(Command("stats"))
async def stats(m: Message):
    if not is_admin(m.from_user.id):
        return
    total = await q("SELECT COUNT(*) FROM users", fetch="val")
    active = await q("SELECT COUNT(*) FROM users WHERE paid_until>$1",
                     int(time.time()), fetch="val")
    r = await q("SELECT COALESCE(SUM(stars),0) AS s, COUNT(*) AS c FROM payments",
                fetch="one")
    await m.answer(
        f"Пользователей: {total}\n"
        f"Активных подписок: {active}\n"
        f"Платежей: {r['c']}, выручка: {r['s']} ⭐")


# ---------- Файлы и владельцы ----------

def extract_file(m: Message):
    if m.photo:      return m.photo[-1].file_id, "photo"
    if m.video:      return m.video.file_id, "video"
    if m.voice:      return m.voice.file_id, "voice"
    if m.video_note: return m.video_note.file_id, "video_note"
    if m.document:   return m.document.file_id, "document"
    return None, None


async def send_file(chat_id, file_id, ftype, caption=None):
    send = {
        "photo": bot.send_photo, "video": bot.send_video,
        "voice": bot.send_voice, "video_note": bot.send_video_note,
        "document": bot.send_document,
    }[ftype]
    ext = {"photo": "jpg", "video": "mp4", "voice": "ogg",
           "video_note": "mp4", "document": "bin"}[ftype]
    try:
        buf = io.BytesIO()
        await bot.download(file_id, destination=buf)
        data = BufferedInputFile(buf.getvalue(), filename=f"file.{ext}")
        kwargs = {} if ftype == "video_note" else {"caption": caption}
        await send(chat_id, data, **kwargs)
    except Exception as e:
        logging.error("Не удалось отправить файл: %s", e)
        await bot.send_message(chat_id, f"⚠️ Не удалось сохранить файл: {e}")


async def owner_of(conn_id):
    v = await q("SELECT owner_chat FROM conns WHERE conn_id=$1", conn_id, fetch="val")
    if v:
        return v
    try:
        c = await bot.get_business_connection(conn_id)
        await q("""INSERT INTO conns VALUES($1,$2)
                   ON CONFLICT (conn_id) DO UPDATE SET owner_chat=EXCLUDED.owner_chat""",
                c.id, c.user_chat_id)
        return c.user_chat_id
    except Exception as e:
        logging.error("Не удалось получить владельца: %s", e)
        return None


# ---------- Бизнес-события ----------

@dp.business_connection()
async def on_connection(c: BusinessConnection):
    await q("""INSERT INTO conns VALUES($1,$2)
               ON CONFLICT (conn_id) DO UPDATE SET owner_chat=EXCLUDED.owner_chat""",
            c.id, c.user_chat_id)
    await get_user(c.user_chat_id)
    try:
        await bot.send_message(c.user_chat_id, "Бот подключён к аккаунту ✅")
    except Exception as e:
        logging.error("Не удалось написать владельцу (нужен /start у бота): %s", e)


@dp.business_message()
async def on_message(m: Message):
    if not m.from_user:
        return
    owner = await owner_of(m.business_connection_id)
    if not owner:
        return
    file_id, ftype = extract_file(m)

    await q("""INSERT INTO msgs2 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT (owner, chat_id, msg_id) DO UPDATE SET
               sender_id=EXCLUDED.sender_id, sender=EXCLUDED.sender,
               text=EXCLUDED.text, file_id=EXCLUDED.file_id,
               ftype=EXCLUDED.ftype, ts=EXCLUDED.ts""",
            owner, m.chat.id, m.message_id, m.from_user.id,
            m.from_user.full_name, m.text or m.caption,
            file_id, ftype, int(time.time()))

    r = m.reply_to_message
    if r and m.from_user.id == owner:
        rid, rtype = extract_file(r)
        if rid and await gate(owner):
            await send_file(owner, rid, rtype,
                            f"💾 Сохранено из чата с {r.from_user.full_name}")


@dp.edited_business_message()
async def on_edit(m: Message):
    owner = await owner_of(m.business_connection_id)
    if not owner:
        return
    old = await q("SELECT text FROM msgs2 WHERE owner=$1 AND chat_id=$2 AND msg_id=$3",
                  owner, m.chat.id, m.message_id, fetch="one")
    if old and m.from_user and m.from_user.id != owner:
        if await gate(owner):
            await bot.send_message(
                owner,
                f"✏️ {m.from_user.full_name} изменил(а) сообщение\n\n"
                f"Было: {old['text']}\nСтало: {m.text or m.caption}")
    await q("UPDATE msgs2 SET text=$1 WHERE owner=$2 AND chat_id=$3 AND msg_id=$4",
            m.text or m.caption, owner, m.chat.id, m.message_id)


@dp.deleted_business_messages()
async def on_delete(d: BusinessMessagesDeleted):
    owner = await owner_of(d.business_connection_id)
    if not owner:
        return
    for mid in d.message_ids:
        row = await q(
            "SELECT sender_id, sender, text, file_id, ftype FROM msgs2 "
            "WHERE owner=$1 AND chat_id=$2 AND msg_id=$3",
            owner, d.chat.id, mid, fetch="one")
        if not row or row["sender_id"] == owner:
            continue
        if not await gate(owner):
            return
        note = f"🗑 {row['sender']} удалил(а) сообщение:\n{row['text'] or ''}"
        if row["file_id"]:
            await send_file(owner, row["file_id"], row["ftype"], note)
        else:
            await bot.send_message(owner, note)


# ---------- Webhook ----------

@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/api/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(None),
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    await ensure_schema()
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
