"""BotHost AI Cashier — two-image payment verification + signed channel sync."""
import os, json, base64, asyncio, logging, hmac, hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pathlib import Path
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from groq import AsyncGroq

CASHIER_TOKEN=os.environ.get("CASHIER_TOKEN","").strip()
OWNER_ID=int(os.environ.get("OWNER_ID","8269807543"))
OWNER_USERNAME=os.environ.get("OWNER_USERNAME","ivan_unreal").lstrip("@")
GROQ_API_KEY=os.environ.get("GROQ_API_KEY","").strip()
SYNC_CHANNEL_ID=int(os.environ.get("SYNC_CHANNEL_ID","0"))
SYNC_SECRET=os.environ.get("SYNC_SECRET","").strip()
DATA_DIR=Path(os.environ.get("DATA_DIR","/app/data")); DATA_DIR.mkdir(parents=True,exist_ok=True)
EXAMPLE_PAYMENT=DATA_DIR/"payment_example.jpg"
EXAMPLE_PROFILE=DATA_DIR/"profile_example.jpg"

PLANS={"week":{"name":"Неделя","stars":15,"days":7},"2weeks":{"name":"2 недели","stars":25,"days":14},"month":{"name":"Месяц","stars":50,"days":30}}
TIMEZONES={
 "tz_msk":{"name":"🇷🇺 Москва / Минск · UTC+3","offset":3},
 "tz_utc2":{"name":"UTC+2","offset":2}, "tz_utc4":{"name":"UTC+4","offset":4},
 "tz_utc5":{"name":"UTC+5","offset":5}, "tz_utc6":{"name":"UTC+6","offset":6}, "tz_utc7":{"name":"UTC+7","offset":7}}
logging.basicConfig(level=logging.INFO,format="[CASHIER] %(asctime)s │ %(levelname)s │ %(message)s")
bot=Bot(token=CASHIER_TOKEN); dp=Dispatcher(storage=MemoryStorage())
groq_client=AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class CashierStates(StatesGroup):
    choose_plan=State(); select_tz=State(); waiting_payment=State(); waiting_profile=State(); setting_examples=State()

def sign(kind, *parts):
    raw=":".join([kind,*map(str,parts)])
    return hmac.new(SYNC_SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()

async def send_channel(text):
    if not SYNC_CHANNEL_ID or not SYNC_SECRET: return False
    try: await bot.send_message(SYNC_CHANNEL_ID,text); return True
    except Exception as e: logging.error("sync channel: %s",e); return False

async def image_b64(message):
    photo=message.photo[-1]; info=await bot.get_file(photo.file_id); data=await bot.download_file(info.file_path)
    return base64.b64encode(data.read()).decode()

async def analyze_two_images(img1,img2,plan_id,tz_offset,tz_name):
    if not groq_client: return {"is_valid":False,"reason":"GROQ_API_KEY не настроен."}
    plan=PLANS[plan_id]; now=datetime.now(timezone.utc)+timedelta(hours=tz_offset); now_s=now.strftime("%d.%m.%Y %H:%M")
    prompt=f"""
Ты проверяешь оплату Telegram Stars. Это ДВА скриншота одного пользователя.
СКРИН 1 — чек/экран отправки Stars. СКРИН 2 — профиль/экран получателя.
Владелец: @{OWNER_USERNAME}. Тариф: {plan['name']}, минимум {plan['stars']} Stars.
Текущее время покупателя: {now_s}, часовой пояс: {tz_name}.

ПРАВИЛА:
1. На первом скрине должно быть видно успешное отправление/подарок, сумму не меньше {plan['stars']} Stars и получателя.
2. На втором скрине должен быть тот же получатель; username/имя должно соответствовать @{OWNER_USERNAME}.
3. Время операции на первом скрине должно быть близко к текущему времени. Допустимая погрешность — 10 минут. Если время не видно, отклоняй.
4. Если сумма, получатель, успешность операции или время не подтверждены — отклоняй.
5. Ищи очевидные признаки редактирования/подделки, но не называй скрин поддельным без визуального основания.
6. Не придумывай данные, которых нет на изображении.

Верни строго JSON:
{{"is_valid":true|false,"reason":"кратко по-русски","detected_stars":0,"detected_time":"DD.MM.YYYY HH:MM или null","recipient":"строка или null","time_difference_minutes":0}}
"""
    try:
        r=await groq_client.chat.completions.create(
            model=os.environ.get("GROQ_VISION_MODEL","qwen/qwen3.8-27b"),
            messages=[{"role":"user","content":[
                {"type":"text","text":prompt},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{img1}"}},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{img2}"}}
            ]}],temperature=0.05,max_completion_tokens=450,reasoning_effort="none",response_format={"type":"json_object"})
        return json.loads(r.choices[0].message.content.strip())
    except Exception as e:
        logging.exception("vision error")
        return {"is_valid":False,"reason":f"Ошибка анализа изображения ИИ: {e}"}

