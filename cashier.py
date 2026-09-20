"""
🤖 BotHost AI Cashier v8.0 (Vision AI + CIS Timezones + Anti-Fraud)
"""

import os
import json
import base64
import asyncio
import logging
import hmac
import hashlib
from datetime import datetime, timedelta
from uuid import uuid4
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from groq import AsyncGroq

# ═══════════════════════════════════════════════════════════════
# 🔧 КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════

CASHIER_TOKEN = os.environ.get("CASHIER_TOKEN", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "8269807543"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "ivan_unreal")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# ID канала для связи (начинается с -100)
SYNC_CHANNEL_ID = int(os.environ.get("SYNC_CHANNEL_ID", "0"))
SYNC_SECRET = os.environ.get("SYNC_SECRET", "").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
REF_IMG_PATH = DATA_DIR / "owner_reference.jpg"

DATA_DIR.mkdir(parents=True, exist_ok=True)

PLANS = {
    "week":   {"name": "Неделя",    "stars": 15, "days": 7},
    "2weeks": {"name": "2 недели",  "stars": 25, "days": 14},
    "month":  {"name": "Месяц",     "stars": 50, "days": 30},
}

TIMEZONES = {
    "tz_minsk_msk": {"name": "🇷🇺/🇧🇾 Москва / Минск (UTC+3)", "offset": 3},
    "tz_kaliningrad": {"name": "🇷🇺 Калининград (UTC+2)", "offset": 2},
    "tz_samara": {"name": "🇷🇺 Самара / Баку / Ереван (UTC+4)", "offset": 4},
    "tz_tashkent": {"name": "🇺🇿 Ташкент / Екатеринбург (UTC+5)", "offset": 5},
    "tz_astana": {"name": "🇰🇿 Астана / 🇰🇬 Бишкек (UTC+6)", "offset": 6},
    "tz_novosibirsk": {"name": "🇷🇺 Новосибирск / Красноярск (UTC+7)", "offset": 7},
}

logging.basicConfig(level=logging.INFO, format="[CASHIER] %(asctime)s │ %(levelname)s │ %(message)s")
bot = Bot(token=CASHIER_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class CashierStates(StatesGroup):
    select_tz = State()
    waiting_screenshot = State()
    setting_ref = State()

# ═══════════════════════════════════════════════════════════════
# 🧠 ИИ-ЗРЕНИЕ (GROQ VISION LLAMA-3.2)
# ═══════════════════════════════════════════════════════════════

async def analyze_payment_screenshot(user_img_b64: str, expected_stars: int, tz_offset: int, tz_name: str) -> dict:
    if not groq_client:
        return {"is_valid": False, "reason": "API ключ Groq не настроен на сервере."}

    now_utc = datetime.utcnow()
    user_time = now_utc + timedelta(hours=tz_offset)
    current_time_str = user_time.strftime("%d.%m.%Y %H:%M")

    prompt = f"""
Ты — высокоточная нейросеть-кассир с защитой от мошенничества.
Проанализируй скриншот перевода / подарка Telegram Stars.

КОНТЕКСТ ПРОВЕРКИ:
- Ожидаемый получатель: Telegram профиль '@{OWNER_USERNAME}' (или имя владельца).
- Ожидаемая сумма: НЕ МЕНЕЕ {expected_stars} ⭐ (Telegram Stars).
- Часовой пояс покупателя: {tz_name}.
- Текущее время у покупателя (ориентир): {current_time_str}.

ПРАВИЛА ИЗУЧЕНИЯ СКРИНШОТА:
1. ПЕРЕВОД / ПОДАРОК: На скрине должно быть четко видно успешное действие (отправка подарка / звезд).
2. ПОЛУЧАТЕЛЬ: Получатель совпадает с @{OWNER_USERNAME}.
3. СУММА: Количество звезд совпадает или больше {expected_stars}.
4. ВРЕМЯ: Время на скрине должно быть близким к текущему ({current_time_str}).
5. ДЕТЕКТОР ПОДДЕЛКИ: Проверь скриншот на следы Фотошопа (разные шрифты, неровный текст, смазанные цифры, наложение слоев).

Ответь СТРОГО в формате JSON без разметки markdown:
{{
    "is_valid": true или false,
    "reason": "подробная причина на русском, если false, или 'Оплата подтверждена' если true",
    "detected_stars": количество_звезд_числом
}}
"""

    try:
        response = await groq_client.chat.completions.create(
            model=os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b"),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{user_img_b64}"}}
                    ]
                }
            ],
            temperature=0.1,
            max_completion_tokens=350,
            reasoning_effort="none",
            response_format={"type": "json_object"}
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("\n", 1)[0]
        return json.loads(raw)
    except Exception as e:
        logging.error(f"Vision error: {e}")
        return {"is_valid": False, "reason": f"Ошибка анализа изображения ИИ: {e}"}

