"""
🤖 BotHost v8.0 Stable
✅ Исправлена ошибка Webhook (bot.delete_webhook)
✅ Починены неработающие кнопки (сброс FSM)
✅ Интеграция с ботом-кассиром через закрытый Telegram-канал
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
import hmac
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
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

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
BOTS_DIR = DATA_DIR / "bots"
DB_PATH = DATA_DIR / "bot.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BOTS_DIR.mkdir(parents=True, exist_ok=True)

PLANS = {
    "week":   {"name": "Неделя",    "stars": 15, "days": 7,  "emoji": "📅"},
    "2weeks": {"name": "2 недели",  "stars": 25, "days": 14, "emoji": "🗓"},
    "month":  {"name": "Месяц",     "stars": 50, "days": 30, "emoji": "💎"},
}

BOT_START_TIME = time.time()
logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("BotHost")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
running_bots: Dict[int, subprocess.Popen] = {}
start_locks: Dict[int, asyncio.Lock] = {}

groq_client = None
if GROQ_AVAILABLE and GROQ_API_KEY:
    try: groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    except Exception as e: logger.error(f"⚠️ Ошибка ИИ: {e}")

# ═══════════════════════════════════════════════════════════════
# 💾 БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════
def _db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def _db_retry(fn, attempts=8):
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
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            if conn:
                try: conn.rollback()
                except Exception: pass
            time.sleep(min(0.25 * (2 ** i), 3.0))
        finally:
            if conn:
                conn.close()
    raise last

def init_db():
    def setup(conn):
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_admin INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan TEXT,
                expires_at TEXT,
                created_at TEXT,
                gift_id TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                filename TEXT,
                bot_token TEXT,
                status TEXT DEFAULT 'stopped',
                created_at TEXT,
                is_frozen INTEGER DEFAULT 0,
                entry_point TEXT DEFAULT 'user_bot.py',
                auto_restart INTEGER DEFAULT 0
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                plan TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                processed_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                plan TEXT,
                uses_left INTEGER,
                created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS used_promos (
                user_id INTEGER,
                promo_id INTEGER,
                UNIQUE(user_id, promo_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS auto_grants (
                grant_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # Миграция старой базы: auto_restart
        try:
            c.execute(
                "ALTER TABLE bots ADD COLUMN auto_restart INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass

        # Миграция старой базы: env_vars
        try:
            c.execute(
                "ALTER TABLE bots ADD COLUMN env_vars TEXT NOT NULL DEFAULT '{}'"
            )
        except sqlite3.OperationalError:
            pass

    _db_retry(setup)
def get_db():
    return _db_connect()

def create_user(uid, uname, fname=""):
    def op(conn):
        conn.execute("""INSERT INTO users (user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (uid, uname, fname, datetime.now().isoformat()))
    _db_retry(op)

def get_user(uid): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM users WHERE user_id = ?", (uid,)); r = c.fetchone(); conn.close(); return r
def get_all_users(): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM users ORDER BY created_at DESC"); r = c.fetchall(); conn.close(); return r
def find_user_by_username(uname): conn = get_db(); c = conn.cursor(); c.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (uname.lstrip("@").lower(),)); r = c.fetchone(); conn.close(); return r[0] if r else None
def is_user_banned(uid): return False if uid == OWNER_ID else bool((get_user(uid) or [0,0,0,0,0])[4])
def ban_user(uid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (uid,)); conn.commit(); conn.close()
def unban_user(uid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (uid,)); conn.commit(); conn.close()
def is_admin(uid): return True if uid == OWNER_ID else bool((get_user(uid) or [0,0,0,0])[3])
def add_admin(uid): conn = get_db(); c = conn.cursor(); c.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, datetime.now().isoformat())); c.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (uid,)); conn.commit(); conn.close()
def remove_admin(uid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (uid,)); conn.commit(); conn.close()
def get_all_admins(): conn = get_db(); c = conn.cursor(); c.execute("SELECT user_id, username, full_name FROM users WHERE is_admin = 1 AND user_id != ?", (OWNER_ID,)); r = c.fetchall(); conn.close(); return r
def has_active_slot(uid):
    if is_admin(uid): return True
    conn = get_db(); c = conn.cursor(); c.execute("SELECT COUNT(*) FROM slots WHERE user_id = ? AND expires_at > ?", (uid, datetime.now().isoformat())); r = c.fetchone()[0]; conn.close(); return r > 0
def get_active_slots(uid):
    if is_admin(uid): return [(0, uid, "month", "2099-12-31", datetime.now().isoformat(), "admin")]
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM slots WHERE user_id = ? AND expires_at > ?", (uid, datetime.now().isoformat())); r = c.fetchall(); conn.close(); return r
def create_slot(uid, plan):
    exp = datetime.now() + timedelta(days=PLANS[plan]["days"])
    conn = get_db(); c = conn.cursor(); c.execute("INSERT INTO slots (user_id, plan, expires_at, created_at) VALUES (?, ?, ?, ?)", (uid, plan, exp.isoformat(), datetime.now().isoformat())); conn.commit(); conn.close()
def save_bot(uid, fname, token, ep="user_bot.py"): conn = get_db(); c = conn.cursor(); c.execute("INSERT INTO bots (user_id, filename, bot_token, created_at, entry_point) VALUES (?, ?, ?, ?, ?)", (uid, fname, token, datetime.now().isoformat(), ep)); bid = c.lastrowid; conn.commit(); conn.close(); return bid
def get_user_bots(uid): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM bots WHERE user_id = ?", (uid,)); r = c.fetchall(); conn.close(); return r
def get_bot(bid): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM bots WHERE id = ?", (bid,)); r = c.fetchone(); conn.close(); return r
def get_all_bots(): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM bots"); r = c.fetchall(); conn.close(); return r
def update_bot_entry(bid, ep): conn = get_db(); c = conn.cursor(); c.execute("UPDATE bots SET entry_point = ? WHERE id = ?", (ep, bid)); conn.commit(); conn.close()
def toggle_auto_restart(bid, val): conn = get_db(); c = conn.cursor(); c.execute("UPDATE bots SET auto_restart = ? WHERE id = ?", (val, bid)); conn.commit(); conn.close()
def get_bot_env(bid):
    b = get_bot(bid)
    if not b or len(b) < 10: return {}
    try: return json.loads(b[9] or "{}")
    except Exception: return {}

