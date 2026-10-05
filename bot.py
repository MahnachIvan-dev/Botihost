"""
🤖 BotHost v9.3 — Subscriptions Edition
────────────────────────────────────────────────────────────────
Что изменилось по сравнению с v8.0:

✅ ГЛАВНЫЙ ФИКС: купленный слот больше не «исчезает».
   • Подписка хранится в отдельной таблице subscriptions.
   • Продление СТАКАЕТСЯ: новый срок прибавляется к текущей дате окончания,
     остаток дней не сгорает.
   • «➕ Ещё один слот» — отдельная покупка, если нужно запускать больше ботов
     (раньше вторая покупка молча продлевала первую, и второй бот был невозможен).
   • Старые записи из таблицы slots автоматически переносятся (миграция).
   • Льготный период 24 ч после истечения — боты не падают в ту же секунду.

✅ Напоминания: за 3 дня, за 1 день и после истечения — с кнопками выбора
   тарифа продления прямо в уведомлении (в 1 клик).

✅ Два типа тарифа:
   🧩 «Слот на 1 бота»    — 1 подписка = 1 работающий бот.
   🚀 «Хостинг без лимита» — ботов сколько угодно, срок общий.

✅ Удобства от себя:
   • «📦 Моя подписка»: прогресс-бар, сколько слотов занято, дата окончания.
   • Динамическая кнопка «⚠️ Продлить подписку» в главном меню.
   • Админ: «📦 Подписки» (список/отзыв/+7 и +30 дней) и «🎁 Выдать подписку».
   • Промокод может выдавать любой тариф, включая хостинг.
   • Автоочистка мусорных записей и напоминания без спама (флаги в БД).
   • Если бот остановлен из-за истёкшей подписки — придёт уведомление
     с кнопками продления, а файлы и переменные останутся на месте.

✅ Для владельца:
   • 🎫 «Безлимит-карта» — персональный безлимит на ботов, выдаётся владельцем
     конкретному пользователю (бессрочно или на срок). НЕ даёт доступ к админке.
   • 👥 «Пользователи» — постраничный список всех аккаунтов: подписки, число
     ботов, флаги (бан/админ/безлимит) и карточка юзера со списком его ботов,
     баном, ЛС, выдачей подписки и безлимита прямо из карточки.
   • 🔍 Поиск пользователя по ID или @username.

⚠️ Обязательно подключи Railway Volume на DATA_DIR (/app/data), иначе при
   редеплое вместе с файлами пропадут и подписки. В логе при старте есть подсказка.

🔗 Кассир: по умолчанию режим «auto» — если задан CASHIER_TOKEN, кассир поднимается
   внутри этого же сервиса (как в v8). Перед запуском проверяется занятость токена,
   поэтому забытый второй экземпляр больше не даёт конфликтов.
   Кассир отдельным сервисом → поставь INTERNAL_CASHIER=0 (и убери CASHIER_TOKEN отсюда).
"""

import os
import re
import sys
import asyncio
import subprocess
import sqlite3
import logging
import signal
import html
import zipfile
import shutil
import time
import atexit
import threading
import hmac
import hashlib
import json
import importlib
import importlib.util
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.exceptions import (TelegramConflictError, TelegramUnauthorizedError, TelegramRetryAfter,
                                TelegramBadRequest)
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

try:
    from groq import AsyncGroq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════
# 🔧 КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "8269807543"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "ivan_unreal")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

VERIFIER_BOT_USERNAME = os.environ.get("VERIFIER_BOT", "BotHostoplatiBot").lstrip("@")
# ID закрытого канала, где сидят оба бота (начинается с -100)
SYNC_CHANNEL_ID = int(os.environ.get("SYNC_CHANNEL_ID", "0"))
SYNC_SECRET = os.environ.get("SYNC_SECRET", "").strip()
FILE_STORAGE_CHANNEL_ID = int(os.environ.get("BOT_FILES_STORAGE_CHANNEL_ID", os.environ.get("STORAGE_CHANNEL_ID", "0")))

# ⏳ Подписки
GRACE_HOURS = int(os.environ.get("GRACE_HOURS", "24"))          # льготный период после истечения
NOTIFY_BEFORE_DAYS = float(os.environ.get("NOTIFY_BEFORE_DAYS", "3"))  # за сколько дней предупреждать
SUB_CHECK_INTERVAL = int(os.environ.get("SUB_CHECK_INTERVAL", "600"))  # как часто проверять подписки (сек)

# 💳 Внутренний кассир. Режимы:
#   "auto" (по умолчанию) — если задан CASHIER_TOKEN, кассир поднимается внутри этого же
#                           сервиса (как в v8). Перед запуском проверяем, не опрашивает ли
#                           токен кто-то ещё: занят → внутренний не стартует, чтобы не было
#                           TelegramConflictError.
#   "on"   — включать обязательно (токен занят → пишем ошибку и не стартуем).
#   "off"  — не включать (режим двух сервисов: кассир живёт отдельным сервисом).
_cashier_env = os.environ.get("INTERNAL_CASHIER", "auto").strip().lower()
if _cashier_env in ("", "auto", "-1"):
    INTERNAL_CASHIER_MODE = "auto"
elif _cashier_env in ("1", "true", "yes", "on", "вкл"):
    INTERNAL_CASHIER_MODE = "on"
elif _cashier_env in ("0", "false", "no", "off", "выкл"):
    INTERNAL_CASHIER_MODE = "off"
else:
    INTERNAL_CASHIER_MODE = "auto"
INTERNAL_CASHIER = INTERNAL_CASHIER_MODE  # для обратной совместимости и логов
BOTHOST_VERSION = "9.6"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
BOTS_DIR = DATA_DIR / "bots"
DB_PATH = DATA_DIR / "bot.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BOTS_DIR.mkdir(parents=True, exist_ok=True)

# ───────────────────────────────────────────────────────────────
# 💎 КАТАЛОГ ТАРИФОВ
# kind: "bot"  — слот на одного бота
#       "host" — хостинг: ботов без лимита
#
# Цены можно менять БЕЗ правки кода — переменными окружения (см. apply_plan_overrides):
#   PRICE_HOST_MONTH=199      цена тарифа host_month в Stars
#   DAYS_HOST_MONTH=45        срок в днях
#   PRICE_WEEK=20  DAYS_2WEEKS=10
#   PLANS_JSON={"host_month":{"stars":199,"days":45}}
# ⚠️ Имена переменных должны совпадать в bot.py и cashier.py, иначе BotHost покажет
#    одну сумму, а кассир потребует другую.
# ───────────────────────────────────────────────────────────────

BASE_PLANS = {
    # 🧩 Слот на 1 бота
    "week":   {"name": "Неделя",   "stars": 15, "days": 7,  "kind": "bot",  "emoji": "📅"},
    "2weeks": {"name": "2 недели", "stars": 25, "days": 14, "kind": "bot",  "emoji": "🗓"},
    "month":  {"name": "Месяц",    "stars": 50, "days": 30, "kind": "bot",  "emoji": "💎"},
    # 🚀 Хостинг без лимита ботов
    "host_week":   {"name": "Хостинг · Неделя",   "stars": 50,  "days": 7,  "kind": "host", "emoji": "🚀"},
    "host_2weeks": {"name": "Хостинг · 2 недели", "stars": 100,  "days": 14, "kind": "host", "emoji": "🛰"},
    "host_month":  {"name": "Хостинг · Месяц",    "stars": 100, "days": 30, "kind": "host", "emoji": "🌌"},
}

KINDS = {
    "bot": {
        "name": "Слот на 1 бота",
        "emoji": "🧩",
        "hint": "Одна подписка = один работающий бот. Удалил бота — слот освободился.",
    },
    "host": {
        "name": "Хостинг без лимита ботов",
        "emoji": "🚀",
        "hint": "Загружай сколько угодно ботов, пока активна подписка. Выгодно от 3 ботов.",
    },
}

BOT_START_TIME = time.time()
logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("BotHost")


def plan_env_suffix(pid):
    """host_month → HOST_MONTH, 2weeks → 2WEEKS."""
    return re.sub(r"[^A-Za-z0-9]", "_", pid).upper()


def apply_plan_overrides(plans, env=None):
    """Применяет цены/сроки из переменных окружения к таблице тарифов.

    Такая же функция есть в cashier.py с тем же набором переменных — так цены
    невозможно рассогласовать между сервисами.
    """
    log = logging.getLogger("BotHost")
    env = os.environ if env is None else env

    def read_int(name, raw, default):
        raw = str(raw).strip().replace(",", ".")
        if not raw:
            return default
        try:
            return max(1, int(float(raw)))
        except ValueError:
            log.warning("%s=%r — не число, оставляю %s", name, raw, default)
            return default

    for pid, p in plans.items():
        sfx = plan_env_suffix(pid)
        p["stars"] = read_int(f"PRICE_{sfx}", env.get(f"PRICE_{sfx}", ""), p["stars"])
        p["days"] = read_int(f"DAYS_{sfx}", env.get(f"DAYS_{sfx}", ""), p["days"])

    raw_json = str(env.get("PLANS_JSON", "") or env.get("CASHIER_PLANS_JSON", "")).strip()
    if raw_json:
        try:
            patch = json.loads(raw_json)
            if not isinstance(patch, dict):
                raise ValueError("ожидался объект вида {\"host_month\": {\"stars\": 199}}")
            for pid, fields in patch.items():
                if pid not in plans or not isinstance(fields, dict):
                    log.warning("PLANS_JSON: пропускаю %r", pid)
                    continue
                for field in ("name", "stars", "days", "kind", "emoji"):
                    if field in fields:
                        plans[pid][field] = fields[field]
                plans[pid]["stars"] = read_int(f"PLANS_JSON[{pid}].stars", plans[pid]["stars"], plans[pid]["stars"])
                plans[pid]["days"] = read_int(f"PLANS_JSON[{pid}].days", plans[pid]["days"], plans[pid]["days"])
                if plans[pid]["kind"] not in ("bot", "host"):
                    plans[pid]["kind"] = "bot"
        except Exception as e:
            log.error("PLANS_JSON не разобран (%s) — беру тарифы по умолчанию", e)
    return plans


PLANS = apply_plan_overrides({pid: dict(p) for pid, p in BASE_PLANS.items()})

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
running_bots: Dict[int, subprocess.Popen] = {}
start_locks: Dict[int, asyncio.Lock] = {}

groq_client = None
if GROQ_AVAILABLE and GROQ_API_KEY:
    try:
        groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    except Exception as e:
        logger.error(f"⚠️ Ошибка ИИ: {e}")

# ═══════════════════════════════════════════════════════════════
# 💾 БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════