# ═══════════════════════════════════════════════════════════════
# 📱 ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    args = message.text.split(" ")
    plan_id = args[1] if len(args) > 1 and args[1] in PLANS else "month"
    
    await state.update_data(plan_id=plan_id)
    await state.set_state(CashierStates.select_tz)

    kb = []
    for code, info in TIMEZONES.items():
        kb.append([InlineKeyboardButton(text=info["name"], callback_data=f"tz:{code}")])

    await message.answer(
        f"💎 <b>Оплата тарифа: {PLANS[plan_id]['name']} ({PLANS[plan_id]['stars']}⭐)</b>\n\n"
        f"🌍 <b>Шаг 1 из 2:</b> Выберите ваш часовой пояс (СНГ):\n"
        f"<i>Это нужно ИИ, чтобы точно проверить время на скриншоте.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("tz:"), CashierStates.select_tz)
async def cb_tz_selected(call: types.CallbackQuery, state: FSMContext):
    code = call.data.split(":")[1]
    tz_info = TIMEZONES.get(code, TIMEZONES["tz_minsk_msk"])
    
    data = await state.get_data()
    plan_id = data.get("plan_id", "month")
    plan = PLANS[plan_id]

    await state.update_data(tz_offset=tz_info["offset"], tz_name=tz_info["name"])
    await state.set_state(CashierStates.waiting_screenshot)

    example_text = (
        f"✅ <b>Часовой пояс установлен:</b> {tz_info['name']}\n\n"
        f"📸 <b>Шаг 2 из 2: Отправьте подтверждение оплаты</b>\n\n"
        f"1️⃣ Переведите <b>{plan['stars']}⭐</b> владельцу: @{OWNER_USERNAME}\n"
        f"2️⃣ Сделайте скриншот чека / отправленного подарка\n"
        f"3️⃣ <b>Отправьте скриншот сюда фотосообщением.</b>\n\n"
        f"💡 <i>Пример хорошего скрина: четко видно получателя @{OWNER_USERNAME}, сумму {plan['stars']}⭐ и время.</i>"
    )
    
    await call.message.edit_text(example_text, parse_mode="HTML")

@dp.message(CashierStates.waiting_screenshot, F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_id = data.get("plan_id", "month")
    expected_stars = PLANS[plan_id]["stars"]
    tz_offset = data.get("tz_offset", 3)
    tz_name = data.get("tz_name", "Москва (UTC+3)")

    msg = await message.answer("⏳ <i>ИИ проверяет изображение и реквизиты… Обычно это занимает несколько секунд.</i>", parse_mode="HTML")

    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        downloaded = await bot.download_file(file_info.file_path)
        img_b64 = base64.b64encode(downloaded.read()).decode("utf-8")

        result = await analyze_payment_screenshot(img_b64, expected_stars, tz_offset, tz_name)

        if result.get("is_valid"):
            grant_id = uuid4().hex
            # ОТПРАВЛЯЕМ КОМАНДУ В ЗАКРЫТЫЙ ТГ-КАНАЛ ДЛЯ БОТ-ХОСТА!
            try:
                await bot.send_message(
                    SYNC_CHANNEL_ID,
                    f"/auto_grant {message.from_user.id} {plan_id} {grant_id} "
                    f"{hmac.new(SYNC_SECRET.encode(), f'{message.from_user.id}:{plan_id}:{grant_id}'.encode(), hashlib.sha256).hexdigest()}"
                )
            except Exception as e:
                logging.error(f"Failed to post to sync channel: {e}")
                await msg.edit_text(
                    "⚠️ <b>Оплата распознана, но слот не удалось автоматически выдать.</b>\n\n"
                    f"Свяжись с @{OWNER_USERNAME} и сообщи об этой ошибке.",
                    parse_mode="HTML"
                )
                return

            await msg.edit_text(
                f"🎉 <b>Оплата успешно подтверждена ИИ!</b>\n\n"
                f"Тариф <b>{PLANS[plan_id]['name']}</b> активирован.\n"
                f"Заходи в основного бота — слот уже выдан!",
                parse_mode="HTML"
            )
            await state.clear()
        else:
            reason = result.get("reason", "Не удалось подтвердить скриншот.")
            await msg.edit_text(
                f"❌ <b>ИИ отклонил скриншот:</b>\n\n"
                f"<i>{reason}</i>\n\n"
                f"Сделай более четкий скриншот и отправь его снова или обратись к @{OWNER_USERNAME}.",
                parse_mode="HTML"
            )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка обработки: {e}")

@dp.message(CashierStates.waiting_screenshot)
async def handle_not_photo(message: types.Message):
    await message.answer("⚠️ Пожалуйста, отправь скриншот <b>как фото (изображение)</b>.")

# ─── УСТАНОВКА ЭТАЛОНА ВЛАДЕЛЬЦЕМ ────────────────────────────

@dp.message(Command("set_reference"))
async def cmd_set_ref(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await state.set_state(CashierStates.setting_ref)
    await message.answer("📸 Отправь эталонный скриншот. Он будет сохранён для дальнейшего расширения проверки.")

@dp.message(CashierStates.setting_ref, F.photo)
async def process_set_ref(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    await bot.download_file(file_info.file_path, destination=REF_IMG_PATH)
    await message.answer("✅ <b>Эталонный скриншот сохранён.</b>")
    await state.clear()

# ═══════════════════════════════════════════════════════════════
# 🎯 ЗАПУСК
# ═══════════════════════════════════════════════════════════════

async def main():
    if not CASHIER_TOKEN:
        raise RuntimeError("CASHIER_TOKEN не задан. Добавьте токен кассира в Variables/Environment.")
    if not GROQ_API_KEY:
        logging.warning("GROQ_API_KEY не задан — ИИ-проверка будет недоступна.")
    if not SYNC_CHANNEL_ID or not SYNC_SECRET:
        logging.warning("SYNC_CHANNEL_ID/SYNC_SECRET не заданы — автоматическая выдача слотов отключена.")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning(f"Не удалось удалить webhook: {e}")
    logging.info("ИИ-Кассир запускается (vision: %s)", os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b"))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())