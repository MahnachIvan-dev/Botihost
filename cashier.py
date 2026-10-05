"""BotHost AI Cashier v2.1 — проверка оплаты подарком (2 скрина + эталоны).

Что исправлено по сравнению с прошлой версией:
  • Тарифы синхронизированы с BotHost v9: слоты на бота (🧩) и хостинг (🚀).
    Раньше хостинг купить через кассира было нельзя — заявка молча отбрасывалась.
  • Подпись /payment_request теперь реально проверяется (раньше username парсился
    вместе с подписью, и проверка не выполнялась).
  • Кассир отказывается вести оплату, если не настроены SYNC_CHANNEL_ID/SYNC_SECRET
    или GROQ_API_KEY: раньше пользователь мог заплатить и не получить ничего.
  • Скрины сжимаются и уменьшаются перед отправкой в ИИ (большие PNG ломали запрос).
  • Эталонные скрины собираются в один композит и кэшируются (было — каждый раз заново).
  • Запрос к ИИ повторяется без необязательных параметров, если модель их не приняла.
  • Разбор ответа ИИ устойчив: «50⭐», «-7 мин», markdown-обёртки JSON больше не ломают проверку.
  • Все JSON-файлы состояния пишутся атомарно (не теряются при редеплое).
  • Есть обработчик-подсказка: после рестарта кассира фото больше не игнорируется молча.
  • Оба скрина не могут быть одним и тем же файлом.
  • Услуги (видео/анимации) выключены по умолчанию: в BotHost их некому выдать.
  • Кулдаун после оплаты настраивается (по умолчанию 5 мин вместо 1 часа).
  • Понятные ошибки TelegramConflictError и команда /diag для владельца.
"""
import os, re, json, base64, asyncio, logging, hmac, hashlib, io, html
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from groq import AsyncGroq
from PIL import Image, ImageOps, ImageDraw, UnidentifiedImageError

# ═══════════════════════════════════════════════════════════════
# 🔧 КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════