async def request_from_user(uid,plan_id):
    username=(await bot.get_chat(uid)).username or "-"
    sig=sign("request",uid,plan_id,username)
    await send_channel(f"/payment_request {uid} {plan_id} {username} {sig}")

async def finish_result(uid,plan_id,result,grant_id):
    sig=sign("result",uid,plan_id,result,grant_id)
    await send_channel(f"/payment_result {uid} {plan_id} {result} {grant_id} {sig}")

async def send_examples(chat_id):
    if EXAMPLE_PAYMENT.exists(): await bot.send_photo(chat_id,types.FSInputFile(EXAMPLE_PAYMENT),caption="📸 Пример чека: здесь должны быть видны сумма, получатель и время.")
    if EXAMPLE_PROFILE.exists(): await bot.send_photo(chat_id,types.FSInputFile(EXAMPLE_PROFILE),caption="👤 Пример второго скрина: профиль/экран получателя.")

@dp.message(CommandStart())
async def start_cmd(message:types.Message,state:FSMContext):
    args=message.text.split(maxsplit=1); plan=args[1].strip() if len(args)>1 and args[1].strip() in PLANS else None
    await state.clear()
    if not plan:
        await state.set_state(CashierStates.choose_plan)
        kb=[[InlineKeyboardButton(text=f"📅 {p['name']} · {p['stars']}⭐",callback_data=f"plan:{k}")] for k,p in PLANS.items()]
        kb.append([InlineKeyboardButton(text="ℹ️ Как проходит проверка",callback_data="info")])
        return await message.answer("💳 <b>Кассир BotHost</b>\n\nВыбери тариф — дальше кассир проведёт тебя по шагам.",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")
    await begin_plan(message,state,plan)

@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(call:types.CallbackQuery,state:FSMContext):
    plan=call.data.split(":",1)[1]
    await call.answer(); await begin_plan(call.message,state,plan,edit=True)

async def begin_plan(message,state,plan,edit=False):
    await state.update_data(plan_id=plan)
    await request_from_user(message.chat.id,plan)
    await state.set_state(CashierStates.select_tz)
    kb=[[InlineKeyboardButton(text=x["name"],callback_data=f"tz:{k}")] for k,x in TIMEZONES.items()]
    text=f"💳 <b>{PLANS[plan]['name']} · {PLANS[plan]['stars']}⭐</b>\n\n🌍 <b>Шаг 1 из 3</b>\nВыбери часовой пояс, чтобы сверить время на чеке."
    if edit: await message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")
    else: await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")

@dp.callback_query(F.data=="info")
async def info(call:types.CallbackQuery):
    await call.answer(); await call.message.edit_text("ℹ️ <b>Как проходит проверка</b>\n\n1. Выбираешь тариф и часовой пояс.\n2. Отправляешь скрин чека.\n3. Отправляешь второй скрин профиля/получателя.\n4. ИИ сверяет сумму, получателя и время с допуском до 10 минут.\n5. Результат уходит в BotHost через защищённый канал.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« К тарифам",callback_data="back_plans")]]),parse_mode="HTML")