def set_bot_env(bid, env_vars):
    payload = json.dumps(env_vars, ensure_ascii=False)
    _db_retry(lambda conn: conn.execute("UPDATE bots SET env_vars = ? WHERE id = ?", (payload, bid)))

def freeze_bot(bid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE bots SET is_frozen = 1 WHERE id = ?", (bid,)); conn.commit(); conn.close()
def unfreeze_bot(bid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE bots SET is_frozen = 0 WHERE id = ?", (bid,)); conn.commit(); conn.close()
def delete_bot_record(bid): conn = get_db(); c = conn.cursor(); c.execute("DELETE FROM bots WHERE id = ?", (bid,)); conn.commit(); conn.close()
def create_payment_request(uid, uname, fname, plan): conn = get_db(); c = conn.cursor(); c.execute("INSERT INTO payment_requests (user_id, username, full_name, plan, created_at) VALUES (?, ?, ?, ?, ?)", (uid, uname, fname, plan, datetime.now().isoformat())); rid = c.lastrowid; conn.commit(); conn.close(); return rid
def get_pending_requests(): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY created_at ASC"); r = c.fetchall(); conn.close(); return r
def get_payment_request(rid): conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM payment_requests WHERE id = ?", (rid,)); r = c.fetchone(); conn.close(); return r
def approve_payment(rid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE payment_requests SET status = 'approved', processed_at = ? WHERE id = ?", (datetime.now().isoformat(), rid)); conn.commit(); conn.close()
def reject_payment(rid): conn = get_db(); c = conn.cursor(); c.execute("UPDATE payment_requests SET status = 'rejected', processed_at = ? WHERE id = ?", (datetime.now().isoformat(), rid)); conn.commit(); conn.close()
def user_has_pending_request(uid): conn = get_db(); c = conn.cursor(); c.execute("SELECT COUNT(*) FROM payment_requests WHERE user_id = ? AND status = 'pending'", (uid,)); r = c.fetchone()[0]; conn.close(); return r > 0
def create_promo(code, plan, uses):
    try: conn = get_db(); c = conn.cursor(); c.execute("INSERT INTO promocodes (code, plan, uses_left, created_at) VALUES (?, ?, ?, ?)", (code, plan, uses, datetime.now().isoformat())); conn.commit(); conn.close(); return True
    except: return False
def get_all_promos(): conn = get_db(); c = conn.cursor(); c.execute("SELECT id, code, plan, uses_left FROM promocodes WHERE uses_left > 0"); r = c.fetchall(); conn.close(); return r
def delete_promo(pid): conn = get_db(); c = conn.cursor(); c.execute("DELETE FROM promocodes WHERE id = ?", (pid,)); conn.commit(); conn.close()
def use_promo(uid, code):
    conn = get_db(); c = conn.cursor(); c.execute("SELECT id, plan, uses_left FROM promocodes WHERE code = ?", (code,)); p = c.fetchone()
    if not p or p[2] <= 0: conn.close(); return False, "❌ Промокод не найден или закончился."
    c.execute("SELECT 1 FROM used_promos WHERE user_id = ? AND promo_id = ?", (uid, p[0]))
    if c.fetchone(): conn.close(); return False, "⚠️ Ты уже использовал этот промокод."
    c.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE id = ?", (p[0],)); c.execute("INSERT INTO used_promos (user_id, promo_id) VALUES (?, ?)", (uid, p[0])); conn.commit(); conn.close(); create_slot(uid, p[1]); return True, p[1]

def get_stats():
    conn = get_db(); c = conn.cursor()
    r = {
        "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "banned": c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0],
        "admins": c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0],
        "slots": c.execute("SELECT COUNT(*) FROM slots WHERE expires_at > ?", (datetime.now().isoformat(),)).fetchone()[0],
        "bots": c.execute("SELECT COUNT(*) FROM bots").fetchone()[0],
        "frozen": c.execute("SELECT COUNT(*) FROM bots WHERE is_frozen = 1").fetchone()[0],
        "pending": c.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'pending'").fetchone()[0],
        "running": len(running_bots)
    }
    conn.close(); return r

# ═══════════════════════════════════════════════════════════════
# 🛡 ГЛОБАЛЬНЫЙ БАН
# ═══════════════════════════════════════════════════════════════
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and is_user_banned(u.id):
            if isinstance(event, types.Message): await event.answer("🚫 <b>Вы заблокированы на хостинге.</b>", parse_mode="HTML")
            elif isinstance(event, types.CallbackQuery): await event.answer("🚫 Вы заблокированы", show_alert=True)
            return
        return await handler(event, data)
dp.message.middleware(BanMiddleware()); dp.callback_query.middleware(BanMiddleware())

# ═══════════════════════════════════════════════════════════════
# 🚀 ОБЁРТКА И ЗАПУСК
# ═══════════════════════════════════════════════════════════════
WRAPPER_CODE = '''#!/usr/bin/env python3
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
IMPORT_TO_PACKAGE={"PIL":"Pillow","cv2":"opencv-python","bs4":"beautifulsoup4","dotenv":"python-dotenv","yaml":"PyYAML","Crypto":"pycryptodome","dateutil":"python-dateutil","jwt":"PyJWT","multipart":"python-multipart","fitz":"PyMuPDF","openai":"openai","groq":"groq","aiogram":"aiogram","discord":"discord.py","requests":"requests","aiohttp":"aiohttp","flask":"flask","fastapi":"fastapi","uvicorn":"uvicorn","pydantic":"pydantic","sqlalchemy":"sqlalchemy","redis":"redis","pymongo":"pymongo"}
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

def write_wrapper(bot_dir, entry_point): (bot_dir / "wrapper.py").write_text(WRAPPER_CODE.replace("{{ENTRY_POINT}}", entry_point), encoding="utf-8")

async def start_user_bot(bot_id):
    lock=start_locks.setdefault(bot_id,asyncio.Lock())
    async with lock:
        try:
            b=get_bot(bot_id)
            if not b or b[6]==1: return False
            current=running_bots.get(bot_id)
            if current and current.poll() is None: return True
            bot_dir=BOTS_DIR/f"bot_{bot_id}"; bot_dir.mkdir(parents=True,exist_ok=True)
            ep=b[7] if len(b)>7 and b[7] else "user_bot.py"; write_wrapper(bot_dir,ep)
            log_file=bot_dir/"bot.log"
            with open(log_file,"a",encoding="utf-8") as f: f.write(f"\n[{datetime.now().strftime('%d.%m %H:%M:%S')}] === ЗАПУСК БОТА #{bot_id} ===\n")
            env=os.environ.copy()
            for key in list(env):
                if key.startswith("RAILWAY_") or key in {"BOT_TOKEN","CASHIER_TOKEN","GROQ_API_KEY","SYNC_CHANNEL_ID","SYNC_SECRET","OWNER_ID","OWNER_USERNAME","VERIFIER_BOT","DATABASE_URL","DATA_DIR","BOTHOST_USER_ENV","BOTHOST_BOT_TOKEN"}: env.pop(key,None)
            user_env=get_bot_env(bot_id); env.update({str(k):str(v) for k,v in user_env.items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",str(k))})
            if b[3]: env["BOTHOST_BOT_TOKEN"]=b[3]
            env["BOTHOST_USER_ENV"]=json.dumps(user_env,ensure_ascii=False); env["PYTHONUNBUFFERED"]="1"
            lf=open(log_file,"a",encoding="utf-8")
            proc=subprocess.Popen([sys.executable,"-u","wrapper.py"],cwd=str(bot_dir),env=env,stdout=lf,stderr=subprocess.STDOUT,start_new_session=True)
            running_bots[bot_id]=proc; await asyncio.sleep(4)
            if proc.poll() is not None:
                if running_bots.get(bot_id) is proc: running_bots.pop(bot_id,None)
                _db_retry(lambda conn: conn.execute("UPDATE bots SET status='error' WHERE id=?",(bot_id,)))
                return False
            _db_retry(lambda conn: conn.execute("UPDATE bots SET status='running' WHERE id=?",(bot_id,)))
            return True
        except Exception as e:
            logger.exception("Не удалось запустить бот #%s: %s",bot_id,e); return False

async def stop_user_bot(bot_id):
    lock=start_locks.setdefault(bot_id,asyncio.Lock())
    async with lock:
        proc=running_bots.pop(bot_id,None)
        if proc:
            try: os.killpg(os.getpgid(proc.pid),signal.SIGTERM); proc.wait(timeout=3)
            except Exception:
                try: os.killpg(os.getpgid(proc.pid),signal.SIGKILL)
                except Exception: pass
        try: _db_retry(lambda conn: conn.execute("UPDATE bots SET status='stopped' WHERE id=?",(bot_id,)))
        except Exception as e: logger.warning("Не удалось обновить статус бота #%s: %s",bot_id,e)


def get_bot_logs(bot_id, lines=50):
    lf = BOTS_DIR / f"bot_{bot_id}" / "bot.log"
    if not lf.exists(): return ""
    try: content = lf.read_text(encoding="utf-8", errors="ignore").strip(); return "\n".join(content.split("\n")[-lines:]) if content else ""
    except Exception as e: return f"Ошибка чтения логов: {e}"

def list_bot_files(bot_id):
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    if not bot_dir.exists(): return []
    files = []
    for root, dirs, fs in os.walk(bot_dir):
        dirs[:] = [d for d in dirs if d not in {".venv", "__pycache__", ".git"}]
        for f in fs:
            if f in ("wrapper.py", "bot.log", ".requirements.installed"): continue
            files.append((str(Path(root) / f).replace(str(bot_dir)+"/", ""), (Path(root) / f).stat().st_size))
    return files

async def monitor_bots():
    while True:
        try:
            for bot_id, proc in list(running_bots.items()):
                if proc.poll() is not None:
                    code = proc.returncode
                    if running_bots.get(bot_id) is proc: running_bots.pop(bot_id,None)
                    conn = get_db(); c = conn.cursor()
                    c.execute("UPDATE bots SET status = 'error' WHERE id = ?", (bot_id,))
                    c.execute("SELECT user_id, auto_restart FROM bots WHERE id = ?", (bot_id,))
                    row = c.fetchone(); conn.commit(); conn.close()
                    if row:
                        uid, auto_restart = row
                        if auto_restart == 1 and not is_user_banned(uid):
                            try: await bot.send_message(uid, f"⚠️ <b>Бот #{bot_id} упал!</b>\n🔄 <i>Авто-рестарт включён, пытаюсь поднять...</i>", parse_mode="HTML")
                            except: pass
                            await asyncio.sleep(5); await start_user_bot(bot_id)
                        else:
                            if uid != OWNER_ID:
                                logs = get_bot_logs(bot_id, 15)
                                try: await bot.send_message(uid, f"⚠️ <b>Бот #{bot_id} упал!</b>\nКод: {code}\n<pre>{html.escape(logs[-500:])}</pre>", parse_mode="HTML")
                                except: pass
                else:
                    b = get_bot(bot_id)
                    if b and (b[6] == 1 or is_user_banned(b[1]) or (b[1] != OWNER_ID and not is_admin(b[1]) and not has_active_slot(b[1]))):
                        await stop_user_bot(bot_id)
        except: pass
        await asyncio.sleep(20)

async def restore_running_bots():
    conn = get_db(); c = conn.cursor(); c.execute("SELECT id, user_id, is_frozen FROM bots WHERE status = 'running'"); bots = c.fetchall(); conn.close()
    for bid, uid, frozen in bots:
        if not frozen and not is_user_banned(uid) and (uid == OWNER_ID or is_admin(uid) or has_active_slot(uid)): await start_user_bot(bid)

async def auto_backup():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            if os.path.exists(DB_PATH): await bot.send_document(OWNER_ID, FSInputFile(DB_PATH, filename=f"bothost_backup_{datetime.now().strftime('%Y%m%d')}.db"), caption="🔄 Автоматический суточный бэкап базы данных.")
        except: pass

async def analyze_with_groq(logs):
    if not groq_client: return "❌ **AI-дебаггер недоступен.**\nОтсутствует ключ `GROQ_API_KEY`."
    if not logs.strip(): return "📭 Логи пустые, нечего анализировать."
    try:
        response = await groq_client.chat.completions.create(
            messages=[{"role": "system", "content": "Ты опытный Python разработчик BotHost. Найди ошибку в логах. Отвечай кратко на русском в HTML: <b>❓ Проблема:</b> ... <b>📍 Где:</b> ... <b>💡 Решение:</b> ..."}, {"role": "user", "content": f"Лог:\n\n{logs[-2500:]}"}],
            model="qwen/qwen3.8-27b", temperature=0.2, max_completion_tokens=350, reasoning_effort="none"
        )
        return response.choices[0].message.content
    except Exception as e: return f"❌ Ошибка нейросети: {e}" # ═══════════════════════════════════════════════════════════════
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
    promo_uses = State()
    search_bot = State()

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

def main_menu_kb(uid):
    buttons = [
        [InlineKeyboardButton(text="💳 Тарифы", callback_data="buy"),
         InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_enter")],
        [InlineKeyboardButton(text="➕ Новый бот", callback_data="upload")],
        [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots"),
         InlineKeyboardButton(text="📦 Моя подписка", callback_data="myslots")],
        [InlineKeyboardButton(text="ℹ️ Как это работает", callback_data="help")]
    ]
    if is_admin(uid):
        buttons.append([InlineKeyboardButton(text="🛠 Администрирование", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_uptime():
    sec = int(time.time() - BOT_START_TIME)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f"{d}д {h}ч {m}м"

# ═══════════════════════════════════════════════════════════════
# 🔗 СВЯЗЬ С ИИ-КАССИРОМ ЧЕРЕЗ КАНАЛ
# Кассир пишет в канал: /auto_grant USER_ID PLAN GRANT_ID SIGNATURE
# Хост проверяет подпись и выдаёт слот
# ═══════════════════════════════════════════════════════════════

@dp.channel_post(F.text.startswith("/payment_request"))
async def channel_payment_request(message: types.Message):
    try:
        parts=message.text.strip().split()
        if len(parts)<5 or not SYNC_SECRET: return
        uid=int(parts[1]); plan=parts[2]; username=parts[3]; sig=parts[4]
        expected=hmac.new(SYNC_SECRET.encode(),f"request:{uid}:{plan}:{username}".encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected) or plan not in PLANS: return
        create_user(uid, username, "")
        if not user_has_pending_request(uid):
            create_payment_request(uid, username, "", plan)
        logger.info("Payment request received from cashier: user=%s plan=%s", uid, plan)
    except Exception: pass

@dp.channel_post(F.text.startswith("/payment_result"))
async def channel_payment_result(message: types.Message):
    try:
        parts=message.text.strip().split()
        if len(parts)<6 or not SYNC_SECRET: return
        uid=int(parts[1]); plan=parts[2]; result=parts[3].upper(); grant_id=parts[4]; sig=parts[5]
        expected=hmac.new(SYNC_SECRET.encode(),f"result:{uid}:{plan}:{result}:{grant_id}".encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,expected) or plan not in PLANS: return
        if result=="APPROVED":
            def grant(conn):
                if conn.execute("SELECT 1 FROM auto_grants WHERE grant_id=?",(grant_id,)).fetchone(): return False
                conn.execute("INSERT INTO auto_grants(grant_id,user_id,plan,created_at) VALUES(?,?,?,?)",(grant_id,uid,plan,datetime.now().isoformat()))
                conn.execute("INSERT INTO users(user_id,username,full_name,created_at) VALUES(?,'','',?) ON CONFLICT(user_id) DO NOTHING",(uid,datetime.now().isoformat()))
                exp=datetime.now()+timedelta(days=PLANS[plan]["days"]); conn.execute("INSERT INTO slots(user_id,plan,expires_at,created_at) VALUES(?,?,?,?)",(uid,plan,exp.isoformat(),datetime.now().isoformat())); return True
            if _db_retry(grant):
                try: await bot.send_message(uid,"🎉 <b>Оплата подтверждена!</b>\nСлот активирован.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Новый бот",callback_data="upload")],[InlineKeyboardButton(text="🤖 Мои проекты",callback_data="mybots")]]),parse_mode="HTML")
                except Exception: pass
        elif result=="REJECTED":
            try: await bot.send_message(uid,"❌ <b>Оплата не подтверждена.</b>\nОтправь два чётких скриншота повторно через кассира.",parse_mode="HTML")
            except Exception: pass
    except Exception: logger.exception("payment_result error")

@dp.channel_post(F.text.startswith("/auto_grant"))
async def channel_auto_grant(message: types.Message):
    """Выдача слота от кассира: /auto_grant USER_ID PLAN GRANT_ID SIGNATURE."""
    try:
        parts = message.text.strip().split()
        if len(parts) < 5 or not SYNC_SECRET:
            logger.warning("Отклонён auto_grant: неверный формат или не задан SYNC_SECRET")
            return
        uid = int(parts[1])
        plan = parts[2]
        grant_id = parts[3]
        signature = parts[4] if len(parts) > 4 else ""
        expected_signature = hmac.new(
            SYNC_SECRET.encode(), f"{uid}:{plan}:{grant_id}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature) or plan not in PLANS or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", grant_id):
            logger.warning("Отклонён auto_grant: проверка подписи не пройдена")
            return
        def grant_once(conn):
            row = conn.execute("SELECT 1 FROM auto_grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row:
                return False
            conn.execute("INSERT INTO auto_grants(grant_id,user_id,plan,created_at) VALUES(?,?,?,?)",
                         (grant_id, uid, plan, datetime.now().isoformat()))
            conn.execute("INSERT INTO users(user_id, username, full_name, created_at) VALUES(?, '', '', ?) ON CONFLICT(user_id) DO NOTHING",
                         (uid, datetime.now().isoformat()))
            exp = datetime.now() + timedelta(days=PLANS[plan]["days"])
            conn.execute("INSERT INTO slots(user_id, plan, expires_at, created_at) VALUES(?,?,?,?)",
                         (uid, plan, exp.isoformat(), datetime.now().isoformat()))
            return True
        created = _db_retry(grant_once)
        if not created:
            logger.info("Повторный auto_grant %s проигнорирован", grant_id)
            return
        plan_info = PLANS[plan]
        try:
            await bot.send_message(
                uid,
                "🎉 <b>Оплата подтверждена!</b>\n\n"
                f"💎 Тариф <b>{plan_info['name']}</b> · {plan_info['days']} дней\n"
                "🚀 Слот уже активен — можно загружать проект.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📤 Загрузить проект", callback_data="upload")],
                    [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots")],
                    [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")]
                ]),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning("Не удалось уведомить user %s: %s", uid, e)
        try:
            await message.reply(f"✅ Слот выдан · <code>{uid}</code> · {html.escape(plan_info['name'])}", parse_mode="HTML")
        except Exception:
            pass
        logger.info("Auto-grant: user=%s plan=%s grant=%s", uid, plan, grant_id)
    except Exception as e:
        logger.exception("auto_grant error: %s", e)

# ═══════════════════════════════════════════════════════════════
# 🎯 СТАРТ / МЕНЮ
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    create_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    name = html.escape(message.from_user.first_name or "друг")
    text = (
        f"👋 <b>Привет, {name}!</b>\n\n"
        f"Добро пожаловать в <b>BotHost</b> — загрузил проект, настроил переменные и запускаешь. 🚀\n\n"
        f"<b>Что умеет хостинг:</b>\n"
        f"• 📦 .py и .zip проекты\n"
        f"• 📚 автоматическая установка библиотек\n"
        f"• 🔄 авто-рестарт и контроль состояния\n"
        f"• 📄 логи + 🧠 ИИ-помощник\n"
        f"• ⚙️ переменные окружения без .env-файлов\n\n"
        f"Выбери действие ниже — дальше всё делается кнопками."
    )
    await message.answer(text, reply_markup=main_menu_kb(message.from_user.id), parse_mode="HTML")

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb(message.from_user.id))

@dp.callback_query(F.data == "back_main")
async def back_main(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text(
            "🏠 <b>BotHost</b>",
            reply_markup=main_menu_kb(call.from_user.id),
            parse_mode="HTML"
        )
    except Exception:
        await call.message.answer(
            "🏠 <b>BotHost</b>",
            reply_markup=main_menu_kb(call.from_user.id),
            parse_mode="HTML"
        )

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

def find_entry_point(bot_dir: Path) -> str:
    candidates = []
    preferred = {"main.py": 0, "bot.py": 1, "app.py": 2, "run.py": 3, "start.py": 4, "user_bot.py": 5}
    for p in bot_dir.rglob("*.py"):
        if p.name in preferred and p.name != "wrapper.py":
            rel = p.relative_to(bot_dir)
            candidates.append((preferred[p.name], len(rel.parts), str(rel)))
    if not candidates:
        return "user_bot.py"
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][2]

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
                await message.answer(f"✅ Архив распакован в бота #{target_bid}!", parse_mode="HTML")
            else:
                await bot.download_file(finfo.file_path, destination=bot_dir / fname)
                await message.answer(f"✅ Файл <code>{html.escape(fname)}</code> добавлен в бота #{target_bid}!", parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
        await state.clear()
        return

    # Восстановление БД владельцем
    if uid == OWNER_ID and ext == "db" and curr is None:
        try:
            finfo = await bot.get_file(doc.file_id)
            await bot.download_file(finfo.file_path, destination=DB_PATH)
            return await message.answer("✅ <b>База данных восстановлена!</b>", parse_mode="HTML")
        except Exception as e:
            return await message.answer(f"❌ Ошибка: {e}")

    # Новый бот
    if not has_active_slot(uid):
        return await message.answer(
            "❌ <b>Нет активного слота!</b>\nКупи слот или введи промокод.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить", callback_data="buy"),
                 InlineKeyboardButton(text="🎁 Промокод", callback_data="promo_enter")]
            ]),
            parse_mode="HTML"
        )
    if ext not in ("py", "zip"):
        return await message.answer("❌ Только <b>.py</b> или <b>.zip</b>", parse_mode="HTML")

    await state.update_data(file_id=doc.file_id, fname=fname, ext=ext)
    await state.set_state(UploadStates.waiting_token)
    await message.answer(
        f"✅ <b>Файл {html.escape(fname)} получен!</b>\n\n"
        f"Отправь <b>токен</b> от @BotFather\n"
        f"или напиши <code>none</code>",
        parse_mode="HTML"
    )

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
        ep = "user_bot.py"
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
            ep = find_entry_point(bot_dir)
        else:
            dpath.rename(bot_dir / "user_bot.py")

        update_bot_entry(bid, ep)
        write_wrapper(bot_dir, ep)
        await msg.edit_text(
            f"✅ <b>Бот #{bid} развёрнут!</b>\n"
            f"🚀 Точка входа: <code>{html.escape(ep)}</code>\n\n"
            f"Зайди в «🤖 Мои проекты» → ▶️ Запуск",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Мои проекты", callback_data="mybots")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
    await state.clear()

@dp.message(UploadStates.waiting_token)
async def handle_token_wrong(message: types.Message):
    await message.answer("⚠️ Отправь токен <b>текстом</b> или <code>none</code>", parse_mode="HTML")

@dp.callback_query(F.data == "upload")
async def cb_upload(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not has_active_slot(call.from_user.id):
        return await call.answer("❌ Нет активного слота", show_alert=True)
    await state.set_state(UploadStates.waiting_file)
    await call.message.edit_text(
        "📤 <b>Загрузка проекта</b>\n\n"
        "Отправь <b>.py</b> или <b>.zip</b>\n\n"
        "📦 Для архивов: авто-поиск main.py + requirements.txt\n"
        "💡 Лимит: 20 МБ\n\n❌ /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════
# 💎 ОПЛАТА + СЛОТЫ + ПРОМО
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "buy")
async def cb_buy(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if is_admin(call.from_user.id):
        return await call.message.edit_text(
            "👑 <b>У тебя безлимит!</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
    text = "💎 <b>Выбери тариф</b>\n\nОплата подарком (⭐) владельцу"
    kb = [
        [InlineKeyboardButton(text=f"📅 Неделя — {PLANS['week']['stars']}⭐", callback_data="plan:week")],
        [InlineKeyboardButton(text=f"🗓 2 недели — {PLANS['2weeks']['stars']}⭐", callback_data="plan:2weeks")],
        [InlineKeyboardButton(text=f"💎 Месяц — {PLANS['month']['stars']}⭐", callback_data="plan:month")],
        [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    pid = call.data.split(":")[1]
    p = PLANS[pid]
    text = (
        f"{p['emoji']} <b>Тариф: {p['name']}</b>\n\n"
        f"💎 {p['stars']}⭐  •  📅 {p['days']} дней\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Как оплатить:</b>\n"
        f"1️⃣ «🎁 Отправить подарок» → владельцу\n"
        f"2️⃣ Отправь подарок от <b>{p['stars']}⭐</b>\n"
        f"3️⃣ Вернись и нажми «✅ Я оплатил»\n\n"
        f"⚡ Или <b>мгновенно через ИИ-кассира</b> — скинь скрин, слот сразу!"
    )
    kb = [
        [InlineKeyboardButton(text="🎁 Отправить подарок", url=get_profile_link())],
        [InlineKeyboardButton(
            text="⚡ Оплатить через ИИ-кассира",
            url=f"https://t.me/{VERIFIER_BOT_USERNAME}?start={pid}"
        )],
        [InlineKeyboardButton(text="✅ Я оплатил (ручная проверка)", callback_data=f"pay_done:{pid}")],
        [InlineKeyboardButton(text="« Тарифы", callback_data="buy")]
    ]
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query(F.data.startswith("pay_done:"))
async def cb_pay_done(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    pid = call.data.split(":")[1]
    p = PLANS[pid]
    u = call.from_user
    if user_has_pending_request(u.id):
        return await call.answer("⏳ Уже есть заявка на проверке!", show_alert=True)
    rid = create_payment_request(u.id, u.username or "", u.full_name or "", pid)
    await call.message.edit_text(
        f"✅ <b>Заявка #{rid} отправлена!</b>\n\n"
        f"{p['emoji']} {p['name']} ({p['stars']}⭐)\n"
        f"⏳ Админ проверит оплату.\n\n"
        f"💡 Быстрее: оплати через ИИ-кассира в тарифах!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )
    try:
        kb_adm = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{rid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{rid}")
        ]])
        await bot.send_message(
            OWNER_ID,
            f"💰 <b>Заявка #{rid}</b>\n\n👤 @{u.username or '—'} (<code>{u.id}</code>)\n"
            f"{p['emoji']} <b>{p['name']}</b> ({p['stars']}⭐)",
            reply_markup=kb_adm,
            parse_mode="HTML"
        )
    except Exception:
        pass

@dp.callback_query(F.data == "myslots")
async def cb_myslots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if is_admin(call.from_user.id):
        return await call.message.edit_text(
            "👑 <b>Безлимит</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
    slots = get_active_slots(call.from_user.id)
    if not slots:
        return await call.message.edit_text(
            "💳 <b>Нет активных слотов</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить", callback_data="buy")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
    text = f"📊 <b>Слоты ({len(slots)})</b>\n\n"
    for s in slots:
        exp = datetime.fromisoformat(s[3])
        left = (exp - datetime.now()).days
        text += f"• <b>{PLANS.get(s[2], {}).get('name', s[2])}</b> — ещё {left} дн.\n"
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить ещё", callback_data="buy")],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "promo_enter")
async def cb_promo_enter(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.enter_promo)
    await call.message.edit_text(
        "🎟 <b>Ввод промокода</b>\n\nОтправь код сообщением:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )

@dp.message(UserStates.enter_promo)
async def process_promo(message: types.Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    ok, res = use_promo(message.from_user.id, code)
    if ok:
        p = PLANS[res]
        await message.answer(
            f"🎉 <b>Промокод активирован!</b>\n\n"
            f"Тариф: {p['emoji']} <b>{p['name']}</b> ({p['days']} дн.)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Новый бот", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ])
        )
    else:
        await message.answer(
            res,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ])
        )
    await state.clear()

@dp.callback_query(F.data == "help")
async def cb_help(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "❓ <b>Помощь</b>\n\n"
        "<b>🎁 Как начать:</b>\n"
        "1. Купи слот или введи промокод\n"
        "2. При покупке — подарок владельцу или ИИ-кассир\n"
        "3. Отправь .py / .zip → токен → Запуск\n\n"
        "<b>⚡ ИИ-кассир:</b> в тарифах кнопка «Оплатить через ИИ» — "
        "скинь скрин перевода, слот выдаётся сразу.\n\n"
        "<b>📁 Файлы:</b> Мои боты → бот → Файлы / Добавить файл"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Владелец", url=get_profile_link())],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

# ═══════════════════════════════════════════════════════════════
# 🤖 МОИ БОТЫ / УПРАВЛЕНИЕ
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "mybots")
async def cb_mybots(call: types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    bots = get_user_bots(call.from_user.id)
    if not bots:
        return await call.message.edit_text(
            "🤖 <b>Нет ботов</b>\n\nЗагрузи первого!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
    kb = []
    for b in bots:
        st = "🟢" if b[0] in running_bots else ("🧊" if b[6] else "🔴")
        name = (b[2] or "?")[:22]
        kb.append([InlineKeyboardButton(text=f"{st} #{b[0]} • {name}", callback_data=f"bot:{b[0]}")])
    kb.append([InlineKeyboardButton(text="📤 Загрузить ещё", callback_data="upload")])
    kb.append([InlineKeyboardButton(text="« Меню", callback_data="back_main")])
    await call.message.edit_text(
        f"🤖 <b>Твои боты ({len(bots)})</b>\n🟢 работает • 🔴 стоп • 🧊 заморожен",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML"
    )

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

    kb = [
        [InlineKeyboardButton(text="▶️ Запуск", callback_data=f"start:{bid}"),
         InlineKeyboardButton(text="⏹ Стоп", callback_data=f"stop:{bid}")],
        [InlineKeyboardButton(text="🔄 Перезапуск", callback_data=f"restart:{bid}"),
         InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bid}")],
        [InlineKeyboardButton(text=f"🔄 Авто-рестарт: {auto_r}", callback_data=f"toggle_restart:{bid}")],
        [InlineKeyboardButton(text="📁 Файлы", callback_data=f"files:{bid}"),
         InlineKeyboardButton(text="⚙️ Переменные", callback_data=f"envmenu:{bid}")],
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
        f"📊 {status}"
    )
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

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
    await call.answer("⏳ Запуск...")
    ok = await start_user_bot(bid)
    if ok:
        await call.message.answer(f"✅ Бот #{bid} запущен!", parse_mode="HTML")
    else:
        logs = html.escape(get_bot_logs(bid, 30)[-1500:] or "пусто")
        await call.message.answer(
            f"❌ Бот #{bid} не запустился\n<pre>{logs}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bid}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]
            ])
        )

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
    if state: await state.clear()
    bid=int(call.data.split(":")[1]); b=get_bot(bid)
    if not b or (b[1]!=call.from_user.id and not is_admin(call.from_user.id)): return await call.answer("❌ Нет доступа",show_alert=True)
    envs=get_bot_env(bid); names=", ".join(envs.keys()) if envs else "пока не заданы"
    await call.message.edit_text(f"⚙️ <b>Переменные · проект #{bid}</b>\n\nБез .env-файлов. Значения передаются процессу только при запуске.\n\nКлючи: <code>{html.escape(names[:800])}</code>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить переменные",callback_data=f"editenv:{bid}")],[InlineKeyboardButton(text="🧹 Очистить",callback_data=f"clearenv:{bid}")],[InlineKeyboardButton(text="« К проекту",callback_data=f"bot:{bid}")]]),parse_mode="HTML")

@dp.callback_query(F.data.startswith("editenv:"))
async def cb_editenv(call: types.CallbackQuery,state:FSMContext):
    bid=int(call.data.split(":")[1]); b=get_bot(bid)
    if not b or (b[1]!=call.from_user.id and not is_admin(call.from_user.id)): return await call.answer("❌ Нет доступа",show_alert=True)
    curr=get_bot_env(bid); lines="\n".join(f"{k}={v}" for k,v in curr.items())
    await state.set_state(EnvStates.waiting_env_text); await state.update_data(target_bid=bid)
    await call.message.edit_text(f"✏️ <b>Переменные проекта #{bid}</b>\n\n<pre>{html.escape(lines[:3000] or 'пусто')}</pre>\n\nОтправь строки <code>KEY=VALUE</code>, каждая с новой строки.\n/cancel — отмена",parse_mode="HTML")

@dp.callback_query(F.data.startswith("clearenv:"))
async def cb_clearenv(call: types.CallbackQuery):
    bid=int(call.data.split(":")[1]); b=get_bot(bid)
    if not b or (b[1]!=call.from_user.id and not is_admin(call.from_user.id)): return await call.answer("❌ Нет доступа",show_alert=True)
    set_bot_env(bid,{}); await call.answer("Переменные очищены"); await cb_envmenu(call,None)

@dp.message(EnvStates.waiting_env_text)
async def env_saved(message: types.Message,state:FSMContext):
    data=await state.get_data(); bid=data["target_bid"]; parsed={}
    for raw in (message.text or "").splitlines():
        line=raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k,v=line.split("=",1); k=k.strip(); v=v.strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",k): parsed[k]=v
    set_bot_env(bid,parsed); await state.clear()
    await message.answer("✅ Переменные сохранены. Перезапусти проект, чтобы применить их.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« К проекту",callback_data=f"bot:{bid}")]]))

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
            [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]
        ])
    )

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
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")]
            ]),
            parse_mode="HTML"
        )
    text = f"📁 <b>Файлы #{bid}</b> ({len(files)})\n\n"
    kb = []
    for i, (fn, sz) in enumerate(files[:15]):
        text += f"• <code>{html.escape(fn)}</code> ({sz/1024:.1f} KB)\n"
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
            [InlineKeyboardButton(text="« Отмена", callback_data=f"bot:{bid}")]
        ]),
        parse_mode="HTML"
    )

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
        await call.answer("🗑 Удалено")
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
    await call.answer("🗑 Удалён")
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
        f"👥 {s['users']} | 🚫 {s['banned']} | 🛡 {s['admins']}\n"
        f"💳 Слотов: {s['slots']}\n"
        f"🤖 Ботов: {s['bots']} (🟢 {s['running']} | 🧊 {s['frozen']})\n"
        f"💰 Заявок: {s['pending']}\n"
        f"⏱ {get_uptime()}"
    )
    kb = [
        [InlineKeyboardButton(text=pay_btn, callback_data="adm_payments"),
         InlineKeyboardButton(text="🎁 Промокоды", callback_data="adm_promos")],
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
    await call.message.answer(
        f"✅ Очистка:\nПапок: {del_folders}\nЗаписей БД: {del_db}",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "adm_searchbot")
async def cb_adm_searchbot(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.search_bot)
    await call.message.edit_text(
        "🔍 Введи ID бота:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="admin")]
        ])
    )

@dp.message(AdminStates.search_bot)
async def adm_searchbot_do(message: types.Message, state: FSMContext):
    if not (message.text or "").isdigit():
        return await message.answer("❌ Нужно число (ID)")
    bid = int(message.text)
    if not get_bot(bid):
        return await message.answer("❌ Бот не найден")
    await state.clear()
    # эмулируем callback bot:ID
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
                [InlineKeyboardButton(text="« Админка", callback_data="admin")]
            ])
        )
    text = f"💰 <b>Заявки ({len(reqs)})</b>\n\n"
    kb = []
    for r in reqs:
        p = PLANS.get(r[4], {})
        text += f"#{r[0]} @{r[2] or '—'} <code>{r[1]}</code> — {p.get('name', r[4])}\n"
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
    approve_payment(rid)
    create_slot(req[1], req[4])
    await call.message.edit_text(
        f"✅ Заявка #{rid} одобрена",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К заявкам", callback_data="adm_payments")]
        ])
    )
    try:
        await bot.send_message(
            req[1],
            "🎉 <b>Оплата подтверждена!</b> Можно загружать бота.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")]
            ]),
            parse_mode="HTML"
        )
    except Exception:
        pass

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
            [InlineKeyboardButton(text="« К заявкам", callback_data="adm_payments")]
        ])
    )
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
            text += f"• <code>{p[1]}</code> — {PLANS.get(p[2], {}).get('name', p[2])} (×{p[3]})\n"
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
    await state.update_data(code=code)
    await message.answer(
        f"Код: <code>{code}</code>\nТариф:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Неделя", callback_data="adm_plan:week")],
            [InlineKeyboardButton(text="🗓 2 недели", callback_data="adm_plan:2weeks")],
            [InlineKeyboardButton(text="💎 Месяц", callback_data="adm_plan:month")]
        ]),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_plan:"))
async def adm_promo_plan(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(plan=call.data.split(":")[1])
    await state.set_state(AdminStates.promo_uses)
    await call.message.edit_text("Количество активаций (число):")

@dp.message(AdminStates.promo_uses)
async def adm_promo_uses(message: types.Message, state: FSMContext):
    if not (message.text or "").isdigit():
        return await message.answer("❌ Число!")
    data = await state.get_data()
    create_promo(data["code"], data["plan"], int(message.text))
    await message.answer(
        f"✅ <code>{data['code']}</code> создан!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К промокодам", callback_data="adm_promos")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("adm_promo_del:"))
async def cb_adm_promo_del(call: types.CallbackQuery):
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
            callback_data=f"bot:{b[0]}"
        )])
    kb.append([InlineKeyboardButton(text="« Админка", callback_data="admin")])
    await call.message.edit_text(
        "🤖 <b>Все боты (God Mode)</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("adm_viewuser_id:"))
async def cb_adm_viewuser_id(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    u = get_user(uid)
    if not u:
        return await call.answer("Не найден", show_alert=True)
    text = (
        f"👤 <b>Юзер</b>\nID: <code>{u[0]}</code>\n@{u[1] or '—'}\n"
        f"Бан: {'Да' if u[4] else 'Нет'}\nБотов: {len(get_user_bots(uid))}"
    )
    kb = [[InlineKeyboardButton(
        text="🚫 Бан" if not u[4] else "✅ Разбан",
        callback_data=f"adm_act_toggleban:{u[0]}"
    )], [InlineKeyboardButton(text="« Админка", callback_data="admin")]]
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_act_toggleban:"))
async def cb_adm_toggleban(call: types.CallbackQuery):
    uid = int(call.data.split(":")[1])
    u = get_user(uid)
    if not u:
        return
    if u[4]:
        unban_user(uid)
    else:
        ban_user(uid)
        for b in get_user_bots(uid):
            await stop_user_bot(b[0])
    await call.answer("Готово")
    await cb_admin(call, None)

@dp.callback_query(F.data == "adm_msguser")
async def cb_adm_msguser(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.msg_uid)
    await call.message.edit_text("✉️ ID или @username:")

@dp.message(AdminStates.msg_uid)
async def adm_msg_uid(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    uid = find_user_by_username(txt) if txt.startswith("@") else (int(txt) if txt.isdigit() else None)
    if not uid:
        return await message.answer("❌ Не найден")
    await state.update_data(target_uid=uid)
    await state.set_state(AdminStates.msg_text)
    await message.answer(f"Текст для <code>{uid}</code>:", parse_mode="HTML")

@dp.message(AdminStates.msg_text)
async def adm_msg_send(message: types.Message, state: FSMContext):
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
    await call.message.edit_text("📢 Текст рассылки (HTML ок) /cancel:")

@dp.message(AdminStates.broadcast)
async def adm_broadcast_send(message: types.Message, state: FSMContext):
    await state.clear()
    users = get_all_users()
    ok = 0
    st = await message.answer(f"⏳ {len(users)} юзеров...")
    body = message.html_text or message.text or ""
    for u in users:
        try:
            await bot.send_message(u[0], body, parse_mode="HTML")
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await st.edit_text(f"✅ Доставлено: {ok}/{len(users)}")

@dp.callback_query(F.data == "adm_ban")
async def cb_adm_ban(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.ban)
    await call.message.edit_text("🚫 ID или @username:")

@dp.message(AdminStates.ban)
async def adm_ban_do(message: types.Message, state: FSMContext):
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
    await state.set_state(AdminStates.unban)
    await call.message.edit_text("✅ ID для разбана:")

@dp.message(AdminStates.unban)
async def adm_unban_do(message: types.Message, state: FSMContext):
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
    remove_admin(int(call.data.split(":")[1]))
    await call.answer("Удалён")
    await cb_admin(call, None)

@dp.callback_query(F.data == "adm_backup")
async def cb_adm_backup(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Только владелец", show_alert=True)
    if DB_PATH.exists():
        await call.message.answer_document(
            FSInputFile(str(DB_PATH), filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db")
        )
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
        if b[6] == 0 and not is_user_banned(b[1]):
            if await start_user_bot(b[0]):
                n += 1
    await call.message.answer(f"🔄 Перезапущено: {n}")

# ═══════════════════════════════════════════════════════════════
# 🎯 MAIN — С УДАЛЕНИЕМ WEBHOOK (ФИКС CONFLICT)
# ═══════════════════════════════════════════════════════════════

async def main():
    init_db()
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Добавьте его в Variables/Environment.")
    if not SYNC_SECRET:
        logger.warning("SYNC_SECRET не задан — автоматическая выдача через кассира отключена.")
    acquire_instance_lock()
    logger.info("=" * 50)
    logger.info("🤖 BotHost v8.0 запускается...")
    logger.info(f"👤 Владелец: {OWNER_ID}")
    logger.info(f"🔗 Кассир: @{VERIFIER_BOT_USERNAME}")
    logger.info(f"📢 SYNC канал: {SYNC_CHANNEL_ID}")
    logger.info("=" * 50)

    # ГЛАВНЫЙ ФИКС: сброс webhook, иначе Conflict forever
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook удалён, polling готов")
    except Exception as e:
        logger.warning(f"delete_webhook: {e}")

    await restore_running_bots()
    asyncio.create_task(monitor_bots())
    asyncio.create_task(auto_backup())

    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "channel_post"])

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