def _db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _db_retry(fn, attempts=10):
    """Операция с короткими повторными попытками при блокировке БД."""
    last = None
    for i in range(attempts):
        conn = None
        try:
            conn = _db_connect()
            result = fn(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            last = e
            text_err = str(e).lower()
            if "locked" not in text_err and "busy" not in text_err:
                raise
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            time.sleep(min(0.10 * (i + 1), 0.8))
        finally:
            if conn:
                conn.close()
    raise last


def _db_read(fn):
    conn = _db_connect()
    try:
        return fn(conn)
    finally:
        conn.close()


# Явный порядок колонок подписок (не завязываемся на порядок в старых БД)
SUB_COLS = "id,user_id,kind,plan,expires_at,period_days,reminded_3d,reminded_1d,expired_notified,created_at"


def init_db():
    def setup(conn):
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS slots (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, expires_at TEXT, created_at TEXT, gift_id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'bot', plan TEXT NOT NULL, expires_at TEXT NOT NULL, period_days INTEGER DEFAULT 0, reminded_3d INTEGER DEFAULT 0, reminded_1d INTEGER DEFAULT 0, expired_notified INTEGER DEFAULT 0, created_at TEXT NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id, expires_at)")
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS vip (user_id INTEGER PRIMARY KEY, note TEXT DEFAULT '', granted_by INTEGER, created_at TEXT NOT NULL, expires_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS bots (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, filename TEXT, bot_token TEXT, status TEXT DEFAULT 'stopped', created_at TEXT, is_frozen INTEGER DEFAULT 0, entry_point TEXT DEFAULT 'user_bot.py', auto_restart INTEGER DEFAULT 0, env_vars TEXT DEFAULT '{}')")
        c.execute("CREATE TABLE IF NOT EXISTS payment_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, full_name TEXT, plan TEXT, status TEXT DEFAULT 'pending', created_at TEXT, processed_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS promocodes (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, plan TEXT, uses_left INTEGER, created_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS used_promos (user_id INTEGER, promo_id INTEGER, UNIQUE(user_id, promo_id))")
        c.execute("CREATE TABLE IF NOT EXISTS auto_grants (grant_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS bot_archives (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, message_id INTEGER NOT NULL, file_id TEXT NOT NULL, filename TEXT NOT NULL, created_at TEXT NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bot_archives_bot ON bot_archives(bot_id, id DESC)")

        cols = {row[1] for row in c.execute("PRAGMA table_info(bots)").fetchall()}
        if "entry_point" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN entry_point TEXT DEFAULT 'user_bot.py'")
        if "auto_restart" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN auto_restart INTEGER DEFAULT 0")
        if "env_vars" not in cols:
            c.execute("ALTER TABLE bots ADD COLUMN env_vars TEXT DEFAULT '{}'")

        pcols = {row[1] for row in c.execute("PRAGMA table_info(payment_requests)").fetchall()}
        if "mode" not in pcols:
            c.execute("ALTER TABLE payment_requests ADD COLUMN mode TEXT DEFAULT 'extend'")

        # Миграция старой модели слотов в подписки (однократно).
        migrated = c.execute("SELECT v FROM meta WHERE k='slots_migrated'").fetchone()
        if not migrated:
            now = datetime.now()
            rows = c.execute("SELECT user_id, plan, expires_at FROM slots WHERE expires_at > ?", (now.isoformat(),)).fetchall()
            moved = 0
            for uid, plan, expires_at in rows:
                if plan not in PLANS:
                    continue
                p = PLANS[plan]
                c.execute(
                    "INSERT INTO subscriptions(user_id,kind,plan,expires_at,period_days,created_at) VALUES(?,?,?,?,?,?)",
                    (uid, p["kind"], plan, expires_at, p["days"], now.isoformat()),
                )
                moved += 1
            c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('slots_migrated',?)", (now.isoformat(),))
            if moved:
                logger.info("♻️ Миграция слотов в подписки: перенесено %s активных записей", moved)

    _db_retry(setup, attempts=20)


def get_db():
    return _db_connect()


def swap_database_file(tmp_db: Path):
    """Безопасно подменяет файл базы загруженным бэкапом.

    1. проверяет, что файл — настоящая SQLite-база;
    2. сохраняет страховочную копию текущей базы;
    3. удаляет -wal/-shm старой базы (иначе SQLite может прочитать старый журнал
       и «не увидеть» новый файл);
    4. подменяет файл и прогоняет init_db() (миграции).

    Возвращает имя страховочной копии или "".
    """
    chk = sqlite3.connect(tmp_db)
    try:
        chk.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        chk.close()
    safety_name = ""
    if DB_PATH.exists():
        safety_path = DATA_DIR / f"backup_before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(DB_PATH, safety_path)
        safety_name = safety_path.name
    for sfx in ("-wal", "-shm"):
        Path(str(DB_PATH) + sfx).unlink(missing_ok=True)
    shutil.move(str(tmp_db), str(DB_PATH))
    init_db()
    return safety_name


# ─────────────── ПОЛЬЗОВАТЕЛИ ───────────────

def create_user(uid, uname, fname=""):
    _db_retry(lambda conn: conn.execute(
        """INSERT INTO users (user_id, username, full_name, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
        (uid, uname, fname, datetime.now().isoformat())))


def create_user_soft(uid, uname=""):
    """Создаёт пользователя, НЕ затирая нормальный @username заглушкой.

    Кассир присылает username в подписанном запросе, но если у человека его нет,
    приходит "-". Раньше это значение перезаписывало настоящий username в базе,
    и в админском списке пользователей появлялось «@-».
    """
    now = datetime.now().isoformat()
    uname = (uname or "").strip().lstrip("@")
    valid = bool(re.fullmatch(r"[A-Za-z0-9_]{4,32}", uname))

    def op(conn):
        row = conn.execute("SELECT username FROM users WHERE user_id = ?", (uid,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (user_id, username, full_name, created_at) VALUES (?, ?, '', ?)",
                         (uid, uname if valid else "", now))
        elif valid and not (row[0] or "").strip().lstrip("@"):
            conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (uname, uid))
    _db_retry(op)


def get_user(uid):
    return _db_read(lambda conn: conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone())


def get_all_users():
    return _db_read(lambda conn: conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall())


def find_user_by_username(uname):
    row = _db_read(lambda conn: conn.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (uname.lstrip("@").lower(),)).fetchone())
    return row[0] if row else None


def is_user_banned(uid):
    if uid == OWNER_ID:
        return False
    row = get_user(uid)
    return bool(row and row[4])


def ban_user(uid):
    _db_retry(lambda conn: conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (uid,)))


def unban_user(uid):
    _db_retry(lambda conn: conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (uid,)))


def is_admin(uid):
    if uid == OWNER_ID:
        return True
    row = get_user(uid)
    return bool(row and row[3])


def add_admin(uid):
    def op(conn):
        conn.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, datetime.now().isoformat()))
        conn.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (uid,))
    _db_retry(op)


def remove_admin(uid):
    _db_retry(lambda conn: conn.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (uid,)))


def get_all_admins():
    return _db_read(lambda conn: conn.execute("SELECT user_id, username, full_name FROM users WHERE is_admin = 1 AND user_id != ?", (OWNER_ID,)).fetchall())


# ═══════════════════════════════════════════════════════════════
# 🎫 БЕЗЛИМИТ-КАРТЫ ОТ ВЛАДЕЛЬЦА
# Персональный безлимит для конкретного пользователя.
# В отличие от админки НЕ даёт доступ к панели управления.
# ═══════════════════════════════════════════════════════════════

def get_vip(uid):
    return _db_read(lambda conn: conn.execute("SELECT * FROM vip WHERE user_id = ?", (uid,)).fetchone())


def vip_is_alive(row):
    """row: (user_id, note, granted_by, created_at, expires_at). expires_at NULL = бессрочно."""
    if not row:
        return False
    exp = row[4]
    if not exp:
        return True
    try:
        return datetime.fromisoformat(exp) > datetime.now()
    except Exception:
        return True


def is_vip(uid):
    return vip_is_alive(get_vip(uid))


def grant_vip(uid, days=None, granted_by=None):
    """Выдать безлимит-карту. days=None → бессрочно. Возвращает дату окончания или None."""
    now = datetime.now()
    exp = (now + timedelta(days=days)).isoformat() if days else None
    def op(conn):
        conn.execute("INSERT INTO users(user_id, created_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING", (uid, now.isoformat()))
        conn.execute(
            """INSERT INTO vip(user_id, note, granted_by, created_at, expires_at) VALUES(?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET granted_by=excluded.granted_by,
                                                  created_at=excluded.created_at,
                                                  expires_at=excluded.expires_at""",
            (uid, "", granted_by, now.isoformat(), exp))
    _db_retry(op)
    return exp


def revoke_vip(uid):
    return _db_retry(lambda conn: conn.execute("DELETE FROM vip WHERE user_id = ?", (uid,)).rowcount)


def get_all_vips(active_only=True):
    def read(conn):
        if active_only:
            return conn.execute("SELECT * FROM vip WHERE expires_at IS NULL OR expires_at > ? ORDER BY created_at DESC",
                                (datetime.now().isoformat(),)).fetchall()
        return conn.execute("SELECT * FROM vip ORDER BY created_at DESC").fetchall()
    return _db_read(read)


def vip_line(uid):
    row = get_vip(uid)
    if not vip_is_alive(row):
        return ""
    if not row[4]:
        return "🎫 безлимит · бессрочно"
    return f"🎫 безлимит · до {fmt_dt(datetime.fromisoformat(row[4]))}"


# ═══════════════════════════════════════════════════════════════
# 📦 ПОДПИСКИ (замена «сгорающих слотов»)
# ═══════════════════════════════════════════════════════════════

def fmt_dt(dt):
    return dt.strftime("%d.%m.%Y %H:%M")


def human_left(expires_at):
    """Сколько осталось до даты окончания — человеческим языком."""
    try:
        exp = datetime.fromisoformat(expires_at)
    except Exception:
        return "—"
    sec = (exp - datetime.now()).total_seconds()
    if sec <= 0:
        return "истекла"
    d = int(sec // 86400)
    h = int((sec % 86400) // 3600)
    m = int((sec % 3600) // 60)
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def bar(ratio, width=10):
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * width))
    return "▰" * filled + "▱" * (width - filled)


def get_active_subs(uid, include_grace=True):
    """Активные подписки. include_grace — учитывать льготный период после истечения."""
    limit = datetime.now() - timedelta(hours=GRACE_HOURS if include_grace else 0)
    return _db_read(lambda conn: conn.execute(
        f"SELECT {SUB_COLS} FROM subscriptions WHERE user_id = ? AND expires_at > ? ORDER BY expires_at ASC",
        (uid, limit.isoformat())).fetchall())


def add_subscription(uid, pid, grant_id=None, mode="extend"):
    """Создаёт или ПРОДЛЕВАЕТ подписку.

    mode="extend" — продление: срок прибавляется к дате окончания активной
                    подписки того же типа, остаток дней НЕ сгорает.
    mode="new"    — дополнительный слот: создаётся отдельная подписка
                    (нужно, чтобы запускать больше ботов одновременно).
    Для kind="host" всегда действует extend — хостинг один на пользователя.

    Возвращает dict: {'sub_id', 'expires_at', 'extended', 'kind', 'mode'}
    или None, если grant_id уже был использован.
    """
    if pid not in PLANS:
        raise ValueError(f"Неизвестный тариф: {pid}")
    p = PLANS[pid]
    kind, days = p["kind"], p["days"]
    if kind == "host":
        mode = "extend"
    now = datetime.now()

    def op(conn):
        if grant_id:
            if conn.execute("SELECT 1 FROM auto_grants WHERE grant_id = ?", (grant_id,)).fetchone():
                return None
            conn.execute("INSERT INTO auto_grants(grant_id,user_id,plan,created_at) VALUES(?,?,?,?)",
                         (grant_id, uid, pid, now.isoformat()))
        conn.execute("INSERT INTO users(user_id,username,full_name,created_at) VALUES(?,'','',?) ON CONFLICT(user_id) DO NOTHING",
                     (uid, now.isoformat()))
        row = None
        if mode == "extend":
            # Продлеваем ту подписку, которая истекает раньше всех.
            row = conn.execute(
                "SELECT id, expires_at FROM subscriptions WHERE user_id=? AND kind=? AND expires_at > ? ORDER BY expires_at ASC LIMIT 1",
                (uid, kind, now.isoformat())).fetchone()
        if row:
            base = max(datetime.fromisoformat(row[1]), now)
            exp = base + timedelta(days=days)
            conn.execute(
                "UPDATE subscriptions SET expires_at=?, plan=?, period_days=?, reminded_3d=0, reminded_1d=0, expired_notified=0 WHERE id=?",
                (exp.isoformat(), pid, days, row[0]))
            return {"sub_id": row[0], "expires_at": exp, "extended": True, "kind": kind, "plan": pid, "mode": "extend"}
        exp = now + timedelta(days=days)
        cur = conn.execute(
            "INSERT INTO subscriptions(user_id,kind,plan,expires_at,period_days,created_at) VALUES(?,?,?,?,?,?)",
            (uid, kind, pid, exp.isoformat(), days, now.isoformat()))
        return {"sub_id": cur.lastrowid, "expires_at": exp, "extended": False, "kind": kind, "plan": pid, "mode": mode}

    return _db_retry(op)


def get_limits(uid):
    """Лимиты пользователя.

    unlimited=True  — безлимит (админ, безлимит-карта или хостинг)
    slots=<N>       — доступно N одновременно работающих ботов
    until           — крайняя дата окончания (None = бессрочно)
    vip             — выдана безлимит-карта владельцем
    """
    if is_admin(uid):
        return {"unlimited": True, "slots": None, "until": None, "host": True, "vip": False, "admin": True, "subs": []}
    subs = get_active_subs(uid, include_grace=True)
    vip_row = get_vip(uid)
    vip = vip_is_alive(vip_row)
    vip_until = (vip_row[4] if vip and vip_row and vip_row[4] else None)
    until = max((s[4] for s in subs), default=None)
    if vip:
        later = max([x for x in (until, vip_until) if x], default=None)
        return {"unlimited": True, "slots": None, "until": later, "host": True, "vip": True, "admin": False, "subs": subs}
    host = [s for s in subs if s[2] == "host"]
    bot_subs = [s for s in subs if s[2] == "bot"]
    if host:
        return {"unlimited": True, "slots": None, "until": until, "host": True, "vip": False, "admin": False, "subs": subs}
    return {"unlimited": False, "slots": len(bot_subs), "until": until, "host": False, "vip": False, "admin": False, "subs": subs}


def has_active_slot(uid):
    """Есть ли вообще право запускать ботов (с учётом льготного периода и безлимита)."""
    if is_admin(uid) or is_vip(uid):
        return True
    row = _db_read(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id=? AND expires_at > ?",
        (uid, (datetime.now() - timedelta(hours=GRACE_HOURS)).isoformat())).fetchone())
    return bool(row and row[0] > 0)


def can_add_bot(uid):
    """Можно ли загрузить ещё одного бота. → (bool, причина)"""
    if is_admin(uid) or is_vip(uid):
        return True, ""
    lim = get_limits(uid)
    if lim["unlimited"]:
        return True, ""
    if not lim["subs"]:
        return False, "нет активной подписки"
    used = len(get_user_bots(uid))
    if used >= lim["slots"]:
        return False, f"заняты все слоты ({used}/{lim['slots']})"
    return True, ""


def limits_line(uid):
    """Короткая строка про лимиты — для меню, карточек и сообщений."""
    if is_admin(uid):
        return "👑 Безлимит (админ)"
    lim = get_limits(uid)
    if lim.get("vip"):
        return "🎫 Безлимит-карта" + (f" · до {fmt_dt(datetime.fromisoformat(lim['until']))}" if lim.get("until") else " · бессрочно")
    if lim["unlimited"]:
        return "🚀 Хостинг без лимита" + (f" · до {fmt_dt(datetime.fromisoformat(lim['until']))}" if lim.get("until") else "")
    line = f"🧩 Слотов: {len(get_user_bots(uid))}/{lim['slots'] or 0}"
    if lim.get("until"):
        line += f" · до {fmt_dt(datetime.fromisoformat(lim['until']))}"
    return line


def users_expiring_soon():
    soon = (datetime.now() + timedelta(days=NOTIFY_BEFORE_DAYS)).isoformat()
    return _db_read(lambda conn: conn.execute(
        f"SELECT {SUB_COLS} FROM subscriptions WHERE expires_at < ? AND expires_at > ? ORDER BY expires_at ASC",
        (soon, datetime.now().isoformat())).fetchall())


def cleanup_subscriptions(days=60):
    """Удаляем давно истёкшие подписки, чтобы БД не пухла."""
    border = (datetime.now() - timedelta(days=days)).isoformat()
    return _db_retry(lambda conn: conn.execute("DELETE FROM subscriptions WHERE expires_at < ?", (border,)).rowcount)


def has_sub_of_kind(uid, kind):
    """Есть ли активная подписка указанного типа (для выбора «продлить / новый слот»)."""
    row = _db_read(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id=? AND kind=? AND expires_at > ?",
        (uid, kind, datetime.now().isoformat())).fetchone())
    return bool(row and row[0] > 0)


# ── Совместимость со старым кодом/кассиром ──
def create_slot(uid, plan, mode="extend"):
    return add_subscription(uid, plan, mode=mode)


def get_active_slots(uid):
    """Оставлено для совместимости: возвращает строки подписок."""
    return get_active_subs(uid)


# ─────────────── БОТЫ ───────────────

def save_bot(uid, fname, token, ep="user_bot.py"):
    def op(conn):
        cur = conn.execute("INSERT INTO bots (user_id, filename, bot_token, created_at, entry_point) VALUES (?, ?, ?, ?, ?)",
                           (uid, fname, token, datetime.now().isoformat(), ep))
        return cur.lastrowid
    return _db_retry(op)


def get_user_bots(uid):
    return _db_read(lambda conn: conn.execute("SELECT * FROM bots WHERE user_id = ?", (uid,)).fetchall())


def get_bot(bid):
    return _db_read(lambda conn: conn.execute("SELECT * FROM bots WHERE id = ?", (bid,)).fetchone())


def get_all_bots():
    return _db_read(lambda conn: conn.execute("SELECT * FROM bots").fetchall())


def update_bot_entry(bid, ep):
    _db_retry(lambda conn: conn.execute("UPDATE bots SET entry_point = ? WHERE id = ?", (ep, bid)))


def toggle_auto_restart(bid, val):
    _db_retry(lambda conn: conn.execute("UPDATE bots SET auto_restart = ? WHERE id = ?", (val, bid)))


def get_bot_env(bid):
    b = get_bot(bid)
    if not b or len(b) < 10:
        return {}
    try:
        return json.loads(b[9] or "{}")
    except Exception:
        return {}


def set_bot_env(bid, env_vars):
    payload = json.dumps(env_vars, ensure_ascii=False)
    _db_retry(lambda conn: conn.execute("UPDATE bots SET env_vars = ? WHERE id = ?", (payload, bid)))


def freeze_bot(bid):
    _db_retry(lambda conn: conn.execute("UPDATE bots SET is_frozen = 1 WHERE id = ?", (bid,)))


def unfreeze_bot(bid):
    _db_retry(lambda conn: conn.execute("UPDATE bots SET is_frozen = 0 WHERE id = ?", (bid,)))


def delete_bot_record(bid):
    _db_retry(lambda conn: conn.execute("DELETE FROM bots WHERE id = ?", (bid,)))


# ─────────────── ЗАЯВКИ / ПРОМОКОДЫ / СТАТИСТИКА ───────────────

def create_payment_request(uid, uname, fname, plan, mode="extend"):
    def op(conn):
        cur = conn.execute("INSERT INTO payment_requests (user_id, username, full_name, plan, mode, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (uid, uname, fname, plan, mode, datetime.now().isoformat()))
        return cur.lastrowid
    return _db_retry(op)


def get_pending_requests():
    return _db_read(lambda conn: conn.execute("SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY created_at ASC").fetchall())


def get_payment_request(rid):
    return _db_read(lambda conn: conn.execute("SELECT * FROM payment_requests WHERE id = ?", (rid,)).fetchone())


def approve_payment(rid):
    _db_retry(lambda conn: conn.execute("UPDATE payment_requests SET status = 'approved', processed_at = ? WHERE id = ?", (datetime.now().isoformat(), rid)))


def reject_payment(rid):
    _db_retry(lambda conn: conn.execute("UPDATE payment_requests SET status = 'rejected', processed_at = ? WHERE id = ?", (datetime.now().isoformat(), rid)))


def user_has_pending_request(uid):
    row = _db_read(lambda conn: conn.execute("SELECT COUNT(*) FROM payment_requests WHERE user_id = ? AND status = 'pending'", (uid,)).fetchone())
    return bool(row and row[0] > 0)


def close_pending_requests(uid, plan=None, status="approved"):
    """Закрывает заявки пользователя, чтобы владелец не одобрил оплату ВТОРОЙ раз.

    Нужно, когда кассир подтвердил платёж автоматически: раньше заявка оставалась
    в статусе pending, владелец жал «Одобрить», и подписка продлевалась дважды за
    одну оплату.
    """
    now = datetime.now().isoformat()
    def op(conn):
        if plan:
            cur = conn.execute("UPDATE payment_requests SET status=?, processed_at=? WHERE user_id=? AND plan=? AND status='pending'",
                               (status, now, uid, plan))
        else:
            cur = conn.execute("UPDATE payment_requests SET status=?, processed_at=? WHERE user_id=? AND status='pending'",
                               (status, now, uid))
        return cur.rowcount
    return _db_retry(op)


def create_promo(code, plan, uses):
    try:
        _db_retry(lambda conn: conn.execute("INSERT INTO promocodes (code, plan, uses_left, created_at) VALUES (?, ?, ?, ?)",
                                            (code, plan, uses, datetime.now().isoformat())))
        return True
    except sqlite3.IntegrityError:
        return False


def get_all_promos():
    return _db_read(lambda conn: conn.execute("SELECT id, code, plan, uses_left FROM promocodes WHERE uses_left > 0").fetchall())


def delete_promo(pid):
    _db_retry(lambda conn: conn.execute("DELETE FROM promocodes WHERE id = ?", (pid,)))


def use_promo(uid, code):
    def op(conn):
        c = conn.cursor()
        p = c.execute("SELECT id, plan, uses_left FROM promocodes WHERE code = ?", (code,)).fetchone()
        if not p or p[2] <= 0:
            return False, "❌ Промокод не найден или закончился."
        if p[1] not in PLANS:
            return False, "❌ Промокод повреждён (неизвестный тариф)."
        if c.execute("SELECT 1 FROM used_promos WHERE user_id = ? AND promo_id = ?", (uid, p[0])).fetchone():
            return False, "⚠️ Ты уже использовал этот промокод."
        c.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE id = ? AND uses_left > 0", (p[0],))
        if c.rowcount != 1:
            return False, "❌ Промокод уже закончился."
        c.execute("INSERT INTO used_promos (user_id, promo_id) VALUES (?, ?)", (uid, p[0]))
        return True, p[1]
    ok, value = _db_retry(op)
    if ok:
        add_subscription(uid, value)
    return ok, value


def get_stats():
    now_iso = datetime.now().isoformat()
    soon_iso = (datetime.now() + timedelta(days=NOTIFY_BEFORE_DAYS)).isoformat()

    def read(conn):
        c = conn.cursor()
        return {
            "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "banned": c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0],
            "admins": c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0],
            "slots": c.execute("SELECT COUNT(*) FROM subscriptions WHERE kind='bot' AND expires_at > ?", (now_iso,)).fetchone()[0],
            "hosts": c.execute("SELECT COUNT(*) FROM subscriptions WHERE kind='host' AND expires_at > ?", (now_iso,)).fetchone()[0],
            "soon": c.execute("SELECT COUNT(*) FROM subscriptions WHERE expires_at > ? AND expires_at < ?", (now_iso, soon_iso)).fetchone()[0],
            "bots": c.execute("SELECT COUNT(*) FROM bots").fetchone()[0],
            "frozen": c.execute("SELECT COUNT(*) FROM bots WHERE is_frozen = 1").fetchone()[0],
            "pending": c.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'pending'").fetchone()[0],
            "vips": c.execute("SELECT COUNT(*) FROM vip WHERE expires_at IS NULL OR expires_at > ?", (now_iso,)).fetchone()[0],
            "running": len(running_bots),
        }
    return _db_read(read)


# ─────────────── СПИСОК ПОЛЬЗОВАТЕЛЕЙ ───────────────

USERS_PER_PAGE = 8


def count_users():
    return _db_read(lambda conn: conn.execute("SELECT COUNT(*) FROM users").fetchone())[0]


def fetch_users(page=0, per=USERS_PER_PAGE):
    return _db_read(lambda conn: conn.execute(
        "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (per, max(0, page) * per)).fetchall())


def get_bots_count_map():
    rows = _db_read(lambda conn: conn.execute("SELECT user_id, COUNT(*) FROM bots GROUP BY user_id").fetchall())
    return {r[0]: r[1] for r in rows}


def get_subs_summary_map():
    """{user_id: {'bot': N слотов, 'host': 1, 'until': дата}} по активным подпискам."""
    rows = _db_read(lambda conn: conn.execute(
        "SELECT user_id, kind, COUNT(*), MAX(expires_at) FROM subscriptions WHERE expires_at > ? GROUP BY user_id, kind",
        (datetime.now().isoformat(),)).fetchall())
    m = {}
    for uid, kind, n, until in rows:
        d = m.setdefault(uid, {"bot": 0, "host": 0, "until": None})
        if kind == "bot":
            d["bot"] += n
        else:
            d["host"] = 1
        if until and (not d["until"] or until > d["until"]):
            d["until"] = until
    return m


def get_vip_map():
    return {r[0]: r for r in get_all_vips()}


def user_flag_emoji(u, vip_map=None):
    if u[4]:
        return "🚫"
    if u[3]:
        return "🛡"
    if vip_map is not None and vip_is_alive(vip_map.get(u[0])):
        return "🎫"
    return "👤"


def bot_status_emoji(b):
    if b[0] in running_bots:
        return "🟢"
    if b[6]:
        return "🧊"
    return "🔴"


# ═══════════════════════════════════════════════════════════════
# 🛡 ГЛОБАЛЬНЫЙ БАН
# ═══════════════════════════════════════════════════════════════

class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and is_user_banned(u.id):
            if isinstance(event, types.Message):
                await event.answer("🚫 <b>Вы заблокированы на хостинге.</b>", parse_mode="HTML")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("🚫 Вы заблокированы", show_alert=True)
            return
        return await handler(event, data)


dp.message.middleware(BanMiddleware())
dp.callback_query.middleware(BanMiddleware())

# ═══════════════════════════════════════════════════════════════
# 🚀 ОБЁРТКА И ЗАПУСК
# ═══════════════════════════════════════════════════════════════

WRAPPER_CODE = r'''#!/usr/bin/env python3
import os, sys, subprocess, time, signal, hashlib, re, venv, json
ENTRY_POINT = "{{ENTRY_POINT}}"
print(f"\n[ BotHost ] Подготовка проекта... Точка входа: {ENTRY_POINT}", flush=True)
VENV_DIR = ".venv"
PYBIN = os.path.join(VENV_DIR, "bin", "python") if os.name != "nt" else os.path.join(VENV_DIR, "Scripts", "python.exe")
if not os.path.exists(PYBIN):
    print("[ BotHost ] Создаю изолированное окружение...", flush=True)
    venv.EnvBuilder(with_pip=True).create(VENV_DIR)
req_files=[]
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in {VENV_DIR, "__pycache__", ".git"}]
    for name in files:
        low=name.lower()
        if low=="requirements.txt" or (low.startswith("requirements") and low.endswith(".txt")):
            req_files.append(os.path.join(root,name))
req_files=sorted(set(req_files))
digest=hashlib.sha256()
for rf in req_files:
    digest.update(rf.encode()); digest.update(open(rf,"rb").read())
req_hash=digest.hexdigest(); marker=".requirements.installed"
old_hash=open(marker,encoding="utf-8").read().strip() if os.path.exists(marker) else ""
if req_files and req_hash != old_hash:
    for rf in req_files:
        print(f"[ BotHost ] Устанавливаю зависимости: {rf}", flush=True)
        r=subprocess.run([PYBIN,"-m","pip","install","-r",rf,"--no-cache-dir","--disable-pip-version-check"])
        if r.returncode: sys.exit(r.returncode)
    open(marker,"w",encoding="utf-8").write(req_hash)
IMPORT_TO_PACKAGE={"telegram":"python-telegram-bot","PIL":"Pillow","cv2":"opencv-python","bs4":"beautifulsoup4","dotenv":"python-dotenv","yaml":"PyYAML","Crypto":"pycryptodome","dateutil":"python-dateutil","jwt":"PyJWT","multipart":"python-multipart","fitz":"PyMuPDF","openai":"openai","groq":"groq","aiogram":"aiogram","discord":"discord.py","requests":"requests","aiohttp":"aiohttp","flask":"flask","fastapi":"fastapi","uvicorn":"uvicorn","pydantic":"pydantic","sqlalchemy":"sqlalchemy","redis":"redis","pymongo":"pymongo"}

def ensure_telegram_package():
    # `telegram` on PyPI (0.0.1) is not python-telegram-bot.
    # A bot using `from telegram import ...` needs python-telegram-bot.
    try:
        import telegram
        if not hasattr(telegram, "Bot"):
            print("[ BotHost ] Обнаружен неправильный пакет telegram; заменяю на python-telegram-bot...", flush=True)
            subprocess.run([PYBIN,"-m","pip","uninstall","-y","telegram"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            r=subprocess.run([PYBIN,"-m","pip","install","python-telegram-bot","--no-cache-dir","--disable-pip-version-check"])
            if r.returncode:
                print("[ BotHost ] Не удалось установить python-telegram-bot", flush=True)
                sys.exit(r.returncode)
    except ImportError:
        pass

ensure_telegram_package()
print("[ BotHost ] Зависимости готовы, запускаю проект...", flush=True)
process=None
def handle_signal(signum,frame):
    global process
    if process:
        try: process.terminate(); process.wait(timeout=5)
        except Exception:
            try: process.kill()
            except Exception: pass
    sys.exit(0)
signal.signal(signal.SIGTERM,handle_signal); signal.signal(signal.SIGINT,handle_signal)
env={k:v for k,v in os.environ.items() if k not in {"BOT_TOKEN","CASHIER_TOKEN","GROQ_API_KEY","SYNC_CHANNEL_ID","SYNC_SECRET","OWNER_ID","OWNER_USERNAME","VERIFIER_BOT","DATABASE_URL","DATA_DIR","BOTHOST_USER_ENV","BOTHOST_BOT_TOKEN"} and not k.startswith("RAILWAY_")}
env["PYTHONUNBUFFERED"]="1"
try:
    user_env=json.loads(os.environ.get("BOTHOST_USER_ENV","{}"))
    if isinstance(user_env,dict): env.update({str(k):str(v) for k,v in user_env.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",str(k))})
except Exception: pass
if os.environ.get("BOTHOST_BOT_TOKEN"): env["BOT_TOKEN"]=os.environ["BOTHOST_BOT_TOKEN"]
attempt=0
while attempt<4:
    process=subprocess.Popen([PYBIN,"-u",ENTRY_POINT],env=env,stdout=sys.stdout,stderr=subprocess.STDOUT)
    while process.poll() is None: time.sleep(1)
    ret=process.returncode
    if ret==0: sys.exit(0)
    attempt+=1
    # Автоустановка отсутствующего модуля по последнему логу процесса.
    missing=None
    if os.path.exists("bot.log"):
        txt=open("bot.log",encoding="utf-8",errors="ignore").read()[-12000:]
        m=re.findall(r"No module named ['\"]([^'\"]+)",txt)
        if m: missing=m[-1].split('.')[0]
    if not missing: sys.exit(ret)
    package=IMPORT_TO_PACKAGE.get(missing,missing)
    print(f"[ BotHost ] Не хватает {missing}; устанавливаю {package}...",flush=True)
    r=subprocess.run([PYBIN,"-m","pip","install",package,"--no-cache-dir","--disable-pip-version-check"])
    if r.returncode: sys.exit(ret)
sys.exit(ret)
'''


def write_wrapper(bot_dir, entry_point):
    (bot_dir / "wrapper.py").write_text(WRAPPER_CODE.replace("{{ENTRY_POINT}}", entry_point), encoding="utf-8")


async def archive_bot_files(bot_id: int):
    """Сохраняет актуальные файлы бота в Telegram Storage Channel."""
    if not FILE_STORAGE_CHANNEL_ID:
        logger.warning("BOT_FILES_STORAGE_CHANNEL_ID/STORAGE_CHANNEL_ID не задан — архив #%s только локальный", bot_id)
        return False
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    if not bot_dir.exists():
        return False
    tmp = DATA_DIR / f"bot_{bot_id}_{int(time.time())}.zip"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(bot_dir):
                dirs[:] = [d for d in dirs if d not in {"__pycache__", ".venv", ".git"}]
                for name in files:
                    if name in {"wrapper.py", "bot.log", ".requirements.installed"}:
                        continue
                    fp = Path(root) / name
                    z.write(fp, fp.relative_to(bot_dir).as_posix())
        if tmp.stat().st_size > 49 * 1024 * 1024:
            logger.error("Архив бота #%s слишком большой для Telegram: %s bytes", bot_id, tmp.stat().st_size)
            return False
        b = get_bot(bot_id)
        ep = (b[7] if b and len(b) > 7 else "") or find_entry_point(bot_dir)
        msg = await bot.send_document(FILE_STORAGE_CHANNEL_ID, FSInputFile(str(tmp)), caption=(
            f"🤖 <b>BotHost FILE BACKUP</b>\n"
            f"BOT_ID=<code>{bot_id}</code>\n"
            f"OWNER_ID=<code>{b[1] if b else 0}</code>\n"
            f"ENTRY_POINT=<code>{html.escape(ep)}</code>\n"
            f"DATE=<code>{datetime.now().isoformat()}</code>"
        ), parse_mode="HTML")
        _db_retry(lambda conn: conn.execute(
            "INSERT INTO bot_archives(bot_id,message_id,file_id,filename,created_at) VALUES(?,?,?,?,?)",
            (bot_id, msg.message_id, msg.document.file_id, tmp.name, datetime.now().isoformat())))
        logger.info("📦 Файлы бота #%s сохранены в Storage Channel (message_id=%s)", bot_id, msg.message_id)
        return True
    except Exception as e:
        logger.exception("Не удалось сохранить файлы бота #%s: %s", bot_id, e)
        return False
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


async def restore_bot_files(bot_id: int):
    """Восстанавливает последнюю архивную версию файлов из Telegram."""
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    archives = _db_read(lambda conn: conn.execute("SELECT file_id FROM bot_archives WHERE bot_id=? ORDER BY id DESC LIMIT 1", (bot_id,)).fetchone())
    if not archives:
        return False
    file_id = archives[0]
    tmp = DATA_DIR / f"restore_bot_{bot_id}_{uuid.uuid4().hex}.zip"
    try:
        info = await bot.get_file(file_id)
        await bot.download_file(info.file_path, destination=tmp)
        bot_dir.mkdir(parents=True, exist_ok=True)
        for child in list(bot_dir.iterdir()):
            if child.name in {"bot.log"}:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except Exception:
                    pass
        with zipfile.ZipFile(tmp, "r") as z:
            safe_extract_zip(z, bot_dir)
        # ВАЖНО: не перезаписываем выбранную пользователем точку входа. Раньше здесь
        # стоял find_entry_point(), и после восстановления из архива точка входа
        # подменялась «первым по алфавиту» файлом (например apscheduler.py) — бот
        # перестарался запускаться.
        b = get_bot(bot_id)
        stored = b[7] if b and len(b) > 7 else ""
        ep, fixed = resolve_entry_point(bot_id, bot_dir, stored)
        logger.info("♻️ Файлы бота #%s восстановлены из Telegram Storage (точка входа: %s%s)",
                    bot_id, ep or "не найдена", ", исправлена" if fixed else "")
        return True
    except Exception as e:
        logger.warning("Не удалось восстановить файлы бота #%s: %s", bot_id, e)
        return False
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


async def start_user_bot(bot_id):
    lock = start_locks.setdefault(bot_id, asyncio.Lock())
    async with lock:
        try:
            b = get_bot(bot_id)
            if not b or b[6] == 1:
                return False
            current = running_bots.get(bot_id)
            if current and current.poll() is None:
                return True
            bot_dir = BOTS_DIR / f"bot_{bot_id}"
            bot_dir.mkdir(parents=True, exist_ok=True)
            stored_ep = b[7] if len(b) > 7 else ""
            ep, ep_fixed = resolve_entry_point(bot_id, bot_dir, stored_ep)
            if not ep:
                logger.error("У бота #%s нет .py файла для запуска", bot_id)
                _db_retry(lambda conn: conn.execute("UPDATE bots SET status='error' WHERE id=?", (bot_id,)))
                return False
            logger.info("▶️ Запуск бота #%s | точка входа: %s%s | первый запуск может ставить зависимости (несколько минут)",
                        bot_id, ep, " (исправлена автоматически)" if ep_fixed else "")
            if ep_fixed:
                try:
                    await bot.send_message(
                        b[1],
                        f"🔧 <b>Точка входа бота #{bot_id} исправлена автоматически</b>\n\n"
                        f"Было: <code>{html.escape(stored_ep or '—')}</code>\n"
                        f"Стало: <code>{html.escape(ep)}</code>\n\n"
                        f"Файл «{html.escape(stored_ep or '—')}» не похож на файл запуска "
                        "(в нём нет <code>if __name__ == \"__main__\"</code> и запуска бота), "
                        "поэтому выбран основной файл проекта.\n"
                        "Если нужен другой — «🤖 Мои проекты» → бот → «📄 Точка входа».",
                        parse_mode="HTML")
                except Exception:
                    pass
            write_wrapper(bot_dir, ep)
            log_file = bot_dir / "bot.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().strftime('%d.%m %H:%M:%S')}] === ЗАПУСК БОТА #{bot_id} ===\n")
            env = os.environ.copy()
            for key in list(env):
                if key.startswith("RAILWAY_") or key in {"BOT_TOKEN", "CASHIER_TOKEN", "GROQ_API_KEY", "SYNC_CHANNEL_ID", "SYNC_SECRET",
                                                         "OWNER_ID", "OWNER_USERNAME", "VERIFIER_BOT", "DATABASE_URL", "DATA_DIR",
                                                         "BOTHOST_USER_ENV", "BOTHOST_BOT_TOKEN"}:
                    env.pop(key, None)
            user_env = get_bot_env(bot_id)
            env.update({str(k): str(v) for k, v in user_env.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k))})
            if b[3]:
                env["BOTHOST_BOT_TOKEN"] = b[3]
            env["BOTHOST_USER_ENV"] = json.dumps(user_env, ensure_ascii=False)
            env["PYTHONUNBUFFERED"] = "1"
            lf = open(log_file, "a", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-u", "wrapper.py"], cwd=str(bot_dir), env=env,
                                    stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
            running_bots[bot_id] = proc
            await asyncio.sleep(4)
            if proc.poll() is not None:
                if running_bots.get(bot_id) is proc:
                    running_bots.pop(bot_id, None)
                _db_retry(lambda conn: conn.execute("UPDATE bots SET status='error' WHERE id=?", (bot_id,)))
                return False
            _db_retry(lambda conn: conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,)))
            return True
        except Exception as e:
            logger.exception("Не удалось запустить бот #%s: %s", bot_id, e)
            return False


async def stop_user_bot(bot_id):
    lock = start_locks.setdefault(bot_id, asyncio.Lock())
    async with lock:
        proc = running_bots.pop(bot_id, None)
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        try:
            _db_retry(lambda conn: conn.execute("UPDATE bots SET status='stopped' WHERE id=?", (bot_id,)))
        except Exception as e:
            logger.warning("Не удалось обновить статус бота #%s: %s", bot_id, e)


def get_bot_logs(bot_id, lines=50):
    lf = BOTS_DIR / f"bot_{bot_id}" / "bot.log"
    if not lf.exists():
        return ""
    try:
        content = lf.read_text(encoding="utf-8", errors="ignore").strip()
        return "\n".join(content.split("\n")[-lines:]) if content else ""
    except Exception as e:
        return f"Ошибка чтения логов: {e}"


def list_bot_files(bot_id):
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    if not bot_dir.exists():
        return []
    files = []
    for root, dirs, fs in os.walk(bot_dir):
        dirs[:] = [d for d in dirs if d not in {".venv", "__pycache__", ".git"}]
        for f in fs:
            if f in ("wrapper.py", "bot.log", ".requirements.installed"):
                continue
            files.append((str(Path(root) / f).replace(str(bot_dir) + "/", ""), (Path(root) / f).stat().st_size))
    return files


def crash_hint(bot_id, code, logs=""):
    """Объясняет частые причины падения прямо в уведомлении."""
    b = get_bot(bot_id)
    ep = (b[7] if b and len(b) > 7 else "") or ""
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    lines = []
    if code == 0:
        lines.append("💡 Код 0 = скрипт завершился сам, сразу после запуска.")
        if ep and not looks_like_entry_point(bot_dir, ep):
            lines.append(f"⚠️ Похоже, точка входа <code>{html.escape(ep)}</code> — это не файл запуска "
                         "(библиотека внутри проекта). Открой «📄 Точка входа» и выбери свой основной файл.")
        else:
            lines.append("Проверь, что скрипт запускает бота (polling/webhook) и не заканчивается сразу.")
    text = "".join(l + "\n" for l in lines)
    return text + "\n"


async def monitor_bots():
    while True:
        try:
            for bot_id, proc in list(running_bots.items()):
                if proc.poll() is not None:
                    code = proc.returncode
                    if running_bots.get(bot_id) is proc:
                        running_bots.pop(bot_id, None)
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE bots SET status = 'error' WHERE id = ?", (bot_id,))
                    c.execute("SELECT user_id, auto_restart FROM bots WHERE id = ?", (bot_id,))
                    row = c.fetchone()
                    conn.commit()
                    conn.close()
                    if row:
                        uid, auto_restart = row
                        if auto_restart == 1 and not is_user_banned(uid):
                            try:
                                await bot.send_message(uid, f"⚠️ <b>Бот #{bot_id} упал!</b>\n🔄 <i>Авто-рестарт включён, пытаюсь поднять...</i>", parse_mode="HTML")
                            except Exception:
                                pass
                            await asyncio.sleep(5)
                            await start_user_bot(bot_id)
                        else:
                            if uid != OWNER_ID:
                                logs = get_bot_logs(bot_id, 15)
                                hint = crash_hint(bot_id, code, logs)
                                try:
                                    await bot.send_message(uid, f"⚠️ <b>Бот #{bot_id} упал!</b>\nКод: {code}\n{hint}<pre>{html.escape(logs[-500:])}</pre>", parse_mode="HTML")
                                except Exception:
                                    pass
                else:
                    b = get_bot(bot_id)
                    if b and (b[6] == 1 or is_user_banned(b[1]) or (b[1] != OWNER_ID and not is_admin(b[1]) and not has_active_slot(b[1]))):
                        await stop_user_bot(bot_id)
                        if b[1] != OWNER_ID and not is_admin(b[1]):
                            try:
                                await bot.send_message(
                                    b[1],
                                    f"⏸ <b>Бот #{bot_id} остановлен</b>\n\n"
                                    "📦 Подписка истекла (льготные 24 ч закончились).\n"
                                    "Продли — и бот снова поднимется с теми же файлами.",
                                    reply_markup=renew_kb("bot"),
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
        except Exception:
            logger.exception("monitor_bots error")
        await asyncio.sleep(20)


async def monitor_subscriptions():
    """Напоминания: за 3 дня, за 1 день и после истечения — с выбором тарифа продления."""
    await asyncio.sleep(15)
    while True:
        try:
            border = (datetime.now() - timedelta(days=7)).isoformat()
            rows = _db_read(lambda conn: conn.execute(
                f"SELECT {SUB_COLS} FROM subscriptions WHERE expires_at > ? AND (reminded_3d=0 OR reminded_1d=0 OR expired_notified=0) ORDER BY expires_at ASC",
                (border,)).fetchall())
            now = datetime.now()
            for s in rows:
                sub_id, uid, kind, pid, expires_at, _period, r3, r1, expn, _created = s
                if not should_notify_about_sub(uid):
                    continue
                p = PLANS.get(pid, {})
                exp = datetime.fromisoformat(expires_at)
                left = (exp - now).total_seconds()
                kind_info = KINDS.get(kind, KINDS["bot"])

                if left <= 0:
                    if expn:
                        continue
                    text = (
                        "🔴 <b>Подписка истекла</b>\n\n"
                        f"{kind_info['emoji']} <b>{kind_info['name']}</b> · {p.get('name', pid)}\n"
                        f"Истекла: <b>{fmt_dt(exp)}</b>\n\n"
                        f"⏳ Боты работают ещё <b>{GRACE_HOURS} ч</b> (льготный период), потом остановятся.\n"
                        "Файлы и настройки сохранятся — просто продли."
                    )
                    _db_retry(lambda conn, i=sub_id: conn.execute("UPDATE subscriptions SET expired_notified=1, reminded_3d=1, reminded_1d=1 WHERE id=?", (i,)))
                    try:
                        await bot.send_message(uid, text, reply_markup=renew_kb(kind), parse_mode="HTML")
                    except Exception as e:
                        logger.warning("Не удалось отправить уведомление об истечении %s: %s", uid, e)
                    continue

                if left <= 86400 and not r1:
                    text = (
                        "⏰ <b>Осталось меньше суток!</b>\n\n"
                        f"{kind_info['emoji']} <b>{kind_info['name']}</b> · {p.get('name', pid)}\n"
                        f"Осталось: <b>{human_left(expires_at)}</b> (до {fmt_dt(exp)})\n\n"
                        "Продли сейчас, чтобы боты не останавливались."
                    )
                    _db_retry(lambda conn, i=sub_id: conn.execute("UPDATE subscriptions SET reminded_1d=1, reminded_3d=1 WHERE id=?", (i,)))
                    try:
                        await bot.send_message(uid, text, reply_markup=renew_kb(kind), parse_mode="HTML")
                    except Exception as e:
                        logger.warning("Не удалось отправить напоминание %s: %s", uid, e)
                    continue

                if left <= NOTIFY_BEFORE_DAYS * 86400 and not r3:
                    text = (
                        "🔔 <b>Подписка скоро закончится</b>\n\n"
                        f"{kind_info['emoji']} <b>{kind_info['name']}</b> · {p.get('name', pid)}\n"
                        f"Осталось: <b>{human_left(expires_at)}</b> (до {fmt_dt(exp)})\n\n"
                        "♻️ Продление стакается: новые дни прибавятся к текущей дате, остаток не сгорит."
                    )
                    _db_retry(lambda conn, i=sub_id: conn.execute("UPDATE subscriptions SET reminded_3d=1 WHERE id=?", (i,)))
                    try:
                        await bot.send_message(uid, text, reply_markup=renew_kb(kind), parse_mode="HTML")
                    except Exception as e:
                        logger.warning("Не удалось отправить напоминание %s: %s", uid, e)
        except Exception:
            logger.exception("monitor_subscriptions error")
        await asyncio.sleep(SUB_CHECK_INTERVAL)


async def bootstrap_bot_archives():
    """После обновления BotHost один раз создаёт архивы для старых локальных ботов."""
    for b in get_all_bots():
        exists = _db_read(lambda conn, bid=b[0]: conn.execute("SELECT 1 FROM bot_archives WHERE bot_id=? LIMIT 1", (bid,)).fetchone())
        if not exists and get_python_files(BOTS_DIR / f"bot_{b[0]}"):
            await archive_bot_files(b[0])


async def restore_running_bots():
    """Поднимает всё, что в БД помечено как running. Возвращает (запущено, пропущено)."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, is_frozen FROM bots WHERE status = 'running'")
    bots = c.fetchall()
    conn.close()
    started = skipped = 0
    for bid, uid, frozen in bots:
        bot_dir = BOTS_DIR / f"bot_{bid}"
        if not get_python_files(bot_dir):
            await restore_bot_files(bid)
        if not frozen and not is_user_banned(uid) and has_active_slot(uid):
            if await start_user_bot(bid):
                started += 1
            else:
                skipped += 1
        else:
            skipped += 1
    return started, skipped


def note_persistence(db_existed_before: bool):
    """Подсказывает, сохраняются ли данные между деплоями (Railway Volume)."""
    probe = DATA_DIR / ".data_persists"
    had_probe = False
    try:
        had_probe = probe.exists()
        probe.write_text(datetime.now().isoformat(), encoding="utf-8")
    except Exception as e:
        logger.warning("Не удалось проверить постоянство хранилища: %s", e)
    if had_probe:
        logger.info("💾 Хранилище постоянное: подписки, боты и карты переживут редеплой.")
    elif db_existed_before:
        logger.warning("⚠️ База есть, а метки прошлого запуска нет — возможно, %s пересоздаётся. "
                       "Проверь Railway → Volume с mount path %s.", DATA_DIR, DATA_DIR)
    else:
        logger.info("💾 База не найдена — создаю в %s.", DATA_DIR)
        if str(DATA_DIR).startswith("/app"):
            logger.warning("⚠️ Если это НЕ самый первый деплой, значит Volume не подключён: "
                           "данные прошлого запуска (подписки, боты, файлы) потеряны. "
                           "Railway → сервис → Settings → Volumes → Mount path %s.", DATA_DIR)


def note_startup_time():
    """Пишет время старта в БД. Если кто-то стартовал только что — это вторая реплика,
    и именно она даёт TelegramConflictError."""
    now = datetime.now()
    def op(conn):
        row = conn.execute("SELECT v FROM meta WHERE k='last_start'").fetchone()
        prev = row[0] if row else None
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_start',?)", (now.isoformat(),))
        return prev
    try:
        prev = _db_retry(op)
    except Exception:
        return
    if not prev:
        return
    try:
        delta = (now - datetime.fromisoformat(prev)).total_seconds()
    except Exception:
        return
    if 0 <= delta < 180:
        logger.warning("⚠️ Предыдущий старт был %.0f сек назад (%s). Это похоже на вторую реплику "
                       "или пересечение деплоя — отсюда TelegramConflictError. Поставь Replicas = 1 "
                       "и убедись, что старый контейнер остановлен.", delta, prev)


async def cashier_token_is_free(token):
    """Проверяет, не опрашивает ли этот токен кто-то ещё прямо сейчас.

    Telegram отвечает 409 Conflict, если параллельно уже работает другой getUpdates.
    Так мы ловим забытый второй сервис кассира ДО того, как начнём конфликтовать.
    Возвращает (свободен, пояснение).
    """
    try:
        async with Bot(token=token) as probe:
            await probe.get_updates(offset=-1, timeout=0, limit=1)
        return True, "свободен"
    except TelegramConflictError:
        return False, "токен уже опрашивает другой процесс"
    except TelegramUnauthorizedError:
        return False, "токен недействителен (проверь CASHIER_TOKEN у @BotFather)"
    except Exception as e:
        return True, f"проверить не удалось ({e})"


async def apply_restored_state():
    """После восстановления БД из бэкапа приводит процессы в соответствие с базой:
    гасит ботов, которых в новой БД нет, и поднимает тех, кто был running."""
    known = {b[0] for b in get_all_bots()}
    stopped = 0
    for bid in list(running_bots.keys()):
        if bid not in known:
            await stop_user_bot(bid)
            stopped += 1
    started, skipped = await restore_running_bots()
    return {"started": started, "skipped": skipped, "stopped": stopped}


async def auto_backup():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            if os.path.exists(DB_PATH):
                await bot.send_document(OWNER_ID, FSInputFile(DB_PATH, filename=f"bothost_backup_{datetime.now().strftime('%Y%m%d')}.db"),
                                        caption="🔄 Автоматический бэкап базы данных.")
            for b in get_all_bots():
                await archive_bot_files(b[0])
            removed = cleanup_subscriptions(60)
            if removed:
                logger.info("🧹 Удалено старых подписок: %s", removed)
        except Exception:
            logger.exception("auto backup error")


async def analyze_with_groq(logs):
    if not groq_client:
        return "❌ **AI-дебаггер недоступен.**\nОтсутствует ключ `GROQ_API_KEY`."
    if not logs.strip():
        return "📭 Логи пустые, нечего анализировать."
    try:
        response = await groq_client.chat.completions.create(
            messages=[{"role": "system", "content": "Ты опытный Python разработчик BotHost. Найди ошибку в логах. Отвечай кратко на русском в HTML: <b>❓ Проблема:</b> ... <b>📍 Где:</b> ... <b>💡 Решение:</b> ..."},
                      {"role": "user", "content": f"Лог:\n\n{logs[-2500:]}"}],
            model="qwen/qwen3.8-27b", temperature=0.2, max_completion_tokens=350, reasoning_effort="none"
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"❌ Ошибка нейросети: {e}"


# ═══════════════════════════════════════════════════════════════
# 📝 FSM
# ═══════════════════════════════════════════════════════════════

class UploadStates(StatesGroup):
    waiting_file = State()
    waiting_token = State()


class AddFileStates(StatesGroup):
    waiting_file = State()


class EnvStates(StatesGroup):
    waiting_env_text = State()


class UserStates(StatesGroup):
    enter_promo = State()


class AdminStates(StatesGroup):
    broadcast = State()
    msg_uid = State()
    msg_text = State()
    view_user = State()
    addadmin = State()
    ban = State()
    unban = State()
    promo_code = State()
    promo_plan = State()
    promo_uses = State()
    search_bot = State()
    search_user = State()
    grant_uid = State()
    grant_plan = State()
    vip_uid = State()


# ═══════════════════════════════════════════════════════════════
# 📱 КНОПКИ
# ═══════════════════════════════════════════════════════════════

# Один polling-процесс на один сервер/контейнер.
INSTANCE_LOCK = DATA_DIR / "bothost.polling.lock"
_instance_lock_handle = None


def acquire_instance_lock():
    global _instance_lock_handle
    if os.name != "posix":
        return
    try:
        import fcntl
        _instance_lock_handle = open(INSTANCE_LOCK, "w")
        fcntl.flock(_instance_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _instance_lock_handle.write(str(os.getpid()))
        _instance_lock_handle.flush()
        atexit.register(lambda: _instance_lock_handle.close())
    except BlockingIOError:
        raise RuntimeError("На этом сервере уже запущен другой экземпляр BotHost.")
    except Exception as e:
        logger.warning("Не удалось создать локальный lock: %s", e)


def get_profile_link():
    return f"https://t.me/{OWNER_USERNAME}" if OWNER_USERNAME else f"tg://user?id={OWNER_ID}"


def get_uptime():
    sec = int(time.time() - BOT_START_TIME)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f"{d}д {h}ч {m}м"


def should_notify_about_sub(uid):
    """Нужно ли беспокоить человека напоминанием об оплате.

    Админов/владельца и держателей безлимит-карты — нет: их подписки ни на что
    не влияют (доступ и так безлимитный), а записи могли остаться от миграции
    старых слотов из v8. Именно из-за этого владелец получал «продлите подписку».
    """
    if is_admin(uid) or is_vip(uid):
        return False
    return not is_user_banned(uid)


def plans_of_kind(kind):
    return [(pid, p) for pid, p in PLANS.items() if p["kind"] == kind]


def tariffs_kb(kind):
    rows = [[InlineKeyboardButton(text=f"{p['emoji']} {p['name']} — {p['stars']}⭐ / {p['days']} дн.", callback_data=f"plan:{pid}")]
            for pid, p in plans_of_kind(kind)]
    rows.append([InlineKeyboardButton(text="« Тарифы", callback_data="buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_kb(kind):
    """Клавиатура продления для уведомлений и для остановленных ботов."""
    rows = [[InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {p['stars']}⭐", callback_data=f"plan:{pid}:extend")]
            for pid, p in plans_of_kind(kind)]
    if kind == "bot" and plans_of_kind("host"):
        rows.append([InlineKeyboardButton(text="🚀 Хостинг без лимита — ботов сколько угодно", callback_data="buykind:host")])
    rows.append([InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")])
    rows.append([InlineKeyboardButton(text="« Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb(uid):
    buttons = []
    lim = get_limits(uid)
    until = lim.get("until")
    urgent = False
    if lim["unlimited"]:
        urgent = False
    elif until:
        left = (datetime.fromisoformat(until) - datetime.now()).total_seconds()
        urgent = left <= NOTIFY_BEFORE_DAYS * 86400
    elif not lim["unlimited"] and not is_admin(uid):
        urgent = True
    if urgent:
        buttons.append([InlineKeyboardButton(text="⚠️ Продлить подписку", callback_data="myslots")])
    buttons += [
        [InlineKeyboardButton(text="💳 Тарифы", callback_data="buy"),
         InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_enter")],
        [InlineKeyboardButton(text="➕ Новый бот", callback_data="upload")],
        [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots"),
         InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")],
        [InlineKeyboardButton(text="ℹ️ Как это работает", callback_data="help")],
    ]
    if is_admin(uid):
        buttons.append([InlineKeyboardButton(text="🛠 Администрирование", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════════════
# 🔗 СВЯЗЬ С ИИ-КАССИРОМ ЧЕРЕЗ КАНАЛ
# Кассир пишет в канал: /auto_grant USER_ID PLAN GRANT_ID SIGNATURE
# ═══════════════════════════════════════════════════════════════

@dp.channel_post(F.text.startswith("/payment_request"))
async def channel_payment_request(message: types.Message):
    try:
        parts = message.text.strip().split()
        if len(parts) < 5 or not SYNC_SECRET:
            return
        uid = int(parts[1])
        plan = parts[2]
        username = parts[3]
        sig = parts[4]
        expected = hmac.new(SYNC_SECRET.encode(), f"request:{uid}:{plan}:{username}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or plan not in PLANS:
            return
        create_user_soft(uid, username)
        if not user_has_pending_request(uid):
            create_payment_request(uid, username, "", plan)
        logger.info("Payment request from cashier: user=%s plan=%s", uid, plan)
    except Exception:
        pass


@dp.channel_post(F.text.startswith("/payment_result"))
async def channel_payment_result(message: types.Message):
    try:
        parts = message.text.strip().split()
        if len(parts) < 6 or not SYNC_SECRET:
            return
        uid = int(parts[1])
        plan = parts[2]
        result = parts[3].upper()
        grant_id = parts[4]
        sig = parts[5]
        expected = hmac.new(SYNC_SECRET.encode(), f"result:{uid}:{plan}:{result}:{grant_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or plan not in PLANS:
            return
        if result == "APPROVED":
            res = add_subscription(uid, plan, grant_id=grant_id)
            if res:
                # Заявку в админке закрываем: оплата уже начислена автоматически.
                closed = close_pending_requests(uid, plan, "approved")
                if closed:
                    logger.info("Заявок закрыто автоматически после оплаты через кассира: %s (user=%s)", closed, uid)
                await send_sub_activated(uid, plan, res)
        elif result == "REJECTED":
            close_pending_requests(uid, plan, "rejected")
            try:
                await bot.send_message(uid, "❌ <b>Оплата не подтверждена.</b>\nОтправь два чётких скриншота повторно через кассира.", parse_mode="HTML")
            except Exception:
                pass
    except Exception:
        logger.exception("payment_result error")


@dp.channel_post(F.text.startswith("/service_payment_result"))
async def channel_service_payment_result(message: types.Message):
    """Оплата доп. услуг кассира (видео/анимации).

    В BotHost таких услуг нет — раньше сообщение просто пропадало в тишине.
    Теперь фиксируем в логе и уведомляем владельца, чтобы оплаченная услуга
    не потерялась. Включай услуги в кассире (CASHIER_SERVICES=1) только если
    выдаёшь их вручную.
    """
    try:
        parts = message.text.strip().split()
        if len(parts) < 6:
            return
        uid = int(parts[1])
        service_key = parts[2]
        result = parts[3].upper()
        grant_id = parts[4]
        logger.warning("💰 Оплачена услуга кассира: service=%s user=%s result=%s grant=%s — "
                       "в BotHost услуг нет, выдай вручную.", service_key, uid, result, grant_id)
        if result == "APPROVED":
            for target in {OWNER_ID, uid}:
                try:
                    await bot.send_message(
                        target,
                        f"💰 <b>Оплата услуги «{html.escape(service_key)}»</b>\n\n"
                        f"👤 ID: <code>{uid}</code>\n✅ Статус: {result}\n🔖 Grant: <code>{html.escape(grant_id)}</code>"
                        + ("\n\n⚠️ В BotHost эта услуга не выдаётся автоматически — свяжись с владельцем."
                           if target == uid else "\n\n⚠️ Выдай услугу вручную."),
                        parse_mode="HTML")
                except Exception:
                    pass
    except Exception:
        logger.exception("service_payment_result error")


@dp.channel_post(F.text.startswith("/auto_grant"))
async def channel_auto_grant(message: types.Message):
    """Выдача подписки от кассира: /auto_grant USER_ID PLAN GRANT_ID SIGNATURE."""
    try:
        parts = message.text.strip().split()
        if len(parts) < 5 or not SYNC_SECRET:
            logger.warning("Отклонён auto_grant: неверный формат или не задан SYNC_SECRET")
            return
        uid = int(parts[1])
        plan = parts[2]
        grant_id = parts[3]
        signature = parts[4]
        expected_signature = hmac.new(SYNC_SECRET.encode(), f"{uid}:{plan}:{grant_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature) or plan not in PLANS or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", grant_id):
            logger.warning("Отклонён auto_grant: проверка подписи не пройдена")
            return
        res = add_subscription(uid, plan, grant_id=grant_id)
        if not res:
            logger.info("Повторный auto_grant %s проигнорирован", grant_id)
            return
        close_pending_requests(uid, plan, "approved")
        await send_sub_activated(uid, plan, res)
        try:
            await message.reply(
                f"✅ Подписка выдана · <code>{uid}</code> · {html.escape(PLANS[plan]['name'])} · до <b>{fmt_dt(res['expires_at'])}</b>",
                parse_mode="HTML")
        except Exception:
            pass
        logger.info("Auto-grant: user=%s plan=%s grant=%s", uid, plan, grant_id)
    except Exception as e:
        logger.exception("auto_grant error: %s", e)


async def send_sub_activated(uid, pid, res):
    """Красивое уведомление об активации/продлении подписки."""
    p = PLANS[pid]
    kind_info = KINDS[p["kind"]]
    if res.get("extended"):
        head = "🎉 <b>Подписка продлена!</b>"
    elif res.get("mode") == "new":
        head = "➕ <b>Добавлен новый слот!</b>"
    else:
        head = "🎉 <b>Оплата подтверждена!</b>"
    lim = get_limits(uid)
    limit_line = ("🎫 Безлимит-карта: ботов можно запускать <b>без лимита</b>." if is_vip(uid)
                  else ("🚀 Ботов можно запускать <b>без лимита</b>." if lim["unlimited"] else f"🧩 Доступно слотов: <b>{lim['slots']}</b>."))
    try:
        await bot.send_message(
            uid,
            f"{head}\n\n"
            f"{kind_info['emoji']} <b>{kind_info['name']}</b>\n"
            f"{p['emoji']} Тариф: <b>{p['name']}</b> (+{p['days']} дн.)\n"
            f"⏳ Действует до: <b>{fmt_dt(res['expires_at'])}</b>\n"
            f"📦 Осталось: <b>{human_left(res['expires_at'].isoformat())}</b>\n\n"
            f"{limit_line}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить проект", callback_data="upload")],
                [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots"),
                 InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить user %s: %s", uid, e)


# ═══════════════════════════════════════════════════════════════
# 🎯 СТАРТ / МЕНЮ
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    create_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    name = html.escape(message.from_user.first_name or "друг")
    lim = get_limits(message.from_user.id)
    badges = []
    if lim.get("vip"):
        badges.append("🎫 безлимит-карта")
    if is_admin(message.from_user.id):
        badges.append("👑 админ")
    sub_line = "📦 " + limits_line(message.from_user.id)
    if badges:
        sub_line = " · ".join(badges) + "\n" + sub_line
    text = (
        f"👋 <b>Привет, {name}!</b>\n\n"
        f"Добро пожаловать в <b>BotHost</b> — загрузил проект, настроил переменные и запускаешь. 🚀\n\n"
        f"{sub_line}\n\n"
        f"<b>Что умеет хостинг:</b>\n"
        f"• 📦 .py и .zip проекты\n"
        f"• 📚 автоматическая установка библиотек\n"
        f"• 🔄 авто-рестарт и контроль состояния\n"
        f"• 📄 логи + 🧠 ИИ-помощник\n"
        f"• ⚙️ переменные окружения без .env-файлов\n"
        f"• 🔔 напоминание об оплате за 3 дня до конца\n\n"
        f"Выбери действие ниже — дальше всё делается кнопками."
    )
    await message.answer(text, reply_markup=main_menu_kb(message.from_user.id), parse_mode="HTML")


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb(message.from_user.id))


@dp.message(Command("myslots"))
async def cmd_myslots(message: types.Message, state: FSMContext):
    await state.clear()
    text, kb = subs_page(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "back_main")
async def back_main(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    lim = get_limits(call.from_user.id)
    if lim["unlimited"]:
        head = "🏠 <b>BotHost</b> · 🚀 без лимита ботов"
    elif lim["slots"]:
        head = f"🏠 <b>BotHost</b> · 🧩 слотов: {len(get_user_bots(call.from_user.id))}/{lim['slots']}"
    else:
        head = "🏠 <b>BotHost</b> · 📦 подписки нет"
    try:
        await call.message.edit_text(head, reply_markup=main_menu_kb(call.from_user.id), parse_mode="HTML")
    except Exception:
        await call.message.answer(head, reply_markup=main_menu_kb(call.from_user.id), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# 🛡 БЕЗОПАСНАЯ РАБОТА С ZIP
# ═══════════════════════════════════════════════════════════════

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ZIP_FILES = 500
MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024


def safe_extract_zip(zf: zipfile.ZipFile, destination: Path):
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_FILES:
        raise ValueError(f"В архиве слишком много файлов (максимум {MAX_ZIP_FILES}).")
    total = 0
    destination = destination.resolve()
    for info in infos:
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED:
            raise ValueError("Распакованный архив слишком большой (максимум 100 МБ).")
        target = (destination / info.filename).resolve()
        if not str(target).startswith(str(destination) + os.sep):
            raise ValueError("Архив содержит небезопасный путь.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src_f, open(target, "wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f)


def get_python_files(bot_dir: Path) -> list[str]:
    """Все возможные точки входа внутри папки бота, самые вероятные — первыми.

    Раньше сортировка была «по имени файла», и в проекте, где рядом с ботом лежит
    скопированная библиотека (например apscheduler.py), точкой входа выбирался именно
    библиотечный файл: он мгновенно завершался, и это выглядело как цикл падений.
    Теперь смотрим ещё и содержимое: `if __name__ == "__main__"`, импорт фреймворка,
    запуск polling.
    """
    result = []
    for p in bot_dir.rglob("*.py"):
        try:
            rel = p.relative_to(bot_dir)
        except ValueError:
            continue
        if any(part in {".venv", "__pycache__", ".git"} or part.startswith(".") for part in rel.parts):
            continue
        if rel.name in {"wrapper.py", "cashier.py"}:
            continue
        result.append(rel.as_posix())
    result.sort(key=lambda x: (-entry_score(bot_dir / x), entry_name_preference(x), len(Path(x).parts), x.lower()))
    return result


# Имена модулей, которые почти всегда являются библиотекой, а не точкой входа.
LIBRARY_FILENAMES = {
    "apscheduler", "sqlalchemy", "aiohttp", "requests", "dotenv", "uvicorn", "fastapi", "flask",
    "django", "numpy", "pandas", "yaml", "redis", "pymongo", "celery", "alembic", "kombu",
    "billiard", "pytz", "dateutil", "six", "attr", "attrs", "click", "jinja2", "werkzeug",
    "starlette", "pydantic", "httpx", "urllib3", "certifi", "idna", "chardet", "bs4", "lxml",
    "cv2", "pil", "openai", "groq", "telegram", "telebot", "aiogram", "discord", "logging",
    "typing", "asyncio", "sqlite3", "json", "config", "settings", "utils", "helpers", "models",
    "database", "middlewares", "middleware", "keyboards", "filters", "states", "handlers",
    "admin", "user", "loader", "scheduler", "tasks", "exceptions", "constants", "texts",
}

# Приоритет по имени файла (добавляется к оценке содержимого).
ENTRY_NAME_BONUS = {
    "main.py": 30, "user_bot.py": 30, "bot.py": 25, "app.py": 20, "run.py": 18,
    "start.py": 15, "server.py": 12, "index.py": 10, "manage.py": 10,
}


def entry_name_preference(rel: str) -> int:
    return 0 if Path(rel).name.lower() in ENTRY_NAME_BONUS else 5


def entry_score(path: Path) -> int:
    """Насколько файл похож на точку входа: 0 — библиотека, 120 — типичный запуск бота."""
    name = path.name.lower()
    score = ENTRY_NAME_BONUS.get(name, 0)
    if name in LIBRARY_FILENAMES:
        score -= 1000
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:8000]
    except Exception:
        return score
    if "__main__" in text:
        score += 100
    if re.search(r"\b(aiogram|telebot|telegram|discord|vkbottle|maxapi|pyrogram|telethon)\b", text):
        score += 40
    if re.search(r"(start_polling|run_polling|infinity_polling|\.polling\(|executor\.start_polling|asyncio\.run\()", text):
        score += 25
    if re.search(r"(Dispatcher\(|Updater\(|Application\.builder\(\)|\bBot\()", text):
        score += 20
    if re.search(r"(BOT_TOKEN|bot_token|os\.environ\.get\()", text):
        score += 10
    if "__main__" not in text and score < 40:
        score -= 20
    return score


def looks_like_entry_point(bot_dir: Path, rel: str) -> bool:
    """Похоже ли, что выбранный файл действительно запускает бота."""
    return entry_score(bot_dir / rel) >= 40


def find_entry_point(bot_dir: Path) -> str:
    files = get_python_files(bot_dir)
    return files[0] if files else ""


def resolve_entry_point(bot_id: int, bot_dir: Path, stored: str):
    """Возвращает рабочую точку входа и лечит испорченную запись.

    Раньше проверялось только «файл существует». Из-за этого в базе мог остаться
    библиотечный файл (например apscheduler.py), и бот запускался вхолостую:
    скрипт мгновенно завершался, а снаружи это выглядело как цикл падений.
    Теперь, если запись не похожа на файл запуска, а в проекте есть файл, который
    на него похож, — переключаемся и говорим об этом владельцу.

    Возвращает (точка_входа, было_исправлено).
    """
    stored = (stored or "").replace("\\", "/").lstrip("/")
    candidate = (bot_dir / stored).resolve() if stored else None
    root = bot_dir.resolve()
    if candidate and str(candidate).startswith(str(root) + os.sep) and candidate.is_file() and candidate.suffix.lower() == ".py":
        rel = candidate.relative_to(root).as_posix()
        if looks_like_entry_point(bot_dir, rel):
            return rel, False
        better = find_entry_point(bot_dir)
        if better and better != rel and looks_like_entry_point(bot_dir, better):
            logger.warning("Точка входа бота #%s выглядела как библиотечный файл (%s) — переключаю на %s",
                           bot_id, rel, better)
            update_bot_entry(bot_id, better)
            return better, True
        return rel, False
    detected = find_entry_point(bot_dir)
    if detected:
        update_bot_entry(bot_id, detected)
        return detected, bool(stored and stored != detected)
    return "", False


# ═══════════════════════════════════════════════════════════════
# 📤 ЗАГРУЗКА ФАЙЛОВ
# ═══════════════════════════════════════════════════════════════

@dp.message(F.document)
async def handle_files(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    doc = message.document
    if (doc.file_size or 0) > MAX_UPLOAD_BYTES:
        return await message.answer("❌ Файл слишком большой. Максимум — 20 МБ.")
    fname = doc.file_name or "bot.zip"
    ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
    curr = await state.get_state()

    # Добавление файла к существующему боту
    if curr == AddFileStates.waiting_file.state:
        data = await state.get_data()
        target_bid = data.get("target_bid")
        b = get_bot(target_bid)
        if not b or (b[1] != uid and not is_admin(uid)):
            await state.clear()
            return await message.answer("❌ Доступ запрещён.")
        bot_dir = BOTS_DIR / f"bot_{target_bid}"
        bot_dir.mkdir(parents=True, exist_ok=True)
        try:
            finfo = await bot.get_file(doc.file_id)
            if ext == "zip":
                tmp = bot_dir / fname
                await bot.download_file(finfo.file_path, destination=tmp)
                with zipfile.ZipFile(tmp, "r") as z:
                    safe_extract_zip(z, bot_dir)
                tmp.unlink(missing_ok=True)
                await archive_bot_files(target_bid)
                await message.answer(f"✅ Архив распакован в бота #{target_bid}! Файлы сохранены в Telegram-хранилище.", parse_mode="HTML")
            else:
                await bot.download_file(finfo.file_path, destination=bot_dir / fname)
                await archive_bot_files(target_bid)
                await message.answer(f"✅ Файл <code>{html.escape(fname)}</code> добавлен в бота #{target_bid}! Файлы сохранены в Telegram-хранилище.", parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
        await state.clear()
        return

    # Восстановление БД владельцем
    if uid == OWNER_ID and ext == "db" and curr is None:
        tmp_db = DATA_DIR / f"restore_{uuid.uuid4().hex}.db"
        try:
            await message.answer("⏳ <i>Загружаю базу и привожу ботов в порядок...</i>", parse_mode="HTML")
            finfo = await bot.get_file(doc.file_id)
            await bot.download_file(finfo.file_path, destination=tmp_db)
            safety_name = swap_database_file(tmp_db)

            restored = 0
            for b in get_all_bots():
                if await restore_bot_files(b[0]):
                    restored += 1
            # Раньше после подмены базы боты оставались лежать: процессы в памяти
            # не соответствовали новой БД. Теперь синхронизируем состояние.
            act = await apply_restored_state()
            stats = get_stats()
            return await message.answer(
                f"✅ <b>Память восстановлена!</b>\n\n🤖 Записей ботов: <b>{len(get_all_bots())}</b>\n"
                f"📦 Файловых архивов восстановлено: <b>{restored}</b>\n"
                f"📊 Подписок активно: <b>{stats['slots'] + stats['hosts']}</b> + 🎫 <b>{stats['vips']}</b>\n"
                f"🚀 Ботов запущено: <b>{act['started']}</b>"
                + (f" · пропущено: <b>{act['skipped']}</b>" if act["skipped"] else "")
                + (f"\n⏹ Остановлено лишних: <b>{act['stopped']}</b>" if act["stopped"] else "")
                + (f"\n\n🛟 Старая база сохранена как <code>{safety_name}</code> — "
                   "если залил не тот бэкап, пришли этот файл обратно."
                   if safety_name else ""),
                parse_mode="HTML")
        except Exception as e:
            return await message.answer(f"❌ Ошибка восстановления базы: {e}\n\nТекущая база не тронута.")
        finally:
            try:
                tmp_db.unlink(missing_ok=True)
            except Exception:
                pass

    # Новый бот
    allowed, reason = can_add_bot(uid)
    if not allowed:
        lim = get_limits(uid)
        if not lim["subs"]:
            return await message.answer(
                "❌ <b>Нет активной подписки!</b>\nКупи тариф или введи промокод.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Тарифы", callback_data="buy"),
                     InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_enter")]
                ]),
                parse_mode="HTML")
        return await message.answer(
            f"❌ <b>Все слоты заняты</b> ({reason}).\n\n"
            "• удали ненужного бота — слот освободится,\n"
            "• или купи ещё слот,\n"
            "• или возьми 🚀 <b>хостинг без лимита</b> — ботов сколько угодно.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Хостинг без лимита", callback_data="buykind:host")],
                [InlineKeyboardButton(text="🧩 Ещё слот", callback_data="buykind:bot")],
                [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots")]
            ]),
            parse_mode="HTML")
    if ext not in ("py", "zip"):
        return await message.answer("❌ Только <b>.py</b> или <b>.zip</b>", parse_mode="HTML")

    await state.update_data(file_id=doc.file_id, fname=fname, ext=ext)
    await state.set_state(UploadStates.waiting_token)
    await message.answer(
        f"✅ <b>Файл {html.escape(fname)} получен!</b>\n\n"
        f"Отправь <b>токен</b> от @BotFather\n"
        f"или напиши <code>none</code>",
        parse_mode="HTML")


@dp.message(UploadStates.waiting_token, F.text)
async def handle_token(message: types.Message, state: FSMContext):
    token = message.text.strip()
    if token.lower() in ("none", "нет", "no", "-", "skip", "нету"):
        token = ""
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    msg = await message.answer("⏳ <i>Распаковка проекта...</i>", parse_mode="HTML")
    try:
        ep = ""
        bid = save_bot(message.from_user.id, data["fname"], token, ep)
        bot_dir = BOTS_DIR / f"bot_{bid}"
        bot_dir.mkdir(parents=True, exist_ok=True)

        finfo = await bot.get_file(data["file_id"])
        dpath = bot_dir / data["fname"]
        await bot.download_file(finfo.file_path, destination=dpath)

        if data["ext"] == "zip":
            with zipfile.ZipFile(dpath, "r") as z:
                safe_extract_zip(z, bot_dir)
            dpath.unlink(missing_ok=True)
        else:
            target = bot_dir / data["fname"]
            if dpath != target:
                dpath.replace(target)

        ep = find_entry_point(bot_dir)
        update_bot_entry(bid, ep)
        if ep:
            write_wrapper(bot_dir, ep)
        await archive_bot_files(bid)
        slot_line = "📦 " + limits_line(message.from_user.id)
        if not ep:
            await msg.edit_text(
                f"⚠️ <b>Бот #{bid} загружен, но .py файл не найден.</b>\n\n"
                "Добавь Python-файл через «📁 Файлы».")
        else:
            # Если автоопределение взяло библиотечный файл (например apscheduler.py),
            # честно предупреждаем: бот запустится и сразу завершится.
            warn = "" if looks_like_entry_point(bot_dir, ep) else (
                "\n\n⚠️ <b>Похоже, это не файл запуска</b> (в нём нет "
                "<code>if __name__ == \"__main__\"</code> и запуска бота).\n"
                "Нажми «📄 Точка входа» и выбери свой основной файл.")
            await msg.edit_text(
                f"✅ <b>Бот #{bid} развёрнут!</b>\n"
                f"🚀 Точка входа: <code>{html.escape(ep)}</code>\n"
                f"{slot_line}{warn}\n\n"
                f"Если нужно запустить другой .py файл — открой «🤖 Мои проекты» → бот → «📄 Точка входа».",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Точка входа", callback_data=f"entry:{bid}")],
                    [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots")]
                ]),
                parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
    await state.clear()


@dp.message(UploadStates.waiting_token)
async def handle_token_wrong(message: types.Message):
    await message.answer("⚠️ Отправь токен <b>текстом</b> или <code>none</code>", parse_mode="HTML")


@dp.callback_query(F.data == "upload")
async def cb_upload(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    allowed, reason = can_add_bot(call.from_user.id)
    if not allowed:
        lim = get_limits(call.from_user.id)
        if not lim["subs"]:
            return await call.answer("❌ Нет активной подписки", show_alert=True)
        return await call.answer(f"❌ Все слоты заняты ({reason}). Удали бота или возьми хостинг.", show_alert=True)
    await state.set_state(UploadStates.waiting_file)
    await call.message.edit_text(
        "📤 <b>Загрузка проекта</b>\n\n"
        "Отправь <b>.py</b> или <b>.zip</b>\n\n"
        "📦 Для архивов: авто-поиск main.py + requirements.txt\n"
        "💡 Лимит: 20 МБ\n\n❌ /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]
        ]),
        parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# 💎 ТАРИФЫ + ПОДПИСКИ
# ═══════════════════════════════════════════════════════════════

def subs_page(uid):
    """Страница «📦 Моя подписка»: лимиты, сроки, прогресс, кнопки продления."""
    if is_admin(uid):
        return ("📦 <b>Моя подписка</b>\n\n👑 <b>Безлимит</b> — админ-доступ.\nБотов можно запускать сколько угодно.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📤 Загрузить проект", callback_data="upload")],
                    [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]))

    lim = get_limits(uid)
    vip_row = get_vip(uid)
    if lim.get("vip"):
        vip_until = vip_row[4] if vip_row else None
        text = (
            "📦 <b>Моя подписка</b>\n\n"
            "🎫 <b>Безлимит-карта от владельца</b>\n"
            + (f"⏳ Действует до: <b>{fmt_dt(datetime.fromisoformat(vip_until))}</b>\n" if vip_until else "♾ Действует <b>бессрочно</b>\n")
            + f"🤖 Проектов загружено: <b>{len(get_user_bots(uid))}</b>\n\n"
            "Лимита на количество ботов нет — загружай сколько нужно."
        )
        return (text, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Загрузить проект", callback_data="upload"),
             InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots")],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]))

    subs = lim["subs"]
    used = len(get_user_bots(uid))
    now = datetime.now()

    text = "📦 <b>Моя подписка</b>\n\n"
    if lim["unlimited"]:
        text += "🚀 <b>Режим: хостинг без лимита ботов</b>\n"
    else:
        text += f"🧩 <b>Слотов: {used}/{lim['slots'] or 0}</b>\n"
    text += f"🤖 Проектов загружено: <b>{used}</b>\n"

    active = [s for s in subs if datetime.fromisoformat(s[4]) > now]
    expired_grace = [s for s in subs if datetime.fromisoformat(s[4]) <= now]

    if not subs:
        text += "\n❌ <b>Активной подписки нет.</b>\nБоты не запускаются — выбери тариф ниже.\n"
    else:
        if active:
            text += "\n<b>Активные:</b>\n"
        for s in active:
            p = PLANS.get(s[3], {})
            kind_info = KINDS.get(s[2], KINDS["bot"])
            exp = datetime.fromisoformat(s[4])
            total = max(1, (s[5] or p.get("days", 1))) * 86400
            left = max(0.0, (exp - now).total_seconds())
            text += (f"\n{kind_info['emoji']} <b>{p.get('name', s[3])}</b>\n"
                     f"   {bar(left / total)} {human_left(s[4])}\n"
                     f"   до <b>{fmt_dt(exp)}</b>\n")
        for s in expired_grace:
            p = PLANS.get(s[3], {})
            kind_info = KINDS.get(s[2], KINDS["bot"])
            text += (f"\n⚠️ {kind_info['emoji']} <b>{p.get('name', s[3])}</b> — истекла\n"
                     f"   льготный период ещё {(datetime.fromisoformat(s[4]) + timedelta(hours=GRACE_HOURS) - now).total_seconds() // 3600:.0f} ч\n")

    kinds_present = {s[2] for s in subs}
    rows = []
    if "bot" in kinds_present or not kinds_present:
        rows.append([InlineKeyboardButton(text="🧩 Продлить слот на 1 бота", callback_data="buykind:bot")])
    if "host" in kinds_present:
        rows.append([InlineKeyboardButton(text="🚀 Продлить хостинг", callback_data="buykind:host")])
    else:
        rows.append([InlineKeyboardButton(text="🚀 Хостинг без лимита (выгоднее от 3 ботов)", callback_data="buykind:host")])
    rows.append([InlineKeyboardButton(text="💳 Все тарифы", callback_data="buy")])
    rows.append([InlineKeyboardButton(text="« Меню", callback_data="back_main")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "myslots")
async def cb_myslots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = subs_page(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "buy")
async def cb_buy(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if is_admin(call.from_user.id):
        return await call.message.edit_text(
            "👑 <b>У тебя безлимит!</b>\nЗагружай сколько нужно.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]),
            parse_mode="HTML")
    bot_min = min(p["stars"] for _, p in plans_of_kind("bot"))
    host_min = min(p["stars"] for _, p in plans_of_kind("host"))
    text = (
        "💎 <b>Тарифы BotHost</b>\n\n"
        f"🧩 <b>Слот на 1 бота</b> — от {bot_min}⭐\n"
        "<i>Одна подписка = один работающий бот. Удалил бота — слот свободен.</i>\n\n"
        f"🚀 <b>Хостинг без лимита</b> — от {host_min}⭐\n"
        "<i>Ботов сколько угодно, срок общий. Выгодно от 3 ботов.</i>\n\n"
        "♻️ Продление <b>стакается</b>: остаток дней не сгорает.\n"
        "➕ Нужен ещё один бот — докупи слот (тот же тариф, кнопка «➕ Ещё один слот»).\n"
        "🔔 Напомним за 3 дня до окончания."
    )
    kb = [
        [InlineKeyboardButton(text=f"🧩 Слот на 1 бота · от {bot_min}⭐", callback_data="buykind:bot")],
        [InlineKeyboardButton(text=f"🚀 Хостинг без лимита · от {host_min}⭐", callback_data="buykind:host")],
        [InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")],
        [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("buykind:"))
async def cb_buykind(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kind = call.data.split(":", 1)[1]
    if kind not in KINDS:
        return await call.answer("Неизвестный тип тарифа", show_alert=True)
    info = KINDS[kind]
    text = (
        f"{info['emoji']} <b>{info['name']}</b>\n\n"
        f"{info['hint']}\n\n"
        "♻️ Если подписка уже активна — новые дни прибавятся к текущей дате.\n"
        "Выбери срок:"
    )
    await call.message.edit_text(text, reply_markup=tariffs_kb(kind), parse_mode="HTML")


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    pid = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""
    p = PLANS.get(pid)
    if not p:
        return await call.answer("Тариф недоступен", show_alert=True)
    kind_info = KINDS[p["kind"]]
    uid = call.from_user.id
    lim = get_limits(uid)
    until = lim.get("until")

    # Шаг 1: для слотов даём выбрать — продлить срок или добавить слот.
    if not mode:
        if p["kind"] == "bot" and has_sub_of_kind(uid, "bot") and not lim["unlimited"]:
            text = (
                f"{p['emoji']} <b>{p['name']}</b> · {p['stars']}⭐ / {p['days']} дн.\n\n"
                f"📦 Сейчас: слотов <b>{lim['slots']}</b>, ботов <b>{len(get_user_bots(uid))}</b>"
                + (f", срок до <b>{fmt_dt(datetime.fromisoformat(until))}</b>" if until else "") + "\n\n"
                "Что делаем?\n"
                "♻️ <b>Продлить</b> — +дни к текущему сроку, остаток не сгорает.\n"
                "➕ <b>Ещё один слот</b> — сможешь запускать на одного бота больше."
            )
            kb = [
                [InlineKeyboardButton(text="♻️ Продлить текущий срок", callback_data=f"plan:{pid}:extend")],
                [InlineKeyboardButton(text="➕ Купить ещё один слот", callback_data=f"plan:{pid}:new")],
                [InlineKeyboardButton(text="🚀 Хостинг без лимита", callback_data="buykind:host")],
                [InlineKeyboardButton(text="« Тарифы", callback_data="buy")]
            ]
            return await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        mode = "extend"

    if mode not in ("extend", "new"):
        mode = "extend"
    if p["kind"] == "host":
        mode = "extend"

    stack_line = ""
    if mode == "extend" and until and datetime.fromisoformat(until) > datetime.now():
        new_exp = datetime.fromisoformat(until) + timedelta(days=p["days"])
        stack_line = f"\n♻️ Продление: +{p['days']} дн. к текущим <b>{human_left(until)}</b> → до <b>{fmt_dt(new_exp)}</b>"
    elif mode == "new":
        stack_line = f"\n➕ Новый слот: срок пойдёт с сегодня → до <b>{fmt_dt(datetime.now() + timedelta(days=p['days']))}</b>"

    text = (
        f"{p['emoji']} <b>Тариф: {p['name']}</b>\n\n"
        f"{kind_info['emoji']} {kind_info['name']}\n"
        f"💎 {p['stars']}⭐  •  📅 {p['days']} дней{stack_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Как оплатить:</b>\n"
        f"1️⃣ «🎁 Отправить подарок» → владельцу\n"
        f"2️⃣ Отправь подарок ровно на <b>{p['stars']}⭐</b>\n"
        f"3️⃣ Вернись и нажми «✅ Я оплатил»\n\n"
        f"⚡ Или <b>мгновенно через ИИ-кассира</b> — скинь скрин, подписка активируется сразу."
    )
    kb = [
        [InlineKeyboardButton(text="🎁 Отправить подарок", url=get_profile_link())],
        [InlineKeyboardButton(text="⚡ Оплатить через ИИ-кассира", url=f"https://t.me/{VERIFIER_BOT_USERNAME}?start={pid}")],
        [InlineKeyboardButton(text="✅ Я оплатил (ручная проверка)", callback_data=f"pay_done:{pid}:{mode}")],
        [InlineKeyboardButton(text="« Тарифы", callback_data="buy")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML", disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("pay_done:"))
async def cb_pay_done(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    pid = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else "extend"
    if mode not in ("extend", "new"):
        mode = "extend"
    p = PLANS.get(pid)
    if not p:
        return await call.answer("Тариф недоступен", show_alert=True)
    if p["kind"] == "host":
        mode = "extend"
    u = call.from_user
    if user_has_pending_request(u.id):
        return await call.answer("⏳ Уже есть заявка на проверке!", show_alert=True)
    rid = create_payment_request(u.id, u.username or "", u.full_name or "", pid, mode)
    kind_info = KINDS[p["kind"]]
    mode_line = "♻️ Продление текущего срока" if mode == "extend" else "➕ Новый слот"
    await call.message.edit_text(
        f"✅ <b>Заявка #{rid} отправлена!</b>\n\n"
        f"{kind_info['emoji']} {kind_info['name']}\n"
        f"{p['emoji']} {p['name']} · {p['days']} дн. ({p['stars']}⭐)\n"
        f"{mode_line}\n"
        f"⏳ Админ проверит оплату.\n\n"
        f"💡 Быстрее: оплати через ИИ-кассира в тарифах!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]),
        parse_mode="HTML")
    try:
        await bot.send_message(
            OWNER_ID,
            f"💰 <b>Заявка #{rid}</b>\n\n👤 @{u.username or '—'} (<code>{u.id}</code>)\n"
            f"{kind_info['emoji']} {kind_info['name']}\n"
            f"{p['emoji']} <b>{p['name']}</b> · {p['days']} дн. ({p['stars']}⭐)\n"
            f"{mode_line}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{rid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{rid}")]]),
            parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data == "promo_enter")
async def cb_promo_enter(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.enter_promo)
    await call.message.edit_text(
        "🎟 <b>Ввод промокода</b>\n\nОтправь код сообщением:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]]),
        parse_mode="HTML")


@dp.message(UserStates.enter_promo)
async def process_promo(message: types.Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    ok, res = use_promo(message.from_user.id, code)
    if ok:
        p = PLANS[res]
        lim = get_limits(message.from_user.id)
        limit_line = ("🎫 Выдана безлимит-карта: ботов можно запускать <b>без лимита</b>." if lim.get("vip")
                      else ("🚀 Теперь ботов можно запускать <b>без лимита</b>." if lim["unlimited"]
                            else f"🧩 Доступно слотов: <b>{lim['slots']}</b>."))
        await message.answer(
            f"🎉 <b>Промокод активирован!</b>\n\n"
            f"{KINDS[p['kind']]['emoji']} {KINDS[p['kind']]['name']}\n"
            f"Тариф: {p['emoji']} <b>{p['name']}</b> (+{p['days']} дн.)\n"
            f"⏳ До: <b>{fmt_dt(datetime.fromisoformat(lim['until'])) if lim.get('until') else '—'}</b>\n\n{limit_line}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Новый бот", callback_data="upload")],
                [InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]))
    else:
        await message.answer(
            res,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]))
    await state.clear()


@dp.callback_query(F.data == "help")
async def cb_help(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "❓ <b>Помощь</b>\n\n"
        "<b>🎁 Как начать:</b>\n"
        "1. Выбери тариф — 🧩 слот на 1 бота или 🚀 хостинг без лимита\n"
        "2. Оплати подарком владельцу или через ИИ-кассира\n"
        "3. Отправь .py / .zip → токен → Запуск\n\n"
        "<b>♻️ Продление:</b> остаток дней не сгорает — новый срок прибавляется "
        "к текущей дате окончания. За 3 дня придёт напоминание с кнопками продления.\n\n"
        "<b>➕ Нужно больше ботов?</b>\n"
        "• 🧩 тариф на 1 бота → «Тарифы» → «➕ Купить ещё один слот» (каждый слот = +1 бот)\n"
        "• 🚀 хостинг → ботов без лимита на весь срок подписки\n\n"
        "<b>⏳ Если подписка истекла:</b> боты работают ещё 24 ч (льготный период), "
        "потом останавливаются. Файлы и переменные сохраняются — после продления просто нажми «▶️ Запуск».\n\n"
        "<b>⚡ ИИ-кассир:</b> в тарифах кнопка «Оплатить через ИИ» — скинь скрин перевода, "
        "подписка выдаётся сразу.\n\n"
        "<b>📁 Файлы:</b> Мои боты → бот → Файлы / Добавить файл\n"
        "<b>⚙️ Переменные:</b> Мои боты → бот → Переменные (без .env-файлов)"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="buy")],
            [InlineKeyboardButton(text="👤 Владелец", url=get_profile_link())],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]),
        parse_mode="HTML", disable_web_page_preview=True)


# ═══════════════════════════════════════════════════════════════
# 🤖 МОИ БОТЫ / УПРАВЛЕНИЕ
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "mybots")
async def cb_mybots(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bots = get_user_bots(call.from_user.id)
    limit_line = limits_line(call.from_user.id)
    if not bots:
        return await call.message.edit_text(
            f"🤖 <b>Нет ботов</b> ({limit_line})\n\nЗагрузи первого!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]]),
            parse_mode="HTML")
    kb = []
    for b in bots:
        st = "🟢" if b[0] in running_bots else ("🧊" if b[6] else "🔴")
        name = (b[2] or "?")[:22]
        kb.append([InlineKeyboardButton(text=f"{st} #{b[0]} • {name}", callback_data=f"bot:{b[0]}")])
    kb.append([InlineKeyboardButton(text="📤 Загрузить ещё", callback_data="upload")])
    kb.append([InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")])
    kb.append([InlineKeyboardButton(text="« Меню", callback_data="back_main")])
    await call.message.edit_text(
        f"🤖 <b>Твои боты ({len(bots)})</b> · {limit_line}\n🟢 работает • 🔴 стоп • 🧊 заморожен",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("bot:"))
async def cb_bot_detail(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    status = "🧊 Заморожен" if b[6] else ("🟢 Работает" if bid in running_bots else "🔴 Остановлен")
    auto_r = "ВКЛ ✅" if (len(b) > 8 and b[8] == 1) else "ВЫКЛ ❌"
    ep = b[7] if len(b) > 7 else "user_bot.py"
    lim_text = limits_line(b[1])

    kb = [
        [InlineKeyboardButton(text="▶️ Запуск", callback_data=f"start:{bid}"),
         InlineKeyboardButton(text="⏹ Стоп", callback_data=f"stop:{bid}")],
        [InlineKeyboardButton(text="🔄 Перезапуск", callback_data=f"restart:{bid}"),
         InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bid}")],
        [InlineKeyboardButton(text=f"🔄 Авто-рестарт: {auto_r}", callback_data=f"toggle_restart:{bid}")],
        [InlineKeyboardButton(text="📁 Файлы", callback_data=f"files:{bid}"),
         InlineKeyboardButton(text="⚙️ Переменные", callback_data=f"envmenu:{bid}")],
        [InlineKeyboardButton(text="📄 Точка входа", callback_data=f"entry:{bid}")],
        [InlineKeyboardButton(text="🧠 AI Поиск ошибки", callback_data=f"ai:{bid}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{bid}")]
    ]
    if is_admin(call.from_user.id):
        kb.insert(0, [InlineKeyboardButton(text=f"👤 Владелец ID {b[1]}", callback_data=f"adm_viewuser_id:{b[1]}")])
        if b[6]:
            kb.append([InlineKeyboardButton(text="♨️ Разморозить", callback_data=f"unfreeze:{bid}")])
        else:
            kb.append([InlineKeyboardButton(text="🧊 Заморозить", callback_data=f"freeze:{bid}")])
    back = "mybots" if b[1] == call.from_user.id else "adm_allbots"
    kb.append([InlineKeyboardButton(text="« Назад", callback_data=back)])

    text = (
        f"🤖 <b>Бот #{bid}</b>\n\n"
        f"📁 <code>{html.escape(str(b[2]))}</code>\n"
        f"🚀 <code>{html.escape(str(ep))}</code>\n"
        f"📊 {status}\n"
        f"📦 {lim_text}"
    )
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("entry:"))
async def cb_entry_menu(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    bot_dir = BOTS_DIR / f"bot_{bid}"
    files = get_python_files(bot_dir)
    if not files:
        return await call.answer("❌ В боте нет .py файлов", show_alert=True)
    current = b[7] if len(b) > 7 else ""
    rows = []
    for i, path in enumerate(files[:80]):
        mark = "✅ " if path == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}🐍 {path}"[:64], callback_data=f"entryset:{bid}:{i}")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"bot:{bid}")])
    await call.message.edit_text(
        f"📄 <b>Выберите точку входа для бота #{bid}</b>\n\n"
        "Именно этот .py файл BotHost будет запускать.\n"
        f"Текущая: <code>{html.escape(current or 'не выбрана')}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("entryset:"))
async def cb_entry_set(call: types.CallbackQuery):
    _, bid_s, idx_s = call.data.split(":", 2)
    bid, idx = int(bid_s), int(idx_s)
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    bot_dir = BOTS_DIR / f"bot_{bid}"
    files = get_python_files(bot_dir)
    if idx < 0 or idx >= len(files):
        return await call.answer("❌ Список файлов изменился. Открой выбор заново.", show_alert=True)
    ep = files[idx]
    update_bot_entry(bid, ep)
    write_wrapper(bot_dir, ep)
    await call.answer("✅ Точка входа изменена")
    await cb_bot_detail(call, None)


@dp.callback_query(F.data.startswith("toggle_restart:"))
async def cb_toggle_restart(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return
    cur = b[8] if len(b) > 8 else 0
    toggle_auto_restart(bid, 0 if cur == 1 else 1)
    await call.answer("Авто-рестарт переключён")
    await cb_bot_detail(call, None)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    if b[6]:
        return await call.answer("🧊 Заморожен!", show_alert=True)
    if bid in running_bots:
        return await call.answer("Уже запущен", show_alert=True)
    if not has_active_slot(b[1]) and not is_admin(b[1]):
        return await call.answer("📦 Подписка истекла — продли её", show_alert=True)
    await call.answer("⏳ Запуск...")
    ok = await start_user_bot(bid)
    if ok:
        await call.message.answer(f"✅ Бот #{bid} запущен!", parse_mode="HTML")
    else:
        logs = html.escape(get_bot_logs(bid, 30)[-1500:] or "пусто")
        b2 = get_bot(bid) or b
        ep_now = (b2[7] if len(b2) > 7 else "") or ""
        warn = ""
        if ep_now and not looks_like_entry_point(BOTS_DIR / f"bot_{bid}", ep_now):
            warn = (f"\n\n⚠️ Точка входа <code>{html.escape(ep_now)}</code> похожа на библиотечный файл "
                    "(нет <code>if __name__ == \"__main__\"</code> и запуска бота). "
                    "Нажми «📄 Точка входа» и выбери свой основной файл.")
        await call.message.answer(
            f"❌ Бот #{bid} не запустился{warn}\n<pre>{logs}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Точка входа", callback_data=f"entry:{bid}")],
                [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bid}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]]))


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    await stop_user_bot(bid)
    await call.answer("⏹ Остановлен")
    await cb_bot_detail(call, None)


@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return
    if b[6]:
        return await call.answer("🧊 Заморожен!", show_alert=True)
    await call.answer("🔄 Перезапуск...")
    await stop_user_bot(bid)
    await asyncio.sleep(1)
    await start_user_bot(bid)
    await cb_bot_detail(call, None)


@dp.callback_query(F.data.startswith("freeze:"))
async def cb_freeze(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    bid = int(call.data.split(":")[1])
    freeze_bot(bid)
    await stop_user_bot(bid)
    await call.answer("🧊 Заморожен")
    await cb_bot_detail(call, None)


@dp.callback_query(F.data.startswith("unfreeze:"))
async def cb_unfreeze(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    bid = int(call.data.split(":")[1])
    unfreeze_bot(bid)
    await call.answer("♨️ Разморожен")
    await cb_bot_detail(call, None)


@dp.callback_query(F.data.startswith("envmenu:"))
async def cb_envmenu(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    envs = get_bot_env(bid)
    names = ", ".join(envs.keys()) if envs else "пока не заданы"
    await call.message.edit_text(
        f"⚙️ <b>Переменные · проект #{bid}</b>\n\nБез .env-файлов. Значения передаются процессу только при запуске.\n\n"
        f"Ключи: <code>{html.escape(names[:800])}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить переменные", callback_data=f"editenv:{bid}")],
            [InlineKeyboardButton(text="🧹 Очистить", callback_data=f"clearenv:{bid}")],
            [InlineKeyboardButton(text="« К проекту", callback_data=f"bot:{bid}")]]),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("editenv:"))
async def cb_editenv(call: types.CallbackQuery, state: FSMContext):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    curr = get_bot_env(bid)
    lines = "\n".join(f"{k}={v}" for k, v in curr.items())
    await state.set_state(EnvStates.waiting_env_text)
    await state.update_data(target_bid=bid)
    await call.message.edit_text(
        f"✏️ <b>Переменные проекта #{bid}</b>\n\n<pre>{html.escape(lines[:3000] or 'пусто')}</pre>\n\n"
        "Отправь строки <code>KEY=VALUE</code>, каждая с новой строки.\n/cancel — отмена",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("clearenv:"))
async def cb_clearenv(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    set_bot_env(bid, {})
    await call.answer("Переменные очищены")
    await cb_envmenu(call, None)


@dp.message(EnvStates.waiting_env_text)
async def env_saved(message: types.Message, state: FSMContext):
    data = await state.get_data()
    bid = data["target_bid"]
    parsed = {}
    for raw in (message.text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            parsed[k] = v
    set_bot_env(bid, parsed)
    await state.clear()
    await message.answer("✅ Переменные сохранены. Перезапусти проект, чтобы применить их.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="« К проекту", callback_data=f"bot:{bid}")]]))


@dp.callback_query(F.data.startswith("ai:"))
async def cb_ai(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    logs = get_bot_logs(bid, 80)
    if not logs:
        return await call.answer("📭 Логи пустые", show_alert=True)
    msg = await call.message.answer("🧠 <i>ИИ анализирует...</i>", parse_mode="HTML")
    res = await analyze_with_groq(logs)
    await msg.edit_text(
        f"🧠 <b>Анализ ИИ:</b>\n\n{res}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]]))


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    logs = get_bot_logs(bid, 40)
    text = f"📄 <b>Логи #{bid}</b>\n\n<pre>{html.escape(logs)}</pre>" if logs else f"📄 <b>Логи #{bid}</b>\n\n📭 Пусто"
    kb = [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"logs:{bid}"),
         InlineKeyboardButton(text="🧠 AI", callback_data=f"ai:{bid}")],
        [InlineKeyboardButton(text="💾 Скачать log", callback_data=f"downlog:{bid}")],
        [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]
    ]
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("downlog:"))
async def cb_downlog(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    lp = BOTS_DIR / f"bot_{bid}" / "bot.log"
    if lp.exists() and lp.stat().st_size > 0:
        await call.message.answer_document(FSInputFile(str(lp), filename=f"bot_{bid}.log"))
        await call.answer()
    else:
        await call.answer("Лог пуст", show_alert=True)


@dp.callback_query(F.data.startswith("files:"))
async def cb_files(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    files = list_bot_files(bid)
    if not files:
        return await call.message.edit_text(
            f"📁 <b>Файлы #{bid}</b>\n\n📭 Пусто",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить", callback_data=f"addfile:{bid}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]]),
            parse_mode="HTML")
    text = f"📁 <b>Файлы #{bid}</b> ({len(files)})\n\n"
    kb = []
    for i, (fn, sz) in enumerate(files[:15]):
        text += f"• <code>{html.escape(fn)}</code> ({sz / 1024:.1f} KB)\n"
        kb.append([
            InlineKeyboardButton(text=f"⬇️ {fn[:18]}", callback_data=f"getf:{bid}:{i}"),
            InlineKeyboardButton(text="🗑", callback_data=f"delf:{bid}:{i}")
        ])
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"addfile:{bid}")])
    kb.append([InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("addfile:"))
async def cb_addfile(call: types.CallbackQuery, state: FSMContext):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    await state.set_state(AddFileStates.waiting_file)
    await state.update_data(target_bid=bid)
    await call.message.edit_text(
        f"➕ <b>Файл для бота #{bid}</b>\n\nОтправь .py / .zip или /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data=f"bot:{bid}")]]),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("getf:"))
async def cb_getf(call: types.CallbackQuery):
    _, bid_s, idx_s = call.data.split(":")
    bid, idx = int(bid_s), int(idx_s)
    files = list_bot_files(bid)
    if idx < len(files):
        fp = BOTS_DIR / f"bot_{bid}" / files[idx][0]
        if fp.exists():
            await call.message.answer_document(FSInputFile(str(fp)))
    await call.answer()


@dp.callback_query(F.data.startswith("delf:"))
async def cb_delf(call: types.CallbackQuery):
    _, bid_s, idx_s = call.data.split(":")
    bid, idx = int(bid_s), int(idx_s)
    files = list_bot_files(bid)
    if idx < len(files):
        fp = BOTS_DIR / f"bot_{bid}" / files[idx][0]
        if fp.exists():
            fp.unlink()
        await archive_bot_files(bid)
        await call.answer("🗑 Удалено и новая версия сохранена в Telegram-хранилище")
        await cb_files(call, None)
    else:
        await call.answer("Не найдено", show_alert=True)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delbot(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    await stop_user_bot(bid)
    bd = BOTS_DIR / f"bot_{bid}"
    if bd.exists():
        shutil.rmtree(bd, ignore_errors=True)
    delete_bot_record(bid)
    lim = get_limits(b[1])
    free_line = "" if lim["unlimited"] else f"\n♻️ Слот свободен: {len(get_user_bots(b[1]))}/{lim['slots'] or 0}"
    await call.answer(f"🗑 Удалён{free_line}", show_alert=False)
    await cb_mybots(call)


# ═══════════════════════════════════════════════════════════════
# 👑 АДМИНКА
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "admin")
async def cb_admin(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    if not is_admin(call.from_user.id):
        return await call.answer("🔐 Нет прав", show_alert=True)
    s = get_stats()
    pay_btn = "💰 Заявки 🔴" if s["pending"] > 0 else "💰 Заявки"
    text = (
        f"👑 <b>Панель управления</b>\n\n"
        f"👥 Пользователей: {s['users']} | 🚫 {s['banned']} | 🛡 {s['admins']} | 🎫 {s['vips']}\n"
        f"🧩 Слотов: {s['slots']} | 🚀 Хостингов: {s['hosts']}\n"
        f"🔔 Истекают ≤{NOTIFY_BEFORE_DAYS:g} дн.: {s['soon']}\n"
        f"🤖 Ботов: {s['bots']} (🟢 {s['running']} | 🧊 {s['frozen']})\n"
        f"💰 Заявок: {s['pending']}\n"
        f"⏱ {get_uptime()}"
    )
    kb = [
        [InlineKeyboardButton(text=pay_btn, callback_data="adm_payments"),
         InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promos")],
        [InlineKeyboardButton(text=f"👥 Пользователи ({s['users']})", callback_data="adm_users:0"),
         InlineKeyboardButton(text="🔍 Найти юзера", callback_data="adm_searchuser")],
        [InlineKeyboardButton(text=f"🎫 Безлимит-карты ({s['vips']})", callback_data="adm_vip_list"),
         InlineKeyboardButton(text="🎫 Выдать безлимит", callback_data="adm_vip_add")],
        [InlineKeyboardButton(text="📦 Подписки", callback_data="adm_subs"),
         InlineKeyboardButton(text="💳 Выдать подписку", callback_data="adm_grant")],
        [InlineKeyboardButton(text="🤖 Все боты", callback_data="adm_allbots"),
         InlineKeyboardButton(text="🔍 Поиск бота", callback_data="adm_searchbot")],
        [InlineKeyboardButton(text="✉️ ЛС юзеру", callback_data="adm_msguser"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data="adm_ban"),
         InlineKeyboardButton(text="✅ Разбан", callback_data="adm_unban")],
        [InlineKeyboardButton(text="🛡 +Админ", callback_data="adm_addadmin"),
         InlineKeyboardButton(text="❌ -Админ", callback_data="adm_remadmin")],
        [InlineKeyboardButton(text="💾 Бэкап БД", callback_data="adm_backup"),
         InlineKeyboardButton(text="🧹 Очистка", callback_data="adm_cleanup")],
        [InlineKeyboardButton(text="🔄 Рестарт всех", callback_data="adm_restart_all")],
        [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# 👥 ПОЛЬЗОВАТЕЛИ (админ)
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("adm_users:"))
async def cb_adm_users(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    try:
        page = int(call.data.split(":")[1])
    except Exception:
        page = 0
    total = count_users()
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    users = fetch_users(page)
    bots_map = get_bots_count_map()
    subs_map = get_subs_summary_map()
    vip_map = get_vip_map()
    s = get_stats()

    text = (
        f"👥 <b>Пользователи</b> · всего {total}\n"
        f"Стр. {page + 1}/{pages} · 🚫 {s['banned']} · 🛡 {s['admins']} · 🎫 {s['vips']}\n\n"
        f"👤 обычный · 🎫 безлимит · 🛡 админ · 🚫 бан\n"
        f"🤖 — сколько ботов · 🧩/🚀 — подписка"
    )
    kb = []
    for u in users:
        n_bots = bots_map.get(u[0], 0)
        sub = subs_map.get(u[0])
        if vip_is_alive(vip_map.get(u[0])):
            sub_txt = "🎫"
        elif sub and sub["host"]:
            sub_txt = "🚀"
        elif sub and sub["bot"]:
            sub_txt = f"🧩{sub['bot']}"
        else:
            sub_txt = "—"
        uname = f"@{u[1]}" if u[1] else (u[2] or "без имени")[:12]
        label = f"{user_flag_emoji(u, vip_map)} {u[0]} {uname} · 🤖{n_bots} · {sub_txt}"
        kb.append([InlineKeyboardButton(text=label[:64], callback_data=f"adm_u:{u[0]}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm_noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users:{page + 1}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(text="🔍 Найти юзера", callback_data="adm_searchuser"),
               InlineKeyboardButton(text="🎫 Безлимит-карты", callback_data="adm_vip_list")])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data == "adm_noop")
async def cb_adm_noop(call: types.CallbackQuery):
    await call.answer("Это счётчик страниц 🙂")


def user_card(uid):
    """Карточка пользователя: аккаунт, подписки, безлимит, его боты."""
    u = get_user(uid)
    if not u:
        return None, None
    bots = get_user_bots(uid)
    lim = get_limits(uid)
    vip_row = get_vip(uid)
    running = sum(1 for b in bots if b[0] in running_bots)
    frozen = sum(1 for b in bots if b[6])

    flags = []
    if u[4]:
        flags.append("🚫 забанен")
    if u[3]:
        flags.append("🛡 админ")
    if vip_is_alive(vip_row):
        flags.append("🎫 безлимит-карта")
    if not flags:
        flags.append("👤 обычный пользователь")

    subs = lim["subs"]
    if lim.get("vip") and vip_row and vip_row[4]:
        subs_txt = f"🎫 безлимит до {fmt_dt(datetime.fromisoformat(vip_row[4]))}"
    elif lim.get("vip"):
        subs_txt = "🎫 безлимит бессрочно"
    elif is_admin(uid):
        subs_txt = "👑 админ — безлимит"
    elif subs:
        subs_txt = "\n".join(
            f"   • {KINDS.get(s[2], {}).get('emoji', '•')} {PLANS.get(s[3], {}).get('name', s[3])} — {human_left(s[4])}"
            for s in subs)
    else:
        subs_txt = "❌ нет активных подписок"
    created = ""
    try:
        created = fmt_dt(datetime.fromisoformat(u[5]))
    except Exception:
        pass

    text = (
        f"{user_flag_emoji(u, get_vip_map())} <b>{html.escape(u[2] or 'без имени')}</b>\n"
        f"🆔 <code>{uid}</code> · @{u[1] or '—'}\n"
        f"📅 Регистрация: {created or '—'}\n"
        f"🏷 Статус: {', '.join(flags)}\n\n"
        f"📦 Подписки:\n{subs_txt}\n"
        f"📊 Лимит: {limits_line(uid)}\n\n"
        f"🤖 Боты: <b>{len(bots)}</b> (🟢 {running} · 🔴 {len(bots) - running - frozen} · 🧊 {frozen})"
    )
    kb = []
    for b in bots[:8]:
        kb.append([InlineKeyboardButton(
            text=f"{bot_status_emoji(b)} #{b[0]} {(b[2] or '?')[:16]}",
            callback_data=f"bot:{b[0]}")])
    if len(bots) > 8:
        kb.append([InlineKeyboardButton(text=f"…и ещё {len(bots) - 8} (см. «Все боты»)", callback_data="adm_allbots")])

    if vip_is_alive(vip_row):
        kb.append([InlineKeyboardButton(text="🎫 Снять безлимит", callback_data=f"adm_unvip:{uid}"),
                   InlineKeyboardButton(text="♻️ Продлить безлимит", callback_data=f"adm_vip:{uid}")])
    else:
        kb.append([InlineKeyboardButton(text="🎫 Выдать безлимит", callback_data=f"adm_vip:{uid}")])
    kb.append([InlineKeyboardButton(text="💳 Выдать подписку", callback_data=f"adm_grant_to:{uid}"),
               InlineKeyboardButton(text="✉️ Написать", callback_data=f"adm_dm:{uid}")])
    kb.append([InlineKeyboardButton(text="✅ Разбан" if u[4] else "🚫 Бан", callback_data=f"adm_act_toggleban:{uid}")])
    kb.append([InlineKeyboardButton(text="« К списку юзеров", callback_data="adm_users:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


@dp.callback_query(F.data.startswith("adm_u:"))
async def cb_adm_user_card(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    text, kb = user_card(uid)
    if not text:
        return await call.answer("Пользователь не найден", show_alert=True)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_searchuser")
async def cb_adm_searchuser(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.search_user)
    await call.message.edit_text(
        "🔍 <b>Поиск пользователя</b>\n\nОтправь ID или @username:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К списку", callback_data="adm_users:0")]]),
        parse_mode="HTML")


@dp.message(AdminStates.search_user)
async def adm_searchuser_do(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    txt = (message.text or "").strip()
    uid = find_user_by_username(txt) if txt.startswith("@") else (int(txt) if txt.isdigit() else None)
    if not uid:
        return await message.answer("❌ Не нашёл. Отправь числовой ID или @username.")
    await state.clear()
    text, kb = user_card(uid)
    if not text:
        return await message.answer("❌ Пользователь не найден")
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# 🎫 БЕЗЛИМИТ-КАРТЫ (админ)
# ═══════════════════════════════════════════════════════════════

def vip_days_kb(uid, back=None):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾ Навсегда", callback_data=f"adm_vip_set:{uid}:0")],
        [InlineKeyboardButton(text="90 дней", callback_data=f"adm_vip_set:{uid}:90"),
         InlineKeyboardButton(text="30 дней", callback_data=f"adm_vip_set:{uid}:30")],
        [InlineKeyboardButton(text="7 дней", callback_data=f"adm_vip_set:{uid}:7"),
         InlineKeyboardButton(text="1 день", callback_data=f"adm_vip_set:{uid}:1")],
        [InlineKeyboardButton(text="« Назад", callback_data=back or f"adm_u:{uid}")]
    ])


@dp.callback_query(F.data == "adm_vip_list")
async def cb_adm_vip_list(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = get_all_vips()
    bg_map = get_bots_count_map()
    if not rows:
        return await call.message.edit_text(
            "🎫 <b>Безлимит-карты</b>\n\nПока никому не выдано.\n\n"
            "Безлимит-карта даёт пользователю запускать сколько угодно ботов, "
            "но <b>не даёт доступ к админ-панели</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Выдать безлимит", callback_data="adm_vip_add")],
                [InlineKeyboardButton(text="« Админка", callback_data="admin")]]),
            parse_mode="HTML")
    text = f"🎫 <b>Безлимит-карты ({len(rows)})</b>\n\n"
    kb = []
    for r in rows:
        uid = r[0]
        u = get_user(uid)
        uname = f"@{u[1]}" if u and u[1] else ((u[2] if u else "") or "без имени")[:14]
        until = f"до {fmt_dt(datetime.fromisoformat(r[4]))}" if r[4] else "♾ навсегда"
        text += f"• <code>{uid}</code> {html.escape(uname)} — {until} · 🤖 {bg_map.get(uid, 0)}\n"
        kb.append([
            InlineKeyboardButton(text=f"👤 {uname} · {('до ' + r[4][:10]) if r[4] else '♾'}", callback_data=f"adm_u:{uid}"),
            InlineKeyboardButton(text="❌", callback_data=f"adm_unvip:{uid}")
        ])
    kb.append([InlineKeyboardButton(text="➕ Выдать безлимит", callback_data="adm_vip_add")])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data == "adm_vip_add")
async def cb_adm_vip_add(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.vip_uid)
    await call.message.edit_text(
        "🎫 <b>Выдача безлимит-карты</b>\n\n"
        "Отправь ID или @username получателя.\n"
        "Пользователь получит безлимит на ботов, но <b>не</b> доступ к админке.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="admin")]]),
        parse_mode="HTML")


@dp.message(AdminStates.vip_uid)
async def adm_vip_uid(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    txt = (message.text or "").strip()
    uid = find_user_by_username(txt) if txt.startswith("@") else (int(txt) if txt.isdigit() else None)
    if not uid:
        return await message.answer("❌ Не нашёл. Отправь числовой ID или @username.")
    await state.clear()
    u = get_user(uid)
    who = f"@{u[1]}" if u and u[1] else ((u[2] if u else "") or "новый пользователь")
    await message.answer(
        f"🎫 Кому: <code>{uid}</code> ({html.escape(who)})\n\nНа какой срок выдать безлимит?",
        reply_markup=vip_days_kb(uid, back="admin"),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_vip_set:"))
async def cb_adm_vip_set(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    _, uid_s, days_s = call.data.split(":")
    uid, days = int(uid_s), int(days_s)
    exp = grant_vip(uid, days or None, granted_by=call.from_user.id)
    u = get_user(uid)
    who = f"@{u[1]}" if u and u[1] else ""
    await call.answer("🎫 Безлимит выдан" if not exp else f"🎫 Выдан до {fmt_dt(datetime.fromisoformat(exp))}", show_alert=True)
    try:
        await bot.send_message(
            uid,
            "🎫 <b>Владелец выдал тебе безлимит-карту!</b>\n\n"
            + (f"⏳ Действует до: <b>{fmt_dt(datetime.fromisoformat(exp))}</b>\n" if exp else "♾ Действует <b>бессрочно</b>\n")
            + "\nТеперь можно запускать сколько угодно ботов — слотов и лимитов нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")]]),
            parse_mode="HTML")
    except Exception as e:
        logger.warning("Не удалось уведомить %s о безлимите: %s", uid, e)
    logger.info("🎫 Безлимит выдан %s (%s) владельцем %s", uid, exp or "бессрочно", call.from_user.id)
    text, kb = user_card(uid)
    if text:
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await call.message.edit_text(f"✅ Безлимит выдан <code>{uid}</code> {who}".strip(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_vip:"))
async def cb_adm_vip_menu(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    u = get_user(uid)
    who = f"@{u[1]}" if u and u[1] else ((u[2] if u else "") or "новый пользователь")
    await call.message.edit_text(
        f"🎫 Кому: <code>{uid}</code> ({html.escape(who)})\n\nНа какой срок выдать безлимит?",
        reply_markup=vip_days_kb(uid),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_unvip:"))
async def cb_adm_unvip(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    removed = revoke_vip(uid)
    await call.answer("🎫 Безлимит снят" if removed else "Карты и не было", show_alert=True)
    if removed:
        try:
            await bot.send_message(
                uid,
                "🎫 <b>Безлимит-карта отозвана владельцем.</b>\n\n"
                "Боты продолжат работать, если есть активная подписка — иначе они остановятся.",
                parse_mode="HTML")
        except Exception:
            pass
        logger.info("🎫 Безлимит снят у %s владельцем %s", uid, call.from_user.id)
    # Возвращаемся туда, откуда пришли: в карточку юзера или в список карт.
    text, kb = user_card(uid)
    if text:
        try:
            return await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    await cb_adm_vip_list(call)


@dp.callback_query(F.data.startswith("adm_grant_to:"))
async def cb_adm_grant_to(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    await state.set_state(AdminStates.grant_plan)
    await state.update_data(grant_uid=uid)
    rows = []
    for kind in ("bot", "host"):
        for pid, p in plans_of_kind(kind):
            rows.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {p['days']} дн.", callback_data=f"adm_grant_plan:{pid}")])
    rows.append([InlineKeyboardButton(text="« Отмена", callback_data=f"adm_u:{uid}")])
    await call.message.edit_text(
        f"💳 Выдать подписку <code>{uid}</code>\n\nВыбери тариф:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_dm:"))
async def cb_adm_dm(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    await state.set_state(AdminStates.msg_text)
    await state.update_data(target_uid=uid)
    await call.message.edit_text(f"✉️ Текст для <code>{uid}</code>:", parse_mode="HTML")


# ─────────────── ПОДПИСКИ В АДМИНКЕ ───────────────

@dp.callback_query(F.data == "adm_subs")
async def cb_adm_subs(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = _db_read(lambda conn: conn.execute(
        f"SELECT {SUB_COLS} FROM subscriptions WHERE expires_at > ? ORDER BY expires_at ASC LIMIT 40",
        (datetime.now().isoformat(),)).fetchall())
    if not rows:
        return await call.message.edit_text(
            "📦 Активных подписок нет",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="adm_grant")],
                [InlineKeyboardButton(text="« Админка", callback_data="admin")]]))
    text = f"📦 <b>Активные подписки ({len(rows)})</b>\n\n"
    unlimited_hint = False
    kb = []
    for s in rows[:30]:
        p = PLANS.get(s[3], {})
        emoji = KINDS.get(s[2], {}).get("emoji", "•")
        note = ""
        if is_admin(s[1]) or is_vip(s[1]):
            note = " · ♾ не влияет (безлимит)"
            unlimited_hint = True
        text += f"{emoji} <code>{s[1]}</code> · {p.get('name', s[3])} · {human_left(s[4])}{note}\n"
        kb.append([InlineKeyboardButton(text=f"{emoji} {s[1]} · {human_left(s[4])}", callback_data=f"adm_sub:{s[0]}")])
    if unlimited_hint:
        text += "\n♾ У админов и безлимит-карт подписки ни на что не влияют — напоминания им не приходят."
    kb.append([InlineKeyboardButton(text="🎁 Выдать подписку", callback_data="adm_grant")])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_sub:"))
async def cb_adm_sub(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    sub_id = int(call.data.split(":")[1])
    s = _db_read(lambda conn: conn.execute(f"SELECT {SUB_COLS} FROM subscriptions WHERE id=?", (sub_id,)).fetchone())
    if not s:
        return await call.answer("Подписка не найдена", show_alert=True)
    p = PLANS.get(s[3], {})
    u = get_user(s[1])
    text = (
        f"📦 <b>Подписка #{s[0]}</b>\n\n"
        f"👤 <code>{s[1]}</code> @{u[1] if u and u[1] else '—'}\n"
        f"{KINDS.get(s[2], {}).get('emoji', '•')} {KINDS.get(s[2], {}).get('name', s[2])}\n"
        f"{p.get('emoji', '')} Тариф: <b>{p.get('name', s[3])}</b>\n"
        f"⏳ Осталось: <b>{human_left(s[4])}</b> (до {fmt_dt(datetime.fromisoformat(s[4]))})\n"
        f"🤖 Ботов у юзера: <b>{len(get_user_bots(s[1]))}</b>"
    )
    kb = [
        [InlineKeyboardButton(text="➕ Продлить на 7 дней", callback_data=f"adm_sub_add:{s[0]}:7"),
         InlineKeyboardButton(text="➕ Продлить на 30 дней", callback_data=f"adm_sub_add:{s[0]}:30")],
        [InlineKeyboardButton(text="❌ Отозвать", callback_data=f"adm_sub_del:{s[0]}")],
        [InlineKeyboardButton(text="« К подпискам", callback_data="adm_subs")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_sub_add:"))
async def cb_adm_sub_add(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    _, sid_s, days_s = call.data.split(":")
    sub_id, days = int(sid_s), int(days_s)
    s = _db_read(lambda conn: conn.execute(f"SELECT {SUB_COLS} FROM subscriptions WHERE id=?", (sub_id,)).fetchone())
    if not s:
        return await call.answer("Подписка не найдена", show_alert=True)
    base = max(datetime.fromisoformat(s[4]), datetime.now())
    new_exp = base + timedelta(days=days)
    _db_retry(lambda conn: conn.execute(
        "UPDATE subscriptions SET expires_at=?, reminded_3d=0, reminded_1d=0, expired_notified=0 WHERE id=?",
        (new_exp.isoformat(), sub_id)))
    await call.answer(f"✅ +{days} дн. → {fmt_dt(new_exp)}", show_alert=True)
    try:
        await bot.send_message(s[1], f"🎁 <b>Администратор продлил подписку на {days} дн.</b>\n\n⏳ Теперь до: <b>{fmt_dt(new_exp)}</b>",
                               parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")]]))
    except Exception:
        pass
    await cb_adm_sub(call)


@dp.callback_query(F.data.startswith("adm_sub_del:"))
async def cb_adm_sub_del(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    sub_id = int(call.data.split(":")[1])
    s = _db_read(lambda conn: conn.execute(f"SELECT {SUB_COLS} FROM subscriptions WHERE id=?", (sub_id,)).fetchone())
    if s:
        _db_retry(lambda conn: conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,)))
        try:
            await bot.send_message(s[1], "❌ <b>Подписка отозвана администратором.</b>", parse_mode="HTML")
        except Exception:
            pass
    await call.answer("Отозвана")
    await cb_adm_subs(call)


@dp.callback_query(F.data == "adm_grant")
async def cb_adm_grant(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.grant_uid)
    await call.message.edit_text(
        "🎁 <b>Выдача подписки</b>\n\nОтправь ID или @username получателя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="admin")]]))


@dp.message(AdminStates.grant_uid)
async def adm_grant_uid(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    txt = (message.text or "").strip()
    uid = find_user_by_username(txt) if txt.startswith("@") else (int(txt) if txt.isdigit() else None)
    if not uid:
        return await message.answer("❌ Не нашёл пользователя. Отправь числовой ID или @username.")
    await state.update_data(grant_uid=uid)
    await state.set_state(AdminStates.grant_plan)
    rows = []
    for kind in ("bot", "host"):
        for pid, p in plans_of_kind(kind):
            rows.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {p['days']} дн.", callback_data=f"adm_grant_plan:{pid}")])
    rows.append([InlineKeyboardButton(text="« Отмена", callback_data="admin")])
    await message.answer(f"👤 Получатель: <code>{uid}</code>\n\nВыбери тариф:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_grant_plan:"))
async def cb_adm_grant_plan(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    parts = call.data.split(":")
    pid = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""
    data = await state.get_data()
    uid = data.get("grant_uid")
    if not uid:
        return await call.answer("Сессия истекла, начни заново", show_alert=True)
    if pid not in PLANS:
        return await call.answer("Тариф недоступен", show_alert=True)
    p = PLANS[pid]

    # Для слотов спрашиваем: продлить срок или выдать дополнительный слот.
    if not mode and p["kind"] == "bot" and has_sub_of_kind(uid, "bot"):
        await state.update_data(grant_uid=uid)
        return await call.message.edit_text(
            f"👤 <code>{uid}</code> · {p['emoji']} <b>{p['name']}</b>\n\nЧто выдать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="♻️ Продлить текущий срок", callback_data=f"adm_grant_plan:{pid}:extend")],
                [InlineKeyboardButton(text="➕ Дополнительный слот", callback_data=f"adm_grant_plan:{pid}:new")],
                [InlineKeyboardButton(text="« Отмена", callback_data="admin")]]),
            parse_mode="HTML")
    mode = mode or "extend"
    await state.clear()
    res = add_subscription(uid, pid, mode=mode)
    await send_sub_activated(uid, pid, res)
    await call.message.edit_text(
        f"✅ Выдано: <code>{uid}</code> · {PLANS[pid]['name']}"
        f" ({'продление' if res['extended'] else ('новый слот' if mode == 'new' else 'подписка')}) до <b>{fmt_dt(res['expires_at'])}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]]),
        parse_mode="HTML")


# ─────────────── ОСТАЛЬНАЯ АДМИНКА ───────────────

@dp.callback_query(F.data == "adm_cleanup")
async def cb_adm_cleanup(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("🧹 Очистка...")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM bots")
    db_ids = {r[0] for r in c.fetchall()}
    del_folders = del_db = 0
    if BOTS_DIR.exists():
        for item in BOTS_DIR.iterdir():
            if item.is_dir() and item.name.startswith("bot_"):
                try:
                    bid = int(item.name.split("_")[1])
                    if bid not in db_ids:
                        shutil.rmtree(item, ignore_errors=True)
                        del_folders += 1
                except Exception:
                    pass
    for bid in list(db_ids):
        if not (BOTS_DIR / f"bot_{bid}").exists():
            c.execute("DELETE FROM bots WHERE id = ?", (bid,))
            del_db += 1
    conn.commit()
    conn.close()
    removed = cleanup_subscriptions(60)
    await call.message.answer(
        f"✅ Очистка:\nПапок: {del_folders}\nЗаписей БД: {del_db}\nСтарых подписок: {removed}",
        parse_mode="HTML")


@dp.callback_query(F.data == "adm_searchbot")
async def cb_adm_searchbot(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.search_bot)
    await call.message.edit_text(
        "🔍 Введи ID бота:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="admin")]]))


@dp.message(AdminStates.search_bot)
async def adm_searchbot_do(message: types.Message, state: FSMContext):
    if not (message.text or "").isdigit():
        return await message.answer("❌ Нужно число (ID)")
    bid = int(message.text)
    if not get_bot(bid):
        return await message.answer("❌ Бот не найден")
    await state.clear()

    class FC:
        def __init__(self):
            self.message = message
            self.data = f"bot:{bid}"
            self.from_user = message.from_user
            self.answer = lambda *a, **k: asyncio.sleep(0)

    await cb_bot_detail(FC(), None)


@dp.callback_query(F.data == "adm_payments")
async def cb_adm_payments(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    reqs = get_pending_requests()
    if not reqs:
        return await call.message.edit_text(
            "💰 Нет заявок",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Админка", callback_data="admin")]]))
    text = f"💰 <b>Заявки ({len(reqs)})</b>\n\n"
    kb = []
    for r in reqs:
        p = PLANS.get(r[4], {})
        text += f"#{r[0]} @{r[2] or '—'} <code>{r[1]}</code> — {p.get('name', r[4])} ({p.get('days', '?')} дн.)\n"
        kb.append([
            InlineKeyboardButton(text=f"✅ #{r[0]}", callback_data=f"approve:{r[0]}"),
            InlineKeyboardButton(text=f"❌ #{r[0]}", callback_data=f"reject:{r[0]}")
        ])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rid = int(call.data.split(":")[1])
    req = get_payment_request(rid)
    if not req or req[5] != "pending":
        return await call.answer("Уже обработано", show_alert=True)
    if req[4] not in PLANS:
        return await call.answer("Неизвестный тариф в заявке", show_alert=True)
    approve_payment(rid)
    mode = (req[8] if len(req) > 8 and req[8] else "extend")
    res = add_subscription(req[1], req[4], mode=mode)
    await call.message.edit_text(
        f"✅ Заявка #{rid} одобрена\n\n{PLANS[req[4]]['name']} ({'продление' if mode != 'new' else 'новый слот'}) → до <b>{fmt_dt(res['expires_at'])}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К заявкам", callback_data="adm_payments")]]),
        parse_mode="HTML")
    await send_sub_activated(req[1], req[4], res)


@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rid = int(call.data.split(":")[1])
    req = get_payment_request(rid)
    if not req or req[5] != "pending":
        return await call.answer("Уже обработано", show_alert=True)
    reject_payment(rid)
    await call.message.edit_text(
        f"❌ Заявка #{rid} отклонена",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К заявкам", callback_data="adm_payments")]]))
    try:
        await bot.send_message(req[1], "❌ Заявка отклонена. Напиши владельцу, если ошибка.", parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data == "adm_promos")
async def cb_adm_promos(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    promos = get_all_promos()
    text = "🎟 <b>Промокоды</b>\n\n"
    kb = [[InlineKeyboardButton(text="➕ Создать", callback_data="adm_promo_add")]]
    if not promos:
        text += "Пусто"
    else:
        for p in promos:
            pname = PLANS.get(p[2], {}).get("name", p[2])
            text += f"• <code>{p[1]}</code> — {pname} (×{p[3]})\n"
            kb.append([InlineKeyboardButton(text=f"❌ {p[1]}", callback_data=f"adm_promo_del:{p[0]}")])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data == "adm_promo_add")
async def cb_adm_promo_add(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.promo_code)
    await call.message.edit_text("🎟 Отправь код промокода (латиница):")


@dp.message(AdminStates.promo_code)
async def adm_promo_code(message: types.Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_\-]{3,32}", code):
        return await message.answer("❌ Только латиница/цифры, 3–32 символа.")
    await state.update_data(code=code)
    await state.set_state(AdminStates.promo_plan)
    rows = []
    for kind in ("bot", "host"):
        for pid, p in plans_of_kind(kind):
            rows.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {p['days']} дн.", callback_data=f"adm_promo_plan:{pid}")])
    await message.answer(f"Код: <code>{code}</code>\nТариф:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_promo_plan:"))
async def adm_promo_plan(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    pid = call.data.split(":", 1)[1]
    if pid not in PLANS:
        return await call.answer("Тариф недоступен", show_alert=True)
    await state.update_data(plan=pid)
    await state.set_state(AdminStates.promo_uses)
    await call.message.edit_text(f"Тариф: <b>{PLANS[pid]['name']}</b>\n\nКоличество активаций (число):", parse_mode="HTML")


@dp.message(AdminStates.promo_uses)
async def adm_promo_uses(message: types.Message, state: FSMContext):
    if not (message.text or "").isdigit():
        return await message.answer("❌ Число!")
    data = await state.get_data()
    uses = int(message.text)
    if uses <= 0:
        return await message.answer("❌ Число должно быть больше нуля.")
    if create_promo(data["code"], data["plan"], uses):
        await message.answer(
            f"✅ <code>{data['code']}</code> создан!\n{PLANS[data['plan']]['name']} × {uses}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« К промокодам", callback_data="adm_promos")]]),
            parse_mode="HTML")
    else:
        await message.answer("❌ Такой код уже существует.")
    await state.clear()


@dp.callback_query(F.data.startswith("adm_promo_del:"))
async def cb_adm_promo_del(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    delete_promo(int(call.data.split(":")[1]))
    await call.answer("🗑")
    await cb_adm_promos(call)


@dp.callback_query(F.data == "adm_allbots")
async def cb_adm_allbots(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    bots = get_all_bots()
    kb = []
    for b in bots[-30:]:
        st = "🟢" if b[0] in running_bots else ("🧊" if b[6] else "🔴")
        kb.append([InlineKeyboardButton(
            text=f"{st} #{b[0]} u:{b[1]} {(b[2] or '')[:12]}",
            callback_data=f"bot:{b[0]}")])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(
        "🤖 <b>Все боты (God Mode)</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_viewuser_id:"))
async def cb_adm_viewuser_id(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    text, kb = user_card(uid)
    if not text:
        return await call.answer("Не найден", show_alert=True)
    await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_act_toggleban:"))
async def cb_adm_toggleban(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    u = get_user(uid)
    if not u:
        return
    if u[4]:
        unban_user(uid)
        await call.answer("✅ Разбанен")
    else:
        ban_user(uid)
        for b in get_user_bots(uid):
            await stop_user_bot(b[0])
        await call.answer("🚫 Забанен, боты остановлены", show_alert=True)
    text, kb = user_card(uid)
    if text:
        try:
            await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await cb_admin(call, None)


@dp.callback_query(F.data == "adm_msguser")
async def cb_adm_msguser(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.msg_uid)
    await call.message.edit_text("✉️ ID или @username:")


@dp.message(AdminStates.msg_uid)
async def adm_msg_uid(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    txt = (message.text or "").strip()
    uid = find_user_by_username(txt) if txt.startswith("@") else (int(txt) if txt.isdigit() else None)
    if not uid:
        return await message.answer("❌ Не найден")
    await state.update_data(target_uid=uid)
    await state.set_state(AdminStates.msg_text)
    await message.answer(f"Текст для <code>{uid}</code>:", parse_mode="HTML")


@dp.message(AdminStates.msg_text)
async def adm_msg_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    uid = (await state.get_data())["target_uid"]
    await state.clear()
    try:
        await bot.send_message(uid, f"✉️ <b>От администрации:</b>\n\n{message.text}", parse_mode="HTML")
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ {e}")


@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.broadcast)
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь или <b>перешли</b> сюда готовое сообщение — бот скопирует его "
        "пользователям <b>один-в-один</b>: медиа, подпись, форматирование и "
        "премиум-эмодзи сохраняются.\n\n"
        "• 🖼 фото, 🎥 видео, 🎵 аудио, 🎙 голос, 📄 документы, GIF, стикеры\n"
        "• 📚 альбом (несколько фото/видео одним постом) — можно\n"
        "• 😀 премиум-эмодзи и кастомные эмодзи — можно\n"
        "• 🔗 сообщение без ссылки «переслано от» (не пересылка, а копия)\n\n"
        "Сначала бот покажет <b>предпросмотр тебе</b> — как это увидят люди. "
        "Потом подтвердишь отправку.\n\n"
        "❌ /cancel — отмена",
        parse_mode="HTML")


_album_buffer: Dict[str, list] = {}
_album_tasks: Dict[str, asyncio.Task] = {}


@dp.message(AdminStates.broadcast)
async def adm_broadcast_input(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    # Альбом приходит несколькими сообщениями с общим media_group_id: собираем их
    # и отправляем как один пост, иначе разошлась бы только первая картинка.
    if message.media_group_id:
        gid = str(message.media_group_id)
        _album_buffer.setdefault(gid, []).append(message.message_id)
        if gid not in _album_tasks:
            _album_tasks[gid] = asyncio.create_task(_flush_album(gid, message.chat.id, state))
        return
    await show_broadcast_preview(message.chat.id, [message.message_id], state)


async def _flush_album(gid, chat_id, state, delay=1.5):
    try:
        await asyncio.sleep(delay)
        ids = sorted(_album_buffer.pop(gid, []))
        _album_tasks.pop(gid, None)
        if ids:
            await show_broadcast_preview(chat_id, ids, state)
    except Exception:
        logger.exception("album collect error")


async def show_broadcast_preview(chat_id, msg_ids, state):
    """Показывает владельцу копию будущей рассылки и ждёт подтверждения."""
    await state.set_state(AdminStates.broadcast)
    await state.update_data(bc_chat=chat_id, bc_ids=list(msg_ids))
    preview_ok = True
    try:
        if len(msg_ids) > 1 and hasattr(bot, "copy_messages"):
            await bot.copy_messages(chat_id, from_chat_id=chat_id, message_ids=msg_ids)
        else:
            await bot.copy_message(chat_id, from_chat_id=chat_id, message_id=msg_ids[0])
    except Exception as e:
        preview_ok = False
        logger.warning("Не удалось показать предпросмотр копией: %s", e)
        try:
            await bot.forward_messages(chat_id, from_chat_id=chat_id, message_ids=msg_ids)
        except Exception as e2:
            logger.warning("Предпросмотр пересылкой тоже не удался: %s", e2)
    total = len(get_all_users())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Разослать всем ({total})", callback_data="bc_send")],
        [InlineKeyboardButton(text="🔁 Другое сообщение", callback_data="bc_again"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")]])
    note = ("👀 <b>Предпросмотр отправлен выше</b> — именно так увидят его получатели."
            if preview_ok else
            "⚠️ <b>Предпросмотр не удался</b> (Telegram отклонил копию). Рассылка, скорее всего, тоже не пройдёт — "
            "попробуй другое сообщение.")
    await bot.send_message(
        chat_id,
        f"{note}\n\n"
        f"📦 Сообщений в посте: <b>{len(msg_ids)}</b>\n"
        f"👥 Получателей: <b>{total}</b>\n"
        f"😀 Премиум-эмодзи и медиа сохраняются при копировании.\n\n"
        "Отправляем?",
        reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "bc_again")
async def cb_bc_again(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.broadcast)
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\nПришли или перешли новое сообщение "
        "(можно альбом и премиум-эмодзи). ❌ /cancel — отмена", parse_mode="HTML")


@dp.callback_query(F.data == "bc_cancel")
async def cb_bc_cancel(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await call.answer("Отменено")
    await call.message.edit_text("❌ Рассылка отменена. Ничего не отправлено.")


@dp.callback_query(F.data == "bc_send")
async def cb_bc_send(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    data = await state.get_data()
    ids = list(data.get("bc_ids") or [])
    from_chat = data.get("bc_chat") or call.message.chat.id
    if not ids:
        return await call.answer("Сообщение потерялось — пришли его заново", show_alert=True)
    await state.clear()
    await call.answer("📢 Отправляю...")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await run_broadcast(call.message, from_chat, ids)


async def copy_broadcast_message(chat_id, from_chat, msg_ids):
    """Копирует сообщение/альбом 1-в-1. Если копия не прошла — пересылка, потом по одному."""
    if len(msg_ids) > 1 and hasattr(bot, "copy_messages"):
        try:
            await bot.copy_messages(chat_id, from_chat_id=from_chat, message_ids=msg_ids)
            return "copy"
        except TelegramBadRequest as e:
            logger.warning("copy_messages для %s: %s — отправляю по одному", chat_id, e)
    for mid in msg_ids:
        try:
            await bot.copy_message(chat_id, from_chat_id=from_chat, message_id=mid)
        except Exception as e:
            logger.warning("copy_message для %s: %s — пробую переслать", chat_id, e)
            await bot.forward_message(chat_id, from_chat_id=from_chat, message_id=mid)
    return "copy-one-by-one"


async def run_broadcast(status_message, from_chat, msg_ids):
    users = get_all_users()
    total = len(users)
    st = await status_message.answer(
        f"⏳ <b>Рассылка началась</b>\n👥 Получателей: {total}\n📦 Пост из {len(msg_ids)} сообщ.", parse_mode="HTML")
    ok = failed = 0
    problems = []
    for i, u in enumerate(users, 1):
        try:
            await copy_broadcast_message(u[0], from_chat, msg_ids)
            ok += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            # Telegram просит подождать — ждём и пробуем ещё раз, иначе потеряем человека
            await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
            try:
                await copy_broadcast_message(u[0], from_chat, msg_ids)
                ok += 1
            except Exception as e2:
                failed += 1
                problems.append((u[0], str(e2)[:80]))
        except Exception as e:
            failed += 1
            problems.append((u[0], str(e)[:80]))
            logger.warning("broadcast to %s failed: %s", u[0], e)
        if i % 25 == 0 and i < total:
            try:
                await st.edit_text(f"⏳ <b>Рассылка…</b>\n\n📨 {ok} | ❌ {failed} | 👥 из {total}", parse_mode="HTML")
            except Exception:
                pass
    summary = (f"✅ <b>Рассылка завершена</b>\n\n📨 Доставлено: <b>{ok}</b>\n"
               f"❌ Ошибок: <b>{failed}</b>\n👥 Всего: <b>{total}</b>")
    if problems:
        head = "\n\n<b>Первые ошибки:</b>\n" + "\n".join(f"• <code>{uid}</code>: {html.escape(err)}" for uid, err in problems[:5])
        summary += head
    try:
        await st.edit_text(summary, parse_mode="HTML")
    except Exception:
        await status_message.answer(summary, parse_mode="HTML")
    logger.info("Рассылка: доставлено %s, ошибок %s, всего %s", ok, failed, total)


@dp.callback_query(F.data == "adm_ban")
async def cb_adm_ban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.ban)
    await call.message.edit_text("🚫 ID или @username:")


@dp.message(AdminStates.ban)
async def adm_ban_do(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    txt = (message.text or "").strip()
    uid = find_user_by_username(txt) if txt.startswith("@") else (int(txt) if txt.isdigit() else None)
    await state.clear()
    if not uid or uid == OWNER_ID:
        return await message.answer("❌")
    ban_user(uid)
    for b in get_user_bots(uid):
        await stop_user_bot(b[0])
    await message.answer(f"🚫 <code>{uid}</code> забанен", parse_mode="HTML")


@dp.callback_query(F.data == "adm_unban")
async def cb_adm_unban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.unban)
    await call.message.edit_text("✅ ID для разбана:")


@dp.message(AdminStates.unban)
async def adm_unban_do(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    if (message.text or "").isdigit():
        unban_user(int(message.text))
        await message.answer("✅ Разбанен")
    else:
        await message.answer("❌ ID")


@dp.callback_query(F.data == "adm_addadmin")
async def cb_adm_addadmin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Только владелец", show_alert=True)
    await state.set_state(AdminStates.addadmin)
    await call.message.edit_text("🛡 ID нового админа:")


@dp.message(AdminStates.addadmin)
async def adm_addadmin_do(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.clear()
    if (message.text or "").isdigit():
        add_admin(int(message.text))
        await message.answer("🛡 Добавлен")
    else:
        await message.answer("❌")


@dp.callback_query(F.data == "adm_remadmin")
async def cb_adm_remadmin(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    admins = get_all_admins()
    kb = [[InlineKeyboardButton(text=f"❌ {a[1] or a[0]}", callback_data=f"remadm:{a[0]}")] for a in admins]
    kb.append([InlineKeyboardButton(text="« Отмена", callback_data="admin")])
    await call.message.edit_text("Удалить админа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("remadm:"))
async def cb_remadm_do(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    remove_admin(int(call.data.split(":")[1]))
    await call.answer("Удалён")
    await cb_admin(call, None)


@dp.callback_query(F.data == "adm_backup")
async def cb_adm_backup(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Только владелец", show_alert=True)
    if DB_PATH.exists():
        await call.message.answer_document(
            FSInputFile(str(DB_PATH), filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"))
        await call.answer("✅")
    else:
        await call.answer("БД нет", show_alert=True)


@dp.callback_query(F.data == "adm_restart_all")
async def cb_restart_all(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("⏳...")
    for bid in list(running_bots.keys()):
        await stop_user_bot(bid)
    n = 0
    for b in get_all_bots():
        if b[6] == 0 and not is_user_banned(b[1]) and has_active_slot(b[1]):
            if await start_user_bot(b[0]):
                n += 1
    await call.message.answer(f"🔄 Перезапущено: {n}")


# ═══════════════════════════════════════════════════════════════
# 🎯 MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    db_existed_before = DB_PATH.exists()
    init_db()
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Добавьте его в Variables/Environment.")
    if not SYNC_SECRET:
        logger.warning("SYNC_SECRET не задан — автоматическая выдача через кассира отключена.")
    acquire_instance_lock()
    note_persistence(db_existed_before)
    note_startup_time()
    logger.info("=" * 60)
    logger.info("🤖 BotHost v%s | pid=%s | db=%s", BOTHOST_VERSION, os.getpid(), DB_PATH)
    logger.info(f"👤 Владелец: {OWNER_ID}")
    logger.info(f"🔗 Кассир: @{VERIFIER_BOT_USERNAME}")
    logger.info(f"💳 Режим кассира: {INTERNAL_CASHIER_MODE} "
                f"({'кассир внутри сервиса' if INTERNAL_CASHIER_MODE in ('auto', 'on') else 'отдельный сервис'})")
    logger.info(f"📢 SYNC канал: {SYNC_CHANNEL_ID}")
    logger.info(f"📦 FILE STORAGE канал: {FILE_STORAGE_CHANNEL_ID or 'не задан'}")
    logger.info(f"⏳ Льготный период: {GRACE_HOURS} ч | напоминания за {NOTIFY_BEFORE_DAYS:g} дн.")
    logger.info("=" * 60)
    if str(DATA_DIR).startswith("/app") and os.name == "posix":
        logger.info("💡 Важно: подключи Railway Volume на %s — иначе подписки и боты исчезнут при редеплое.", DATA_DIR)

    # Сброс webhook, иначе Conflict forever
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook удалён, polling готов")
    except Exception as e:
        logger.warning(f"delete_webhook: {e}")

    await bootstrap_bot_archives()
    await restore_running_bots()
    asyncio.create_task(monitor_bots())
    asyncio.create_task(auto_backup())
    asyncio.create_task(monitor_subscriptions())

    # Кассир должен работать РОВНО в одном месте, поэтому перед запуском внутреннего
    # кассира проверяем, не опрашивает ли его токен кто-то ещё.
    cashier_task = None
    cashier_token_present = bool(os.environ.get("CASHIER_TOKEN", "").strip())
    want_internal = INTERNAL_CASHIER_MODE in ("auto", "on")
    if want_internal and cashier_token_present:
        if importlib.util.find_spec("cashier") is None:
            logger.error("❌ Внутренний кассир не запущен: рядом с bot.py нет cashier.py "
                         "(в образе должен быть файл кассира).")
        else:
            free, why = await cashier_token_is_free(os.environ.get("CASHIER_TOKEN", "").strip())
            if not free:
                logger.error("❌ Внутренний кассир НЕ запущен: %s.\n"
                             "   Кассир уже работает в другом месте (второй сервис, старая реплика или "
                             "локальный запуск) — оплата идёт через него, конфликтов не будет.\n"
                             "   Нужен кассир только здесь: останови второй экземпляр.\n"
                             "   Нужен кассир отдельным сервисом: поставь INTERNAL_CASHIER=0.", why)
            else:
                async def run_internal_cashier():
                    while True:
                        try:
                            cashier = importlib.import_module("cashier")
                            logger.info("💳 Внутренний кассир запускается в том же сервисе...")
                            await cashier.main()
                            logger.warning("⚠️ Кассир завершил polling без исключения; перезапуск через 3 сек")
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("❌ Внутренний кассир остановился; перезапуск через 5 сек")
                        await asyncio.sleep(5)

                cashier_task = asyncio.create_task(run_internal_cashier(), name="internal-cashier")
                logger.info("✅ Токен кассира свободен (%s) — внутренний кассир стартует", why)
    elif want_internal and not cashier_token_present:
        logger.warning("⚠️ Внутренний кассир не запущен: нет CASHIER_TOKEN. "
                       "Оплата пойдёт через отдельный сервис кассира, если он есть.")
    elif cashier_token_present:
        logger.info("ℹ️ CASHIER_TOKEN задан, но INTERNAL_CASHIER=0 — внутренний кассир выключен "
                    "по настройке. Это режим двух сервисов: кассир работает отдельно.")
    else:
        logger.info("ℹ️ Внутренний кассир не задан — оплата идёт через отдельный сервис кассира.")

    try:
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=["message", "callback_query", "channel_post"])
                logger.warning("⚠️ Основной polling завершился без исключения; перезапуск через 3 сек")
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except TelegramConflictError:
                logger.error(
                    "❌ TelegramConflictError: этот BOT_TOKEN прямо сейчас опрашивает другой процесс.\n"
                    "   Что проверить:\n"
                    "   1) Railway → Settings: реплик должно быть 1 (Replicas = 1);\n"
                    "   2) старый деплой/контейнер ещё жив — во время редеплоя ~30 сек "
                    "пересечения это нормально, ошибка сама проходит;\n"
                    "   3) нет ли второго сервиса (или локального запуска) с тем же BOT_TOKEN;\n"
                    "   4) CASHIER_TOKEN должен быть задан ТОЛЬКО в одном месте — либо здесь "
                    "(внутренний кассир), либо в отдельном сервисе, но не в обоих.")
                raise
            except TelegramUnauthorizedError:
                logger.exception("❌ TelegramUnauthorizedError: BOT_TOKEN недействителен")
                raise
            except Exception:
                logger.exception("❌ Основной BotHost polling упал; перезапуск через 5 сек")
                await asyncio.sleep(5)
    finally:
        if cashier_task:
            cashier_task.cancel()
            try:
                await cashier_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except TelegramConflictError:
        logger.error("❌ TelegramConflictError: этот BOT_TOKEN уже запущен другим polling-процессом. Оставьте только один экземпляр/replica и остановите старый процесс.")
        raise
    except TelegramUnauthorizedError:
        logger.error("❌ BOT_TOKEN недействителен или отозван у @BotFather.")
        raise
    except KeyboardInterrupt:
        logger.info("👋 Выход")