@dp.callback_query(F.data=="back_plans")
async def back_plans(call:types.CallbackQuery,state:FSMContext):
    await state.clear()
    kb=[[InlineKeyboardButton(text=f"📅 {p['name']} · {p['stars']}⭐",callback_data=f"plan:{k}")] for k,p in PLANS.items()]
    await call.message.edit_text("💳 <b>Кассир BotHost</b>\n\nВыбери тариф — дальше кассир проведёт тебя по шагам.",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")

@dp.callback_query(F.data.startswith("tz:"),CashierStates.select_tz)
async def tz_selected(call:types.CallbackQuery,state:FSMContext):
    code=call.data.split(":",1)[1]; tz=TIMEZONES.get(code,TIMEZONES["tz_msk"]); data=await state.get_data(); plan=data["plan_id"]
    await state.update_data(tz_offset=tz["offset"],tz_name=tz["name"]); await state.set_state(CashierStates.waiting_payment)
    await send_examples(call.from_user.id)
    await call.message.edit_text(f"🌍 <b>{tz['name']}</b>\n\n📸 <b>Шаг 2 из 3 — чек оплаты</b>\n\nОтправь один чёткий скрин, где видны: <b>{PLANS[plan]['stars']}⭐+</b>, @{OWNER_USERNAME}, успешная операция и время.\n\nПосле этого я попрошу второй скрин.",parse_mode="HTML")

@dp.message(CashierStates.waiting_payment,F.photo)
async def payment_photo(message:types.Message,state:FSMContext):
    b64=await image_b64(message); await state.update_data(payment_b64=b64); await state.set_state(CashierStates.waiting_profile)
    await message.answer("✅ Первый скрин получен.\n\n👤 <b>Шаг 3 из 3</b>\nТеперь отправь второй скрин — профиль/экран получателя @{0}, чтобы подтвердить, что Stars ушли именно владельцу.".format(OWNER_USERNAME),parse_mode="HTML")

@dp.message(CashierStates.waiting_profile,F.photo)
async def profile_photo(message:types.Message,state:FSMContext):
    data=await state.get_data(); plan=data["plan_id"]; msg=await message.answer("⏳ <i>Сверяю два скриншота: получателя, сумму и время…</i>",parse_mode="HTML")
    try:
        result=await analyze_two_images(data["payment_b64"],await image_b64(message),plan,data.get("tz_offset",3),data.get("tz_name","UTC+3"))
        if not result.get("is_valid"):
            await msg.edit_text(f"❌ <b>Оплата не подтверждена</b>\n\n{result.get('reason','Данные не совпали.')}\n\nМожно отправить новый чек и пройти проверку заново.",parse_mode="HTML")
            await state.set_state(CashierStates.waiting_payment); return
        grant_id=uuid4().hex
        ok=await finish_result(message.from_user.id,plan,"APPROVED",grant_id)
        if not ok:
            await msg.edit_text("⚠️ Оплата проверена, но BotHost сейчас недоступен. Повторять оплату не нужно — обратись к владельцу.",parse_mode="HTML"); return
        await msg.edit_text(f"🎉 <b>Оплата подтверждена!</b>\n\nТариф: <b>{PLANS[plan]['name']}</b>\nСлот передан BotHost.\n\nТеперь открой основной BotHost и загрузи проект.",parse_mode="HTML"); await state.clear()
    except Exception as e: await msg.edit_text(f"❌ Ошибка проверки: {e}",parse_mode="HTML")

@dp.message(CashierStates.waiting_payment)
async def need_payment_photo(message:types.Message,state:FSMContext): await message.answer("📸 Отправь именно фото чека, не текст.")
@dp.message(CashierStates.waiting_profile)
async def need_profile_photo(message:types.Message,state:FSMContext): await message.answer(f"👤 Отправь второй скрин профиля/получателя @{OWNER_USERNAME} как фото.")

@dp.message(Command("set_examples"))
async def set_examples(message:types.Message,state:FSMContext):
    if message.from_user.id!=OWNER_ID: return
    await state.set_state(CashierStates.setting_examples)
    await state.update_data(example_index=0)
    await message.answer("📸 Пришли два примера подряд: сначала пример чека, затем пример профиля/получателя.")

@dp.message(CashierStates.setting_examples,F.photo)
async def save_example(message:types.Message,state:FSMContext):
    data=await state.get_data(); idx=data.get("example_index",0); info=await bot.get_file(message.photo[-1].file_id)
    target=EXAMPLE_PAYMENT if idx==0 else EXAMPLE_PROFILE; await bot.download_file(info.file_path,destination=target)
    if idx==0:
        await state.update_data(example_index=1); await message.answer("✅ Первый пример сохранён. Теперь пришли пример профиля/получателя.")
    else:
        await state.clear(); await message.answer("✅ Оба примера сохранены. Теперь кассир будет показывать их покупателям.")

@dp.message(Command("cancel"))
async def cancel(message:types.Message,state:FSMContext): await state.clear(); await message.answer("↩️ Текущее действие отменено. Напиши /start, чтобы начать заново.")

async def main():
    if not CASHIER_TOKEN: raise RuntimeError("CASHIER_TOKEN не задан")
    if not GROQ_API_KEY: logging.warning("GROQ_API_KEY не задан")
    if not SYNC_CHANNEL_ID or not SYNC_SECRET: logging.warning("SYNC_CHANNEL_ID/SYNC_SECRET не заданы")
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("ИИ-Кассир запускается (vision: %s)",os.environ.get("GROQ_VISION_MODEL","qwen/qwen3.8-27b"))
    await dp.start_polling(bot)
if __name__=="__main__": asyncio.run(main())