CASHIER_TOKEN = os.environ.get("CASHIER_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "8269807543"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "ivan_unreal").lstrip("@")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
SYNC_CHANNEL_ID = int(os.environ.get("SYNC_CHANNEL_ID", "0"))
SYNC_SECRET = os.environ.get("SYNC_SECRET", "").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
EXAMPLES_DIR = DATA_DIR / "cashier_examples"
EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = EXAMPLES_DIR / "config.json"
PAYMENT_GUARD_FILE = EXAMPLES_DIR / "payment_guard.json"
MANUAL_REVIEWS_FILE = EXAMPLES_DIR / "manual_reviews.json"
SERVICE_REQUESTS_FILE = EXAMPLES_DIR / "service_requests.json"

# ⏱ Кулдаун после одобренной оплаты. Раньше был жёсткий час, из-за чего нельзя было
# докупить второй слот. Защита от повторов держится на хешах скринов, а не на таймере.
COOLDOWN_SECONDS = max(0, int(float(os.environ.get("CASHIER_COOLDOWN_MIN", "5")) * 60))
# 🎬 Доп. услуги (видео/анимации). В BotHost таких услуг нет — по умолчанию выключено,
# иначе человек может заплатить и не получить ничего.
ENABLE_SERVICES = os.environ.get("CASHIER_SERVICES", "0").strip().lower() in {"1", "true", "yes", "on"}
# 🖼 Ограничения на скрины перед отправкой в ИИ
MAX_IMAGE_SIDE = int(os.environ.get("CASHIER_IMAGE_SIDE", "1600"))
MAX_IMAGE_BYTES = int(os.environ.get("CASHIER_IMAGE_MAX_BYTES", "3500000"))
DEFAULT_TOLERANCE = 10
REVIEW_TTL_DAYS = 7

payment_guard_lock = asyncio.Lock()
service_requests = {}   # request_id -> {uid, service_key, uses, stars, status, created_at}
manual_reviews = {}     # review_id  -> {uid, plan, ..., created_at}
_ref_cache = {"key": None, "b64": None}

logging.basicConfig(level=logging.INFO, format="[CASHIER] %(asctime)s │ %(levelname)s │ %(message)s")
log = logging.getLogger("Cashier")

bot = Bot(token=CASHIER_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ───────────────────────────────────────────────────────────────
# 💎 ТАРИФЫ — ДОЛЖНЫ СОВПАДАТЬ С PLANS В BotHost (bot.py)!
# kind: "bot" — слот на одного бота, "host" — ботов без лимита
# ───────────────────────────────────────────────────────────────

DEFAULT_PLANS = {
    "week":   {"name": "Неделя",   "stars": 15, "days": 7,  "kind": "bot",  "emoji": "📅"},
    "2weeks": {"name": "2 недели", "stars": 25, "days": 14, "kind": "bot",  "emoji": "🗓"},
    "month":  {"name": "Месяц",    "stars": 50, "days": 30, "kind": "bot",  "emoji": "💎"},
    "host_week":   {"name": "Хостинг · Неделя",   "stars": 50,  "days": 7,  "kind": "host", "emoji": "🚀"},
    "host_2weeks": {"name": "Хостинг · 2 недели", "stars": 100,  "days": 14, "kind": "host", "emoji": "🛰"},
    "host_month":  {"name": "Хостинг · Месяц",    "stars": 100, "days": 30, "kind": "host", "emoji": "🌌"},
}


def load_plans():
    """Тарифы по умолчанию + необязательные правки из CASHIER_PLANS_JSON.

    Пример: CASHIER_PLANS_JSON={"month":{"stars":45},"host_month":{"stars":119}}
    Меняя цены в BotHost, поменяй их и здесь (или через эту переменную).
    """
    plans = {k: dict(v) for k, v in DEFAULT_PLANS.items()}
    raw = os.environ.get("CASHIER_PLANS_JSON", "").strip()
    if not raw:
        return plans
    try:
        override = json.loads(raw)
        if not isinstance(override, dict):
            raise ValueError("ожидался объект")
        for pid, patch in override.items():
            if pid not in plans or not isinstance(patch, dict):
                log.warning("CASHIER_PLANS_JSON: пропускаю %r", pid)
                continue
            for field in ("name", "stars", "days", "kind", "emoji"):
                if field in patch:
                    plans[pid][field] = patch[field]
            plans[pid]["stars"] = max(1, int(plans[pid]["stars"]))
            plans[pid]["days"] = max(1, int(plans[pid]["days"]))
            if plans[pid]["kind"] not in ("bot", "host"):
                plans[pid]["kind"] = "bot"
        log.info("CASHIER_PLANS_JSON применён: %s", ", ".join(sorted(override)))
    except Exception as e:
        log.error("Не удалось применить CASHIER_PLANS_JSON (%s) — беру тарифы по умолчанию", e)
    return plans


PLANS = load_plans()
PAID_SERVICE_PLANS = {"video": {"name": "🎬 Видео", "stars": 15, "uses": 5},
                      "animation": {"name": "✨ Анимации", "stars": 15, "uses": 5}}
TIMEZONES = {"tz_msk": {"name": "🇷🇺 Москва / Минск · UTC+3", "offset": 3},
             "tz_utc2": {"name": "UTC+2", "offset": 2},
             "tz_utc4": {"name": "UTC+4", "offset": 4},
             "tz_utc5": {"name": "UTC+5", "offset": 5},
             "tz_utc6": {"name": "UTC+6", "offset": 6},
             "tz_utc7": {"name": "UTC+7", "offset": 7}}


class CashierStates(StatesGroup):
    choose_plan = State(); select_tz = State(); waiting_payment = State(); waiting_profile = State()
    admin_menu = State(); admin_payment_example = State(); admin_profile_example = State()
    admin_tolerance = State()


# ═══════════════════════════════════════════════════════════════
# 🧰 ФАЙЛЫ СОСТОЯНИЯ (атомарная запись)
# ═══════════════════════════════════════════════════════════════

def atomic_write_json(path: Path, data):
    """Пишем через временный файл: редеплой посреди записи не портит состояние."""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # На Windows замена может упасть, если файл кем-то открыт.
        path.write_text(payload, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: Path, default):
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, type(default)):
                return data
    except Exception:
        log.exception("Чтение %s не удалось", path.name)
    return default


def load_config():
    data = read_json(CONFIG_FILE, {})
    try:
        return {"time_tolerance_minutes": max(1, min(60, int(data.get("time_tolerance_minutes", DEFAULT_TOLERANCE))))}
    except Exception:
        return {"time_tolerance_minutes": DEFAULT_TOLERANCE}


def save_config(data):
    atomic_write_json(CONFIG_FILE, data)


def admin_cfg():
    return load_config()


def load_payment_guard():
    return read_json(PAYMENT_GUARD_FILE, {"users": {}, "hashes": {}})


def save_payment_guard(data):
    atomic_write_json(PAYMENT_GUARD_FILE, data)


def load_manual_reviews():
    global manual_reviews
    manual_reviews = read_json(MANUAL_REVIEWS_FILE, {})


def save_manual_reviews():
    atomic_write_json(MANUAL_REVIEWS_FILE, manual_reviews)


def load_service_requests():
    global service_requests
    service_requests = read_json(SERVICE_REQUESTS_FILE, {})


def save_service_requests():
    atomic_write_json(SERVICE_REQUESTS_FILE, service_requests)


def prune_state():
    """Чистим старые заявки: иначе файлы растут вечно."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=REVIEW_TTL_DAYS)).timestamp()
    changed = False
    for rid, review in list(manual_reviews.items()):
        try:
            if float(review.get("created_at", 0)) < cutoff:
                manual_reviews.pop(rid, None); changed = True
        except Exception:
            manual_reviews.pop(rid, None); changed = True
    for sid, req in list(service_requests.items()):
        try:
            if req.get("status") != "pending" and float(req.get("created_at", 0)) < cutoff:
                service_requests.pop(sid, None); changed = True
        except Exception:
            service_requests.pop(sid, None); changed = True
    if changed:
        save_manual_reviews(); save_service_requests()
    return changed


def payment_hash(image_b64):
    try:
        return hashlib.sha256(base64.b64decode(image_b64)).hexdigest()
    except Exception:
        return hashlib.sha256(image_b64.encode()).hexdigest()


def guard_status(uid, image_b64=None, cooldown=True):
    data = load_payment_guard()
    now = int(datetime.now(timezone.utc).timestamp())
    user = data.get("users", {}).get(str(uid), {})
    approved_at = int(user.get("approved_at", 0) or 0)
    left = max(0, COOLDOWN_SECONDS - (now - approved_at)) if approved_at and COOLDOWN_SECONDS else 0
    if cooldown and left:
        return False, f"⏳ После одобренной оплаты повторная покупка доступна через <b>{(left + 59) // 60} мин.</b>"
    if image_b64:
        h = payment_hash(image_b64)
        if h in data.get("hashes", {}):
            return False, "❌ Этот скрин чека уже использовался для одобренной оплаты. Повторно начислить по нему нельзя."
    return True, ""


async def commit_approved_payment(uid, image_b64, plan_id, grant_id):
    data = load_payment_guard()
    now = int(datetime.now(timezone.utc).timestamp())
    h = payment_hash(image_b64)
    data.setdefault("users", {})[str(uid)] = {"approved_at": now, "plan_id": plan_id, "grant_id": grant_id, "payment_hash": h}
    data.setdefault("hashes", {})[h] = {"uid": uid, "approved_at": now, "plan_id": plan_id, "grant_id": grant_id}
    cutoff = now - REVIEW_TTL_DAYS * 24 * 3600
    data["hashes"] = {k: v for k, v in data["hashes"].items() if int(v.get("approved_at", 0) or 0) >= cutoff}
    save_payment_guard(data)


# ═══════════════════════════════════════════════════════════════
# 🖼 ИЗОБРАЖЕНИЯ
# ═══════════════════════════════════════════════════════════════

def normalize_image_bytes(raw: bytes) -> bytes:
    """Уменьшает и пережимает скриншот.

    Telegram отдаёт крупные PNG, а ИИ принимает ограниченный payload: без этого
    анализ больших скринов падал с ошибкой вместо проверки.
    """
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im) or im
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        data = raw
        for _ in range(6):
            for quality in (85, 75, 65, 55):
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=quality, optimize=True)
                data = buf.getvalue()
                if len(data) <= MAX_IMAGE_BYTES:
                    return data
            im.thumbnail((max(1, int(im.width * 0.8)), max(1, int(im.height * 0.8))), Image.Resampling.LANCZOS)
        return data
    except UnidentifiedImageError:
        log.warning("Присланный файл не является изображением — отправляю как есть")
        return raw
    except Exception:
        log.exception("Не удалось сжать изображение — отправляю как есть")
        return raw


async def photo_b64(message: types.Message) -> str:
    photo = message.photo[-1]
    info = await bot.get_file(photo.file_id)
    buf = await bot.download_file(info.file_path)
    raw = buf.read()
    return base64.b64encode(normalize_image_bytes(raw)).decode()


def ref_file(kind: str) -> Path:
    return EXAMPLES_DIR / f"{kind}_reference.jpg"


def has_ref(kind: str) -> bool:
    return ref_file(kind).exists()


def build_reference_composite():
    """Один композит из эталонов + кэш по времени изменения файлов."""
    payment, profile = ref_file("payment"), ref_file("profile")
    key = tuple((str(p), p.stat().st_mtime_ns if p.exists() else 0) for p in (payment, profile))
    if _ref_cache["key"] == key:
        return _ref_cache["b64"]
    out_b64 = None
    if key[0][1] or key[1][1]:
        try:
            imgs = []
            for label, path in (("ЭТАЛОН ЧЕКА", payment), ("ЭТАЛОН ПРОФИЛЯ", profile)):
                if not path.exists():
                    continue
                im = Image.open(path).convert("RGB")
                im.thumbnail((900, 1200), Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", (920, im.height + 70), "white")
                canvas.paste(im, ((920 - im.width) // 2, 70))
                ImageDraw.Draw(canvas).text((20, 20), label, fill="black")
                imgs.append(canvas)
            if imgs:
                if len(imgs) == 1:
                    out = imgs[0]
                else:
                    w = max(x.width for x in imgs)
                    h = sum(x.height for x in imgs) + 20
                    out = Image.new("RGB", (w, h), "white")
                    y = 0
                    for im in imgs:
                        out.paste(im, ((w - im.width) // 2, y)); y += im.height + 10
                buf = io.BytesIO()
                out.save(buf, format="JPEG", quality=88, optimize=True)
                out_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            log.exception("Не удалось собрать эталонный композит")
    _ref_cache["key"], _ref_cache["b64"] = key, out_b64
    return out_b64


async def send_examples(chat_id):
    if has_ref("payment"):
        await bot.send_photo(chat_id, types.FSInputFile(ref_file("payment")),
                             caption="📸 Эталон чека: пример того, где видны сумма, получатель и время.")
    if has_ref("profile"):
        await bot.send_photo(chat_id, types.FSInputFile(ref_file("profile")),
                             caption="👤 Эталон профиля: пример профиля получателя, с которым сверяется второй скрин.")


# ═══════════════════════════════════════════════════════════════
# 🔗 СВЯЗЬ С BotHost ЧЕРЕЗ ЗАКРЫТЫЙ КАНАЛ
# ═══════════════════════════════════════════════════════════════

def sync_ready():
    return bool(SYNC_CHANNEL_ID and SYNC_SECRET)


def sign(kind, *parts):
    raw = ":".join([kind, *map(str, parts)])
    return hmac.new(SYNC_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


async def send_channel(text):
    if not SYNC_CHANNEL_ID:
        log.error("SYNC_CHANNEL_ID не задан — сообщение в канал не отправлено")
        return False
    try:
        await bot.send_message(SYNC_CHANNEL_ID, text)
        return True
    except Exception as e:
        log.error("sync channel: %s", e)
        return False


async def request_from_user(uid, plan_id):
    """Просим BotHost создать заявку. username нужен для подписи, поэтому «-» вместо пустоты."""
    username = "-"
    try:
        chat = await bot.get_chat(uid)
        username = (chat.username or "-").strip() or "-"
    except Exception as e:
        log.warning("get_chat(%s): %s", uid, e)
    sig = sign("request", uid, plan_id, username)
    return await send_channel(f"/payment_request {uid} {plan_id} {username} {sig}")


async def finish_result(uid, plan_id, result, grant_id):
    return await send_channel(f"/payment_result {uid} {plan_id} {result} {grant_id} {sign('result', uid, plan_id, result, grant_id)}")


async def finish_service_result(uid, service_key, result, grant_id, request_id=""):
    if request_id:
        sig = sign("service_result", uid, service_key, result, grant_id, request_id)
        return await send_channel(f"/service_payment_result {uid} {service_key} {result} {grant_id} {sig} {request_id}")
    sig = sign("service_result", uid, service_key, result, grant_id)
    return await send_channel(f"/service_payment_result {uid} {service_key} {result} {grant_id} {sig}")


@dp.message(F.chat.id == SYNC_CHANNEL_ID)
@dp.channel_post(F.chat.id == SYNC_CHANNEL_ID)
async def sync_channel_post(message: types.Message):
    text = message.text or ""

    # ── Заявка на тариф от BotHost: /payment_request UID PLAN USERNAME SIGNATURE ──
    if text.startswith("/payment_request"):
        parts = text.split()
        if len(parts) < 5:
            log.warning("payment_request: мало полей (%s)", text[:120])
            return
        try:
            uid = int(parts[1])
        except ValueError:
            return
        plan = parts[2].strip()
        username = parts[3].strip()
        signature = parts[4].strip()
        if plan not in PLANS:
            log.warning("payment_request: неизвестный тариф %r — он должен быть и в BotHost, и здесь", plan)
            return
        # Подпись обязательна: без неё заявку мог бы подделать любой, кто пишет в канал.
        if not SYNC_SECRET:
            log.error("payment_request: SYNC_SECRET не задан — заявка отклонена")
            return
        expected = sign("request", uid, plan, username)
        if not hmac.compare_digest(expected, signature):
            log.warning("payment_request: неверная подпись для %s", uid)
            return
        p = PLANS[plan]
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"💳 Оплатить {p['emoji']} {p['name']} · {p['stars']}⭐", callback_data=f"plan:{plan}")]])
        try:
            await bot.send_message(
                uid,
                f"💳 <b>Оплата заказа</b>\n\n"
                f"{p['emoji']} <b>{p['name']}</b> · <b>{p['stars']}⭐</b>\n\n"
                "Нажми кнопку, чтобы пройти проверку оплаты через кассир.",
                reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            log.error("payment request user notify: %s", e)
        return

    # ── Заявка на доп. услугу (по умолчанию услуги отключены) ──
    if not text.startswith("/service_payment_request"):
        return
    if not ENABLE_SERVICES:
        log.info("service_payment_request проигнорирован: CASHIER_SERVICES выключен")
        return
    parts = text.split(maxsplit=5)
    if len(parts) < 6:
        return
    try:
        uid = int(parts[1]); service_key = parts[2]; uses = int(parts[3]); stars = int(parts[4])
        request_id = parts[5].splitlines()[0].strip()
    except Exception:
        return
    service = PAID_SERVICE_PLANS.get(service_key)
    if not service or uses != service["uses"] or stars != service["stars"]:
        return
    sig_line = next((x for x in text.splitlines()[1:] if x.startswith("SIGNATURE=")), "")
    signature = sig_line.split("=", 1)[1] if "=" in sig_line else ""
    expected = sign("service_request", uid, service_key, request_id)
    if not signature or not hmac.compare_digest(expected, signature):
        log.warning("Неверная подпись service_payment_request для %s", uid)
        return
    # Один активный запрос на услугу у пользователя: повторное событие не создаёт второй.
    for existing_id, existing in service_requests.items():
        if (existing.get("uid") == uid and existing.get("service_key") == service_key
                and existing.get("status", "pending") == "pending"):
            request_id = existing_id
            break
    service_requests[request_id] = {"uid": uid, "service_key": service_key, "uses": uses, "stars": stars,
                                    "status": "pending", "created_at": datetime.now(timezone.utc).timestamp()}
    save_service_requests()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"💳 Купить за {stars}⭐", callback_data=f"service_start:{request_id}:{service_key}")]])
    try:
        await bot.send_message(
            uid,
            f"💎 <b>{service['name']}</b>\n\n🎟 Пакет: <b>{uses} использований</b>\n💰 Стоимость: <b>{stars}⭐</b>\n\n"
            "Оплата проходит подарком владельцу. Нажми кнопку ниже и пройди проверку по скриншотам.",
            reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        log.error("service request user notify: %s", e)


# ═══════════════════════════════════════════════════════════════
# 👤 ПОКУПАТЕЛЬ
# ═══════════════════════════════════════════════════════════════

def plans_kb(kind=None):
    rows = []
    for pid, p in PLANS.items():
        if kind and p["kind"] != kind:
            continue
        rows.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} · {p['stars']}⭐", callback_data=f"plan:{pid}")])
    return rows


@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    args = (message.text or "").split(maxsplit=1)
    plan = args[1].strip() if len(args) > 1 and args[1].strip() in PLANS else None
    await state.clear()
    if not sync_ready():
        return await message.answer(
            "⚠️ <b>Кассир пока не настроен.</b>\n\n"
            "Владельцу нужно задать SYNC_CHANNEL_ID и SYNC_SECRET. Оплата временно недоступна — "
            "напиши владельцу и не отправляй подарок.",
            parse_mode="HTML")
    if not plan:
        await state.set_state(CashierStates.choose_plan)
        kb = [[InlineKeyboardButton(text="🧩 Слот на 1 бота", callback_data="catalog:bot")],
              [InlineKeyboardButton(text="🚀 Хостинг без лимита ботов", callback_data="catalog:host")],
              [InlineKeyboardButton(text="ℹ️ Как проходит проверка", callback_data="info")]]
        return await message.answer(
            "💳 <b>Кассир BotHost</b>\n\n"
            "Выбери тариф — дальше кассир проведёт тебя по шагам.\n"
            "Продление автоматически прибавляется к текущему сроку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await begin_plan(message, state, plan)


@dp.callback_query(F.data.startswith("catalog:"))
async def catalog(call: types.CallbackQuery, state: FSMContext):
    kind = call.data.split(":", 1)[1]
    await call.answer()
    title = "🧩 Слот на 1 бота" if kind == "bot" else "🚀 Хостинг без лимита ботов"
    hint = ("Одна подписка = один работающий бот." if kind == "bot"
            else "Ботов сколько угодно, пока активна подписка.")
    kb = plans_kb(kind)
    kb.append([InlineKeyboardButton(text="« Назад", callback_data="back_plans")])
    await call.message.edit_text(f"<b>{title}</b>\n{hint}\n\nВыбери срок:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("service_start:"))
async def service_start(call: types.CallbackQuery, state: FSMContext):
    if not ENABLE_SERVICES:
        return await call.answer("Эта услуга отключена.", show_alert=True)
    try:
        _, request_id, service_key = call.data.split(":", 2)
    except ValueError:
        return await call.answer("Некорректная заявка.", show_alert=True)
    service = PAID_SERVICE_PLANS.get(service_key)
    req = service_requests.get(request_id)
    if not service or not req or req.get("uid") != call.from_user.id or req.get("service_key") != service_key:
        return await call.answer("Запрос устарел — открой кассир заново.", show_alert=True)
    if req.get("status", "pending") != "pending":
        return await call.answer("Эта заявка уже обработана.", show_alert=True)
    await call.answer()
    await state.clear()
    await state.update_data(plan_id=service_key, service_key=service_key, request_id=request_id)
    await state.set_state(CashierStates.select_tz)
    await ask_timezone(call.message, service, edit=False)


@dp.callback_query(F.data == "cancel_cashier")
async def cancel_cashier(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Отменено")
    try:
        await call.message.edit_text("↩️ Покупка отменена. Ничего не начислено.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(call: types.CallbackQuery, state: FSMContext):
    plan = call.data.split(":", 1)[1]
    await call.answer()
    if plan not in PLANS:
        return await call.answer("Такого тарифа нет", show_alert=True)
    await begin_plan(call.message, state, plan, edit=True)


async def begin_plan(message, state, plan, edit=False):
    if not sync_ready():
        return await message.answer("⚠️ Кассир не настроен: нет SYNC_CHANNEL_ID/SYNC_SECRET. Оплата недоступна.", parse_mode="HTML")
    if not groq_client:
        return await message.answer("⚠️ Проверка скриншотов недоступна: владельцу нужно задать GROQ_API_KEY.", parse_mode="HTML")
    allowed, reason = guard_status(message.chat.id, cooldown=True)
    if not allowed:
        return await message.answer(reason, parse_mode="HTML")
    await state.update_data(plan_id=plan)
    # Заявку создаём в BotHost, чтобы он знал, что ждём оплату.
    if not await request_from_user(message.chat.id, plan):
        await state.clear()
        return await message.answer(
            "❌ Не удалось связаться с BotHost. Оплата не начата — пожалуйста, напиши владельцу.",
            parse_mode="HTML")
    await state.set_state(CashierStates.select_tz)
    await ask_timezone(message, PLANS[plan], edit=edit)


async def ask_timezone(message, cfg, edit=False):
    kb = [[InlineKeyboardButton(text=x["name"], callback_data=f"tz:{k}")] for k, x in TIMEZONES.items()]
    text = (f"💳 <b>{cfg['emoji']} {cfg['name']} · {cfg['stars']}⭐</b>\n\n"
            "🌍 <b>Шаг 1 из 3</b>\nВыбери часовой пояс, чтобы сверить время на чеке.")
    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    if edit:
        try:
            return await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "info")
async def info(call: types.CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "ℹ️ <b>Как проходит проверка</b>\n\n"
        "1. Тариф и часовой пояс.\n2. Скрин чека.\n3. Скрин профиля/получателя.\n"
        "4. ИИ сверяет сумму, получателя, профиль и время.\n"
        "5. Допуск времени настраивает владелец.\n"
        "6. Результат уходит в BotHost через защищённый канал, подписка активируется сама.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« К тарифам", callback_data="back_plans")]]),
        parse_mode="HTML")


@dp.callback_query(F.data == "back_plans")
async def back_plans(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = [[InlineKeyboardButton(text="🧩 Слот на 1 бота", callback_data="catalog:bot")],
          [InlineKeyboardButton(text="🚀 Хостинг без лимита ботов", callback_data="catalog:host")]]
    try:
        await call.message.edit_text("💳 <b>Кассир BotHost</b>\n\nВыбери тариф — дальше кассир проведёт тебя по шагам.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception:
        await call.message.answer("💳 <b>Кассир BotHost</b>\n\nВыбери тариф.",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("tz:"), CashierStates.select_tz)
async def tz_selected(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    tz = TIMEZONES.get(code, TIMEZONES["tz_msk"])
    data = await state.get_data()
    plan = data.get("plan_id")
    if plan not in PLANS and plan not in PAID_SERVICE_PLANS:
        await state.clear()
        return await call.answer("Сессия устарела, начни заново: /start", show_alert=True)
    await state.update_data(tz_offset=tz["offset"], tz_name=tz["name"])
    await state.set_state(CashierStates.waiting_payment)
    await send_examples(call.from_user.id)
    cfg = PLANS.get(plan) or PAID_SERVICE_PLANS.get(plan)
    gift_url = f"https://t.me/{OWNER_USERNAME}"
    await call.message.edit_text(
        f"🌍 <b>{tz['name']}</b>\n\n"
        f"🎁 <b>Шаг 2 из 3 — подтверждение подарка</b>\n\n"
        f"Отправь владельцу <b>@{OWNER_USERNAME}</b> подарок стоимостью <b>{cfg['stars']}⭐</b>.\n"
        "После отправки нажми кнопку ниже и пришли скрин успешной отправки.\n\n"
        "⚠️ Один и тот же скрин нельзя использовать дважды.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Открыть профиль владельца", url=gift_url)],
            [InlineKeyboardButton(text="📸 Я отправил подарок", callback_data="gift_sent")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_cashier")]]),
        parse_mode="HTML")


@dp.callback_query(F.data == "gift_sent", CashierStates.waiting_payment)
async def gift_sent(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.edit_text(
        "📸 <b>Шаг 2 из 3</b>\n\nТеперь отправь <b>скрин успешной отправки подарка</b>. "
        "На нём должны быть видны сумма/стоимость, получатель и время.", parse_mode="HTML")


@dp.message(CashierStates.waiting_payment, F.photo)
async def payment_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan = data.get("plan_id")
    if plan not in PLANS and plan not in PAID_SERVICE_PLANS:
        await state.clear()
        return await message.answer("⚠️ Сессия устарела. Начни заново: /start")
    b64 = await photo_b64(message)
    allowed, reason = guard_status(message.from_user.id, b64, cooldown=(plan not in PAID_SERVICE_PLANS))
    if not allowed:
        return await message.answer(reason, parse_mode="HTML")
    await state.update_data(payment_b64=b64, payment_file_id=message.photo[-1].file_id)
    await state.set_state(CashierStates.waiting_profile)
    await message.answer(
        "✅ Первый скрин получен.\n\n"
        f"👤 <b>Шаг 3 из 3</b>\nТеперь отправь второй скрин — профиль получателя <b>@{OWNER_USERNAME}</b>.",
        parse_mode="HTML")


@dp.message(CashierStates.waiting_profile, F.photo)
async def profile_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan = data.get("plan_id")
    payment_b64 = data.get("payment_b64", "")
    if plan not in PLANS and plan not in PAID_SERVICE_PLANS or not payment_b64:
        await state.clear()
        return await message.answer("⚠️ Сессия устарела, скрины не сохранены. Начни заново: /start")
    profile_b64 = await photo_b64(message)
    if payment_hash(profile_b64) == payment_hash(payment_b64):
        return await message.answer("❌ Оба скрина одинаковые. Пришли отдельно скрин чека и скрин профиля получателя.")

    allowed, reason = guard_status(message.from_user.id, payment_b64, cooldown=(plan not in PAID_SERVICE_PLANS))
    if not allowed:
        await state.clear()
        return await message.answer(reason, parse_mode="HTML")
    profile_hash = payment_hash(profile_b64)
    if profile_hash in load_payment_guard().get("hashes", {}):
        await state.clear()
        return await message.answer("❌ Второй скрин уже использовался в подтверждённой заявке. Пришли актуальный скрин.")

    msg = await message.answer("⏳ <i>Проверяю подарок, получателя, время и профиль по 2 скринам и эталонам…</i>", parse_mode="HTML")
    try:
        result = await analyze_two_images(payment_b64, profile_b64, plan, data.get("tz_offset", 3), data.get("tz_name", "UTC+3"))
        decision = str(result.get("decision", "")).lower()
        needs_manual = bool(result.get("needs_manual_review")) or decision in {"manual", "review", "uncertain"}

        if not result.get("is_valid") and not needs_manual:
            await msg.edit_text(
                f"❌ <b>Проверка не пройдена</b>\n\n{html.escape(str(result.get('reason', 'Данные не совпали.')))}\n\n"
                "Можно отправить актуальные скрины заново.", parse_mode="HTML")
            await state.set_state(CashierStates.waiting_payment)
            return

        if needs_manual:
            await open_manual_review(message, state, data, plan, payment_b64, profile_b64,
                                     profile_hash, str(result.get("reason", "ИИ не уверен")))
            await msg.edit_text("🕐 <b>Нужна дополнительная проверка.</b>\n\nЗаявка передана владельцу. Не отправляй подарок повторно.",
                                parse_mode="HTML")
            await state.clear()
            return

        async with payment_guard_lock:
            allowed, reason = guard_status(message.from_user.id, payment_b64, cooldown=(plan not in PAID_SERVICE_PLANS))
            if not allowed:
                await state.clear()
                return await msg.edit_text(reason, parse_mode="HTML")
            grant_id = uuid4().hex
            if plan in PAID_SERVICE_PLANS:
                ok = await finish_service_result(message.from_user.id, plan, "APPROVED", grant_id, data.get("request_id", ""))
            else:
                ok = await finish_result(message.from_user.id, plan, "APPROVED", grant_id)
            if not ok:
                log.error("Не удалось передать APPROVED в BotHost: uid=%s plan=%s", message.from_user.id, plan)
                return await msg.edit_text(
                    "💳 Проверка пройдена, но результат не удалось передать в BotHost.\n\n"
                    "Повторно подарок не отправляй — напиши владельцу, он начислит вручную.", parse_mode="HTML")
            await commit_approved_payment(message.from_user.id, payment_b64, plan, grant_id)
            if plan in PAID_SERVICE_PLANS and data.get("request_id") in service_requests:
                service_requests[data["request_id"]]["status"] = "approved"
                save_service_requests()
            guard = load_payment_guard()
            guard.setdefault("hashes", {})[profile_hash] = {"uid": message.from_user.id,
                                                            "approved_at": int(datetime.now(timezone.utc).timestamp()),
                                                            "plan_id": plan, "grant_id": grant_id, "type": "profile"}
            save_payment_guard(guard)

        cfg = PLANS.get(plan) or PAID_SERVICE_PLANS.get(plan)
        extra = (f"Пакет: <b>{cfg['uses']} использований</b>." if plan in PAID_SERVICE_PLANS
                 else f"{cfg['emoji']} Тариф: <b>{cfg['name']}</b>\n⏳ Подписка активирована, срок смотри в BotHost.")
        await msg.edit_text(f"🎉 <b>Подарок подтверждён!</b>\n\n{extra}\n\nБот уже прислал подтверждение. 💜", parse_mode="HTML")
        await state.clear()
    except Exception as e:
        log.exception("verification error")
        await msg.edit_text(
            "❌ Временная ошибка проверки. Заявка не начислена; повторно подарок не отправляй.\n"
            f"<code>{html.escape(str(e)[:200])}</code>", parse_mode="HTML")


async def open_manual_review(message, state, data, plan, payment_b64, profile_b64, profile_hash, reason):
    review_id = uuid4().hex
    manual_reviews[review_id] = {
        "uid": message.from_user.id, "plan": plan,
        "payment_file_id": data.get("payment_file_id"), "profile_file_id": message.photo[-1].file_id,
        "payment_hash": payment_hash(payment_b64), "profile_hash": profile_hash,
        "reason": reason, "request_id": data.get("request_id", ""),
        "created_at": datetime.now(timezone.utc).timestamp(),
    }
    save_manual_reviews()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"review_ok:{review_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"review_no:{review_id}")]])
    cfg = PLANS.get(plan) or PAID_SERVICE_PLANS.get(plan)
    await bot.send_photo(OWNER_ID, message.photo[-1].file_id,
                         caption=f"🔎 <b>Ручная проверка кассира</b>\n👤 ID: <code>{message.from_user.id}</code>\n"
                                 f"📦 {html.escape(cfg['name'])} · {cfg['stars']}⭐\n\n"
                                 f"🤖 {html.escape(reason)}\n\nНиже — второй скрин. Первый придёт следующим сообщением.",
                         reply_markup=kb, parse_mode="HTML")
    if data.get("payment_file_id"):
        await bot.send_photo(OWNER_ID, data["payment_file_id"], caption="🧾 Первый скрин — подтверждение подарка.")


@dp.message(CashierStates.waiting_payment)
async def need_payment_photo(message: types.Message, state: FSMContext):
    await message.answer("📸 Отправь именно скрин успешной отправки подарка (фото).")


@dp.message(CashierStates.waiting_profile)
async def need_profile_photo(message: types.Message, state: FSMContext):
    await message.answer(f"👤 Отправь второй скрин профиля получателя @{OWNER_USERNAME} как фото.")


# ═══════════════════════════════════════════════════════════════
# 🤖 ПРОВЕРКА ИИ
# ═══════════════════════════════════════════════════════════════

def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def parse_number(value):
    """Достаём число из «-7 мин», «7,5», «1 500»."""
    s = re.sub(r"(?<=\d)[\s\u00a0'](?=\d)", "", str(value))
    m = re.search(r"-?\d+(?:[.,]\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_int(value):
    """Целое из «50⭐», «1 500 Stars», «50»."""
    digits = re.sub(r"\D", "", re.sub(r"(?<=\d)[\s\u00a0'](?=\d)", "", str(value)))
    return int(digits) if digits else None


async def _vision_request(content):
    """Запрос к ИИ с деградацией: не все модели принимают reasoning_effort."""
    base = {"model": VISION_MODEL, "messages": [{"role": "user", "content": content}],
            "temperature": 0.05, "max_completion_tokens": 500}
    attempts = [{**base, "reasoning_effort": "none", "response_format": {"type": "json_object"}},
                {**base, "response_format": {"type": "json_object"}},
                base]
    last = None
    for i, params in enumerate(attempts, 1):
        try:
            r = await groq_client.chat.completions.create(**params)
            return r.choices[0].message.content
        except Exception as e:
            last = e
            log.warning("vision attempt %s/%s не прошёл: %s", i, len(attempts), e)
    raise last


async def analyze_two_images(img1, img2, plan_id, tz_offset, tz_name):
    if not groq_client:
        return {"is_valid": False, "reason": "GROQ_API_KEY не настроен."}
    plan = PLANS.get(plan_id) or PAID_SERVICE_PLANS.get(plan_id)
    if not plan:
        return {"is_valid": False, "reason": "Неизвестный тип оплаты."}
    tolerance = admin_cfg()["time_tolerance_minutes"]
    now = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
    now_s = now.strftime("%d.%m.%Y %H:%M")
    reference = build_reference_composite()

    kind = PLANS.get(plan_id, {}).get("kind")
    what = ("подписку на хостинг (ботов без лимита)" if kind == "host"
            else ("подписку на 1 бота" if kind == "bot" else "услугу"))
    prompt = f"""
Ты проверяешь оплату подарком Telegram владельцу. Есть ДВА скриншота пользователя.
СКРИН 1 — чек/экран оплаты. СКРИН 2 — профиль/экран получателя.
Владелец платежа: @{OWNER_USERNAME}. Покупается: {what}. Тариф: {plan['name']}, минимум {plan['stars']} Stars.
Текущее локальное время покупателя: {now_s}. Часовой пояс: {tz_name}.
Допуск по времени: не более {tolerance} минут.

ПРАВИЛА ПРОВЕРКИ:
1. На первом скрине должна быть успешная отправка ПОДАРКА, номинал не меньше {plan['stars']} Stars, получатель @{OWNER_USERNAME} и время.
2. На втором скрине должен быть виден получатель: username/имя/аватар должны согласовываться с @{OWNER_USERNAME}.
3. Если приложен ЭТАЛОН, сравнивай с ним: он только ориентир, НЕ доказательство оплаты.
4. Время операции на чеке сравнивай с {now_s}. Разница больше {tolerance} минут — отклоняй. Время не видно или неоднозначно — ручная проверка, не подтверждай автоматически.
5. Сумму бери только с чека пользователя, не из эталона.
6. Не придумывай данные. При сомнении возвращай needs_manual_review=true и decision="manual".
7. Ищи признаки подделки, но не утверждай подделку без визуального основания.

Верни строго JSON:
{{"is_valid":true|false,"decision":"approve|manual|reject","needs_manual_review":true|false,"reason":"кратко по-русски","detected_stars":0,"detected_time":"DD.MM.YYYY HH:MM или null","recipient":"строка или null","time_difference_minutes":0,"profile_match":"yes|no|unclear"}}
"""
    content = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": "USER PAYMENT SCREENSHOT (основное доказательство):"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img1}"}},
        {"type": "text", "text": "USER PROFILE/RECIPIENT SCREENSHOT (основное доказательство):"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img2}"}},
    ]
    if reference:
        content += [{"type": "text", "text": "ADMIN REFERENCE (эталоны; только для сравнения, не доказательство оплаты):"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{reference}"}}]
    try:
        raw = await _vision_request(content)
    except Exception as e:
        log.exception("vision error")
        return {"is_valid": False, "reason": f"Ошибка анализа изображения ИИ: {e}"}

    result = _extract_json(raw)
    if not isinstance(result, dict):
        log.warning("ИИ вернул не JSON: %s", str(raw)[:300])
        return {"is_valid": False, "needs_manual_review": True, "decision": "manual",
                "reason": "ИИ вернул неразборчивый ответ — нужна ручная проверка."}

    # ── Серверные ограничители: ИИ не может подтвердить оплату без доказательств ──
    if str(result.get("profile_match", "")).lower() == "no":
        result["is_valid"] = False
        result.setdefault("reason", "Профиль получателя не совпадает с владельцем.")
    diff = parse_number(result.get("time_difference_minutes"))
    if diff is not None and abs(diff) > tolerance:
        result["is_valid"] = False
        result["reason"] = f"Разница времени {abs(diff):g} мин больше допуска {tolerance} мин."
    stars = parse_int(result.get("detected_stars"))
    if stars is None or stars < plan["stars"]:
        result["is_valid"] = False
        if stars is None:
            result["reason"] = result.get("reason") or "На чеке не удалось прочитать сумму."
        else:
            result["reason"] = f"На чеке {stars}⭐, а нужно минимум {plan['stars']}⭐."
    return result


# ═══════════════════════════════════════════════════════════════
# 👑 РУЧНАЯ ПРОВЕРКА
# ═══════════════════════════════════════════════════════════════

async def approve_review(review) -> tuple[bool, str]:
    """Общая выдача по ручному одобрению. Возвращает (успех, текст ошибки)."""
    plan = review["plan"]
    if plan not in PLANS and plan not in PAID_SERVICE_PLANS:
        return False, "Неизвестный тариф в заявке"
    async with payment_guard_lock:
        grant_id = uuid4().hex
        if plan in PAID_SERVICE_PLANS:
            ok = await finish_service_result(review["uid"], plan, "APPROVED", grant_id, review.get("request_id", ""))
        else:
            ok = await finish_result(review["uid"], plan, "APPROVED", grant_id)
        if not ok:
            return False, "BotHost недоступен — заявка осталась в ожидании"
        await commit_approved_payment(review["uid"], review["payment_hash"], plan, grant_id)
        if plan in PAID_SERVICE_PLANS and review.get("request_id") in service_requests:
            service_requests[review["request_id"]]["status"] = "approved"
            save_service_requests()
        guard = load_payment_guard()
        guard.setdefault("hashes", {})[review["profile_hash"]] = {
            "uid": review["uid"], "approved_at": int(datetime.now(timezone.utc).timestamp()),
            "plan_id": plan, "grant_id": grant_id, "type": "profile"}
        save_payment_guard(guard)
    return True, ""


@dp.callback_query(F.data.startswith("review_ok:"))
async def review_ok(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Нет доступа", show_alert=True)
    rid = call.data.split(":", 1)[1]
    review = manual_reviews.get(rid)
    if not review:
        return await call.answer("Заявка уже обработана", show_alert=True)
    ok, err = await approve_review(review)
    if not ok:
        return await call.answer(err, show_alert=True)
    manual_reviews.pop(rid, None)
    save_manual_reviews()
    await call.answer("Одобрено")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(review["uid"], "✅ <b>Проверка вручную одобрена!</b>\n\nПокупка подтверждена, подписка активирована. 💜",
                           parse_mode="HTML")


@dp.callback_query(F.data.startswith("review_no:"))
async def review_no(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Нет доступа", show_alert=True)
    rid = call.data.split(":", 1)[1]
    review = manual_reviews.pop(rid, None)
    if not review:
        return await call.answer("Заявка уже обработана", show_alert=True)
    save_manual_reviews()
    await call.answer("Отклонено")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(review["uid"], "⚠️ <b>Проверка отклонена.</b>\n\nПокупка не начислена. "
                                          "Если считаешь, что это ошибка — свяжись с владельцем.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# 🛠 АДМИНКА
# ═══════════════════════════════════════════════════════════════

def admin_kb():
    cfg = admin_cfg()
    p = "✅" if has_ref("payment") else "❌"
    pr = "✅" if has_ref("profile") else "❌"
    sync = "✅" if sync_ready() else "❌"
    pending = sum(1 for r in manual_reviews.values())
    review_btn = f"🧐 Ручные заявки: {pending}" if pending else "🧐 Ручные заявки"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🧾 Эталон чека {p}", callback_data="adm_payment")],
        [InlineKeyboardButton(text=f"👤 Эталон профиля {pr}", callback_data="adm_profile")],
        [InlineKeyboardButton(text=f"⏱ Допуск времени: {cfg['time_tolerance_minutes']} мин", callback_data="adm_tolerance")],
        [InlineKeyboardButton(text=review_btn, callback_data="adm_reviews")],
        [InlineKeyboardButton(text=f"🔗 Связь с BotHost {sync}", callback_data="adm_diag")],
        [InlineKeyboardButton(text="📊 Статус кассира", callback_data="adm_status")],
        [InlineKeyboardButton(text="🗑 Очистить эталоны", callback_data="adm_clear")],
    ])


@dp.message(Command("admin"))
async def admin_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.set_state(CashierStates.admin_menu)
    await message.answer("🛠 <b>Админка кассира</b>\n\nЗдесь эталоны скринов, допуск времени и диагностика.",
                         reply_markup=admin_kb(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Нет доступа", show_alert=True)
    action = call.data
    await call.answer()

    if action == "adm_payment":
        await state.set_state(CashierStates.admin_payment_example)
        return await call.message.answer("🧾 Пришли ОДИН эталонный скрин чека. Он будет показан покупателю как пример; "
                                         "ИИ использует его только как визуальный ориентир.")
    if action == "adm_profile":
        await state.set_state(CashierStates.admin_profile_example)
        return await call.message.answer("👤 Пришли ОДИН эталонный скрин профиля получателя (username, имя, аватар).")
    if action == "adm_tolerance":
        await state.set_state(CashierStates.admin_tolerance)
        return await call.message.answer(f"⏱ Сейчас допуск {admin_cfg()['time_tolerance_minutes']} минут. "
                                         "Отправь число от 1 до 60.")
    if action == "adm_reviews":
        if not manual_reviews:
            return await call.message.answer("🧐 Заявок на ручную проверку нет.", reply_markup=admin_kb())
        lines = []
        for rid, r in list(manual_reviews.items())[-10:]:
            cfg = PLANS.get(r["plan"]) or PAID_SERVICE_PLANS.get(r["plan"], {})
            lines.append(f"• <code>{rid[:8]}</code> · ID <code>{r['uid']}</code> · {cfg.get('name', r['plan'])}")
        return await call.message.answer("🧐 <b>Ожидают решения</b> (" + str(len(manual_reviews)) + ")\n\n" + "\n".join(lines) +
                                         "\n\nКнопки одобрения есть под каждым сообщением с заявкой.", parse_mode="HTML")
    if action == "adm_status":
        cfg = admin_cfg()
        return await call.message.answer(
            f"📊 <b>Статус кассира</b>\n\n"
            f"Эталон чека: {'есть' if has_ref('payment') else 'нет'}\n"
            f"Эталон профиля: {'есть' if has_ref('profile') else 'нет'}\n"
            f"Допуск времени: {cfg['time_tolerance_minutes']} мин\n"
            f"Связь с BotHost: {'настроена' if sync_ready() else 'НЕ настроена'}\n"
            f"ИИ: {'подключён' if groq_client else 'нет ключа'} ({VISION_MODEL})\n"
            f"Кулдаун после оплаты: {COOLDOWN_SECONDS // 60} мин\n"
            f"Доп. услуги: {'включены' if ENABLE_SERVICES else 'выключены'}\n"
            f"Владелец: @{OWNER_USERNAME}\n"
            f"Тарифов: {len(PLANS)}", parse_mode="HTML")
    if action == "adm_diag":
        return await call.message.answer(await build_diagnostics(), parse_mode="HTML")
    if action == "adm_clear":
        for p in (ref_file("payment"), ref_file("profile")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        _ref_cache["key"] = None
        return await call.message.answer("🗑 Эталоны удалены.", reply_markup=admin_kb())


async def build_diagnostics():
    """Проверка, что кассир реально сможет выдать подписку."""
    lines = ["🧪 <b>Диагностика кассира</b>\n"]
    try:
        me = await bot.get_me()
        lines.append(f"✅ Токен: @{me.username}")
    except Exception as e:
        lines.append(f"❌ Токен: {html.escape(str(e)[:120])}")
    lines.append(f"{'✅' if SYNC_SECRET else '❌'} SYNC_SECRET {'задан' if SYNC_SECRET else 'НЕ задан — подписи не проверяются'}")
    if not SYNC_CHANNEL_ID:
        lines.append("❌ SYNC_CHANNEL_ID не задан — заявки некуда отправлять")
    else:
        try:
            chat = await bot.get_chat(SYNC_CHANNEL_ID)
            lines.append(f"✅ Канал: {html.escape(chat.title or str(SYNC_CHANNEL_ID))}")
        except Exception as e:
            lines.append(f"❌ Канал недоступен: {html.escape(str(e)[:120])} — добавь кассира в канал")
    lines.append(f"{'✅' if groq_client else '❌'} GROQ_API_KEY {'есть' if groq_client else 'НЕ задан'} · модель <code>{VISION_MODEL}</code>")
    lines.append(f"{'✅' if has_ref('payment') else '⚠️'} Эталон чека {'есть' if has_ref('payment') else 'нет (не критично)'}")
    lines.append(f"{'✅' if has_ref('profile') else '⚠️'} Эталон профиля {'есть' if has_ref('profile') else 'нет (не критично)'}")
    lines.append("\n<b>Тарифы кассира</b> (должны совпадать с BotHost):")
    for pid, p in PLANS.items():
        lines.append(f"• <code>{pid}</code> — {p['emoji']} {html.escape(p['name'])} · {p['stars']}⭐ / {p['days']} дн.")
    lines.append("\n⚠️ Если тарифы в BotHost и кассире разошлись — оплата не пройдёт.")
    return "\n".join(lines)


@dp.message(CashierStates.admin_payment_example, F.photo)
async def admin_payment_save(message: types.Message, state: FSMContext):
    info = await bot.get_file(message.photo[-1].file_id)
    await bot.download_file(info.file_path, destination=ref_file("payment"))
    _ref_cache["key"] = None
    await state.set_state(CashierStates.admin_menu)
    await message.answer("✅ Эталон чека сохранён.", reply_markup=admin_kb())


@dp.message(CashierStates.admin_profile_example, F.photo)
async def admin_profile_save(message: types.Message, state: FSMContext):
    info = await bot.get_file(message.photo[-1].file_id)
    await bot.download_file(info.file_path, destination=ref_file("profile"))
    _ref_cache["key"] = None
    await state.set_state(CashierStates.admin_menu)
    await message.answer("✅ Эталон профиля сохранён. Теперь ИИ будет сравнивать второй скрин с этим эталоном.",
                         reply_markup=admin_kb())


@dp.message(CashierStates.admin_tolerance)
async def admin_tolerance_save(message: types.Message, state: FSMContext):
    try:
        value = int((message.text or "").strip())
    except Exception:
        return await message.answer("Введите целое число от 1 до 60.")
    if not 1 <= value <= 60:
        return await message.answer("Введите число от 1 до 60.")
    save_config({"time_tolerance_minutes": value})
    await state.set_state(CashierStates.admin_menu)
    await message.answer(f"✅ Допуск времени установлен: {value} минут.", reply_markup=admin_kb())


@dp.message(Command("set_examples"))
async def legacy_examples(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.set_state(CashierStates.admin_payment_example)
    await message.answer("🧾 Пришли эталонный скрин чека.")


@dp.message(Command("diag"))
async def diag_cmd(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    await message.answer(await build_diagnostics(), parse_mode="HTML")


@dp.message(Command("cancel"))
async def cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("↩️ Текущее действие отменено. Напиши /start или /admin.")


# Последний рубеж: раньше после рестарта кассира присланное фото просто игнорировалось.
@dp.message(F.chat.type == "private", F.photo)
async def stray_photo(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🤔 Не понял, к какой заявке этот скрин.\n\nНажми /start, выбери тариф и пройди шаги заново — "
                         "тогда я приму скрины в правильном порядке.")


@dp.message(F.chat.type == "private")
async def stray_text(message: types.Message, state: FSMContext):
    await message.answer("ℹ️ Напиши /start, чтобы купить подписку, или /admin (владельцу) для настроек.")


# ═══════════════════════════════════════════════════════════════
# 🚀 ЗАПУСК
# ═══════════════════════════════════════════════════════════════

async def main():
    if not CASHIER_TOKEN:
        raise RuntimeError("CASHIER_TOKEN не задан")
    problems = []
    if not SYNC_CHANNEL_ID:
        problems.append("SYNC_CHANNEL_ID")
    if not SYNC_SECRET:
        problems.append("SYNC_SECRET")
    if not groq_client:
        problems.append("GROQ_API_KEY")
    log.info("=" * 60)
    log.info("💳 Кассир BotHost v2.1 | тарифов: %s | модель: %s", len(PLANS), VISION_MODEL)
    log.info("🔗 Канал: %s | секрет: %s | кулдаун: %s мин | услуги: %s",
             SYNC_CHANNEL_ID or "нет", "есть" if SYNC_SECRET else "нет",
             COOLDOWN_SECONDS // 60, "вкл" if ENABLE_SERVICES else "выкл")
    if problems:
        log.error("❌ Не задано: %s — покупки будут отказывать с понятным сообщением.", ", ".join(problems))
    log.info("=" * 60)
    if prune_state():
        log.info("🧹 Старые заявки вычищены")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("delete_webhook: %s", e)
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query", "channel_post"])
            log.warning("polling завершился без исключения; перезапуск через 3 сек")
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except TelegramConflictError:
            log.error("❌ TelegramConflictError: CASHIER_TOKEN опрашивает другой процесс.\n"
                      "   Кассир должен работать РОВНО в одном месте: либо отдельным сервисом, либо внутри BotHost "
                      "(INTERNAL_CASHIER=1), но не там и там.\n"
                      "   Проверь также Replicas = 1 в Railway.")
            raise
        except TelegramUnauthorizedError:
            log.error("❌ CASHIER_TOKEN недействителен или отозван у @BotFather.")
            raise
        except Exception:
            log.exception("❌ polling упал; перезапуск через 5 сек")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("👋 Выход")
