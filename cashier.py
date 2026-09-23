"""BotHost AI Cashier — payment verification with admin reference images and time/profile checks."""
import os, json, base64, asyncio, logging, hmac, hashlib, io
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
from PIL import Image, ImageOps, ImageDraw

CASHIER_TOKEN=os.environ.get("CASHIER_TOKEN","").strip()
OWNER_ID=int(os.environ.get("OWNER_ID","8269807543"))
OWNER_USERNAME=os.environ.get("OWNER_USERNAME","ivan_unreal").lstrip("@")
GROQ_API_KEY=os.environ.get("GROQ_API_KEY","").strip()
SYNC_CHANNEL_ID=int(os.environ.get("SYNC_CHANNEL_ID","0"))
SYNC_SECRET=os.environ.get("SYNC_SECRET","").strip()
DATA_DIR=Path(os.environ.get("DATA_DIR","/app/data")); DATA_DIR.mkdir(parents=True,exist_ok=True)
EXAMPLES_DIR=DATA_DIR/"cashier_examples"; EXAMPLES_DIR.mkdir(parents=True,exist_ok=True)
CONFIG_FILE=EXAMPLES_DIR/"config.json"
PAYMENT_GUARD_FILE=EXAMPLES_DIR/"payment_guard.json"
COOLDOWN_SECONDS=60*60  # 1 час после успешной оплаты
payment_guard_lock=asyncio.Lock()

PLANS={"week":{"name":"Неделя","stars":15,"days":7},"2weeks":{"name":"2 недели","stars":25,"days":14},"month":{"name":"Месяц","stars":50,"days":30}}
TIMEZONES={"tz_msk":{"name":"🇷🇺 Москва / Минск · UTC+3","offset":3},"tz_utc2":{"name":"UTC+2","offset":2},"tz_utc4":{"name":"UTC+4","offset":4},"tz_utc5":{"name":"UTC+5","offset":5},"tz_utc6":{"name":"UTC+6","offset":6},"tz_utc7":{"name":"UTC+7","offset":7}}
DEFAULT_TOLERANCE=10
logging.basicConfig(level=logging.INFO,format="[CASHIER] %(asctime)s │ %(levelname)s │ %(message)s")
bot=Bot(token=CASHIER_TOKEN); dp=Dispatcher(storage=MemoryStorage())
groq_client=AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class CashierStates(StatesGroup):
    choose_plan=State(); select_tz=State(); waiting_payment=State(); waiting_profile=State()
    admin_menu=State(); admin_payment_example=State(); admin_profile_example=State()
    admin_tolerance=State()

def load_config():
    try:
        if CONFIG_FILE.exists():
            data=json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return {"time_tolerance_minutes":max(1,min(60,int(data.get("time_tolerance_minutes",DEFAULT_TOLERANCE))))}
    except Exception: logging.exception("config read error")
    return {"time_tolerance_minutes":DEFAULT_TOLERANCE}

def save_config(data):
    CONFIG_FILE.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")

def admin_cfg(): return load_config()


def load_payment_guard():
    try:
        if PAYMENT_GUARD_FILE.exists():
            data=json.loads(PAYMENT_GUARD_FILE.read_text(encoding="utf-8"))
            if isinstance(data,dict): return data
    except Exception:
        logging.exception("payment guard read error")
    return {"users":{},"hashes":{}}

def save_payment_guard(data):
    tmp=PAYMENT_GUARD_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    tmp.replace(PAYMENT_GUARD_FILE)

def payment_hash(image_b64):
    try:
        return hashlib.sha256(base64.b64decode(image_b64)).hexdigest()
    except Exception:
        return hashlib.sha256(image_b64.encode()).hexdigest()

def guard_status(uid, image_b64=None):
    data=load_payment_guard(); now=int(datetime.now(timezone.utc).timestamp())
    user=data.get("users",{}).get(str(uid),{})
    approved_at=int(user.get("approved_at",0) or 0)
    left=max(0,COOLDOWN_SECONDS-(now-approved_at)) if approved_at else 0
    if left:
        return False, f"⏳ После одобренной оплаты повторная покупка доступна через <b>{(left+59)//60} мин.</b>"
    if image_b64:
        h=payment_hash(image_b64)
        if h in data.get("hashes",{}):
            return False, "❌ Этот скрин чека уже использовался для одобренной оплаты. Повторно получить слот по нему нельзя."
    return True, ""

async def commit_approved_payment(uid, image_b64, plan_id, grant_id):
    data=load_payment_guard(); now=int(datetime.now(timezone.utc).timestamp())
    h=payment_hash(image_b64)
    data.setdefault("users",{})[str(uid)]={"approved_at":now,"plan_id":plan_id,"grant_id":grant_id,"payment_hash":h}
    data.setdefault("hashes",{})[h]={"uid":uid,"approved_at":now,"plan_id":plan_id,"grant_id":grant_id}
    # Чистим старые записи хешей, но сохраняем защиту от повторов в разумном окне.
    cutoff=now-7*24*3600
    data["hashes"]={k:v for k,v in data["hashes"].items() if int(v.get("approved_at",0) or 0)>=cutoff}
    save_payment_guard(data)

async def make_reference_composite():
    """Объединяет два эталона в ОДНО изображение, чтобы Groq получил 2 user + 1 reference = 3 images."""
    payment=ref_file("payment"); profile=ref_file("profile")
    if not payment.exists() and not profile.exists(): return None
    try:
        imgs=[]
        for label,path in (("ЭТАЛОН ЧЕКА",payment),("ЭТАЛОН ПРОФИЛЯ",profile)):
            if not path.exists(): continue
            im=Image.open(path).convert("RGB")
            im.thumbnail((900,1200),Image.Resampling.LANCZOS)
            canvas=Image.new("RGB",(920,im.height+70),"white")
            canvas.paste(im,((920-im.width)//2,70))
            d=ImageDraw.Draw(canvas); d.text((20,20),label,fill="black")
            imgs.append(canvas)
        if len(imgs)==1:
            out=imgs[0]
        else:
            w=max(x.width for x in imgs); h=sum(x.height for x in imgs)+20
            out=Image.new("RGB",(w,h),"white"); y=0
            for im in imgs:
                out.paste(im,((w-im.width)//2,y)); y+=im.height+10
        buf=io.BytesIO(); out.save(buf,format="JPEG",quality=88)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        logging.exception("reference composite error")
        return None

def ref_file(kind):
    return EXAMPLES_DIR/f"{kind}_reference.jpg"

def has_ref(kind): return ref_file(kind).exists()

def sign(kind,*parts):
    raw=":".join([kind,*map(str,parts)])
    return hmac.new(SYNC_SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()

async def send_channel(text):
    if not SYNC_CHANNEL_ID or not SYNC_SECRET: return False
    try: await bot.send_message(SYNC_CHANNEL_ID,text); return True
    except Exception as e: logging.error("sync channel: %s",e); return False

async def image_b64(message):
    photo=message.photo[-1]; info=await bot.get_file(photo.file_id); data=await bot.download_file(info.file_path)
    return base64.b64encode(data.read()).decode()

async def file_b64(path):
    if not path.exists(): return None
    return base64.b64encode(path.read_bytes()).decode()

async def analyze_two_images(img1,img2,plan_id,tz_offset,tz_name):
    if not groq_client: return {"is_valid":False,"reason":"GROQ_API_KEY не настроен."}
    plan=PLANS[plan_id]; tolerance=admin_cfg()["time_tolerance_minutes"]
    now=datetime.now(timezone.utc)+timedelta(hours=tz_offset); now_s=now.strftime("%d.%m.%Y %H:%M")
    reference_ref=await make_reference_composite()
    prompt=f"""
Ты проверяешь оплату Telegram Stars. Есть ДВА скриншота пользователя.
СКРИН 1 — чек/экран оплаты. СКРИН 2 — профиль/экран получателя.
Владелец платежа: @{OWNER_USERNAME}. Тариф: {plan['name']}, минимум {plan['stars']} Stars.
Текущее локальное время покупателя: {now_s}. Часовой пояс: {tz_name}.
Допуск по времени: не более {tolerance} минут.

ПРАВИЛА ПРОВЕРКИ:
1. На чеке должна быть успешная операция/отправка, сумма не меньше {plan['stars']} Stars, получатель и время.
2. На втором скрине должен быть виден получатель. Username/имя/фото профиля должны согласовываться с владельцем @{OWNER_USERNAME}.
3. Если приложен ЭТАЛОННЫЙ скрин профиля владельца, сравнивай второй скрин с ним: username, имя и аватар должны быть визуально согласованы. Эталон — только ориентир, не доказательство оплаты.
4. Если приложен ЭТАЛОННЫЙ чек, используй его только для понимания ожидаемого вида полей; НЕ считай его платежом пользователя.
5. Время операции на чеке сравнивай с текущим временем {now_s}. Если разница больше {tolerance} минут — отклоняй. Если время не видно или неоднозначно — отклоняй.
6. Сумма должна быть подтверждена именно на чеке пользователя. Не бери сумму из эталона.
7. Не придумывай данные. При сомнении отклоняй.
8. Ищи явные признаки редактирования/подделки, но не утверждай подделку без визуального основания.

Верни строго JSON:
{{"is_valid":true|false,"reason":"кратко по-русски","detected_stars":0,"detected_time":"DD.MM.YYYY HH:MM или null","recipient":"строка или null","time_difference_minutes":0,"profile_match":"yes|no|unclear"}}
"""
    content=[
        {"type":"text","text":prompt},
        {"type":"text","text":"USER PAYMENT SCREENSHOT (основное доказательство):"},
        {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{img1}"}},
        {"type":"text","text":"USER PROFILE/RECIPIENT SCREENSHOT (основное доказательство):"},
        {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{img2}"}},
    ]
    if reference_ref:
        content += [{"type":"text","text":"ADMIN REFERENCE (одно объединённое изображение: сверху/рядом эталон чека и эталон профиля; только для сравнения, не доказательство оплаты):"},{"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{reference_ref}"}}]
    try:
        r=await groq_client.chat.completions.create(
            model=os.environ.get("GROQ_VISION_MODEL","qwen/qwen3.8-27b"),
            messages=[{"role":"user","content":content}],temperature=0.05,max_completion_tokens=500,
            reasoning_effort="none",response_format={"type":"json_object"})
        result=json.loads(r.choices[0].message.content.strip())
        # Server-side guardrails: AI cannot approve when required evidence is clearly absent.
        if result.get("profile_match")=="no": result["is_valid"]=False
        try:
            diff=float(result.get("time_difference_minutes",9999))
            if diff>tolerance: result["is_valid"]=False
        except Exception: pass
        try:
            stars=int(result.get("detected_stars",0))
            if stars < plan["stars"]: result["is_valid"]=False
        except Exception: result["is_valid"]=False
        return result
    except Exception as e:
        logging.exception("vision error")
        return {"is_valid":False,"reason":f"Ошибка анализа изображения ИИ: {e}"}

async def request_from_user(uid,plan_id):
    username=(await bot.get_chat(uid)).username or "-"; sig=sign("request",uid,plan_id,username)
    await send_channel(f"/payment_request {uid} {plan_id} {username} {sig}")

async def finish_result(uid,plan_id,result,grant_id):
    sig=sign("result",uid,plan_id,result,grant_id); await send_channel(f"/payment_result {uid} {plan_id} {result} {grant_id} {sig}")

async def send_examples(chat_id):
    p=ref_file("payment"); pr=ref_file("profile")
    if p.exists(): await bot.send_photo(chat_id,types.FSInputFile(p),caption="📸 Эталон чека: пример того, где должны быть видны сумма, получатель и время.")
    if pr.exists(): await bot.send_photo(chat_id,types.FSInputFile(pr),caption="👤 Эталон профиля: пример профиля получателя, с которым кассир сверяет второй скрин.")

# ---------- User flow ----------
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
    plan=call.data.split(":",1)[1]; await call.answer(); await begin_plan(call.message,state,plan,edit=True)

async def begin_plan(message,state,plan,edit=False):
    allowed,reason=guard_status(message.chat.id)
    if not allowed:
        return await message.answer(reason,parse_mode="HTML")
    await state.update_data(plan_id=plan); await request_from_user(message.chat.id,plan); await state.set_state(CashierStates.select_tz)
    kb=[[InlineKeyboardButton(text=x["name"],callback_data=f"tz:{k}")] for k,x in TIMEZONES.items()]
    text=f"💳 <b>{PLANS[plan]['name']} · {PLANS[plan]['stars']}⭐</b>\n\n🌍 <b>Шаг 1 из 3</b>\nВыбери часовой пояс, чтобы сверить время на чеке."
    if edit: await message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")
    else: await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")

@dp.callback_query(F.data=="info")
async def info(call:types.CallbackQuery):
    await call.answer(); await call.message.edit_text("ℹ️ <b>Как проходит проверка</b>\n\n1. Тариф и часовой пояс.\n2. Скрин чека.\n3. Скрин профиля/получателя.\n4. ИИ сверяет сумму, получателя, профиль и время.\n5. Допуск времени настраивает админ.\n6. Результат уходит в BotHost через защищённый канал.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« К тарифам",callback_data="back_plans")]]),parse_mode="HTML")

@dp.callback_query(F.data=="back_plans")
async def back_plans(call:types.CallbackQuery,state:FSMContext):
    await state.clear(); kb=[[InlineKeyboardButton(text=f"📅 {p['name']} · {p['stars']}⭐",callback_data=f"plan:{k}")] for k,p in PLANS.items()]
    await call.message.edit_text("💳 <b>Кассир BotHost</b>\n\nВыбери тариф — дальше кассир проведёт тебя по шагам.",reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),parse_mode="HTML")

@dp.callback_query(F.data.startswith("tz:"),CashierStates.select_tz)
async def tz_selected(call:types.CallbackQuery,state:FSMContext):
    code=call.data.split(":",1)[1]; tz=TIMEZONES.get(code,TIMEZONES["tz_msk"]); data=await state.get_data(); plan=data["plan_id"]
    await state.update_data(tz_offset=tz["offset"],tz_name=tz["name"]); await state.set_state(CashierStates.waiting_payment); await send_examples(call.from_user.id)
    await call.message.edit_text(f"🌍 <b>{tz['name']}</b>\n\n📸 <b>Шаг 2 из 3 — чек оплаты</b>\n\nОтправь чёткий скрин, где видны: <b>{PLANS[plan]['stars']}⭐+</b>, @{OWNER_USERNAME}, успешная операция и время.\n\nПосле этого я попрошу второй скрин.",parse_mode="HTML")

@dp.message(CashierStates.waiting_payment,F.photo)
async def payment_photo(message:types.Message,state:FSMContext):
    b64=await image_b64(message); await state.update_data(payment_b64=b64); await state.set_state(CashierStates.waiting_profile)
    await message.answer(f"✅ Первый скрин получен.\n\n👤 <b>Шаг 3 из 3</b>\nТеперь отправь второй скрин — профиль/экран получателя @{OWNER_USERNAME}.",parse_mode="HTML")

@dp.message(CashierStates.waiting_profile,F.photo)
async def profile_photo(message:types.Message,state:FSMContext):
    data=await state.get_data(); plan=data["plan_id"]; payment_b64=data.get("payment_b64","")
    allowed,reason=guard_status(message.from_user.id,payment_b64)
    if not allowed:
        await state.clear()
        return await message.answer(reason,parse_mode="HTML")
    msg=await message.answer("⏳ <i>Сверяю 2 твоих скрина + 2 эталона (эталоны будут объединены в одно изображение)…</i>",parse_mode="HTML")
    try:
        result=await analyze_two_images(payment_b64,await image_b64(message),plan,data.get("tz_offset",3),data.get("tz_name","UTC+3"))
        if not result.get("is_valid"):
            await msg.edit_text(f"❌ <b>Оплата не подтверждена</b>\n\n{result.get('reason','Данные не совпали.')}\n\nМожно отправить новый чек и пройти проверку заново.",parse_mode="HTML"); await state.set_state(CashierStates.waiting_payment); return
        # Повторная проверка перед выдачей слота: защищает от двух почти одновременных запросов.
        async with payment_guard_lock:
            allowed,reason=guard_status(message.from_user.id,payment_b64)
            if not allowed:
                await state.clear(); return await msg.edit_text(reason,parse_mode="HTML")
            grant_id=uuid4().hex
            ok=await finish_result(message.from_user.id,plan,"APPROVED",grant_id)
            if not ok:
                await msg.edit_text("⚠️ Оплата проверена, но BotHost сейчас недоступен. Повторять оплату не нужно — обратись к владельцу.",parse_mode="HTML"); return
            await commit_approved_payment(message.from_user.id,payment_b64,plan,grant_id)
        await msg.edit_text(f"🎉 <b>Оплата подтверждена!</b>\n\nТариф: <b>{PLANS[plan]['name']}</b>\nСлот передан BotHost.\n\n⏱ Повторная покупка будет доступна через <b>1 час</b>.",parse_mode="HTML"); await state.clear()
    except Exception as e: await msg.edit_text(f"❌ Ошибка проверки: {e}",parse_mode="HTML")

@dp.message(CashierStates.waiting_payment)
async def need_payment_photo(message:types.Message,state:FSMContext): await message.answer("📸 Отправь именно фото чека, не текст.")
@dp.message(CashierStates.waiting_profile)
async def need_profile_photo(message:types.Message,state:FSMContext): await message.answer(f"👤 Отправь второй скрин профиля/получателя @{OWNER_USERNAME} как фото.")

# ---------- Admin panel ----------
def admin_kb():
    cfg=admin_cfg(); p="✅" if has_ref("payment") else "❌"; pr="✅" if has_ref("profile") else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🧾 Эталон чека {p}",callback_data="adm_payment")],
        [InlineKeyboardButton(text=f"👤 Эталон профиля {pr}",callback_data="adm_profile")],
        [InlineKeyboardButton(text=f"⏱ Допуск времени: {cfg['time_tolerance_minutes']} мин",callback_data="adm_tolerance")],
        [InlineKeyboardButton(text="📊 Статус кассира",callback_data="adm_status")],
        [InlineKeyboardButton(text="🗑 Очистить эталоны",callback_data="adm_clear")],
    ])

@dp.message(Command("admin"))
async def admin_cmd(message:types.Message,state:FSMContext):
    if message.from_user.id!=OWNER_ID: return
    await state.set_state(CashierStates.admin_menu); await message.answer("🛠 <b>Админка кассира</b>\n\nЗдесь можно загрузить эталон чека и эталон профиля, а также настроить допуск по времени.",reply_markup=admin_kb(),parse_mode="HTML")

@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(call:types.CallbackQuery,state:FSMContext):
    if call.from_user.id!=OWNER_ID: await call.answer("Нет доступа",show_alert=True); return
    action=call.data; await call.answer()
    if action=="adm_payment":
        await state.set_state(CashierStates.admin_payment_example); return await call.message.answer("🧾 Пришли ОДИН эталонный скрин чека. Он будет сохранён и показан покупателю как пример; ИИ использует его только как визуальный ориентир.")
    if action=="adm_profile":
        await state.set_state(CashierStates.admin_profile_example); return await call.message.answer("👤 Пришли ОДИН эталонный скрин профиля/получателя. Желательно, чтобы были видны username, имя и аватар.")
    if action=="adm_tolerance":
        await state.set_state(CashierStates.admin_tolerance); return await call.message.answer(f"⏱ Сейчас допуск {admin_cfg()['time_tolerance_minutes']} минут. Отправь число от 1 до 60.")
    if action=="adm_status":
        cfg=admin_cfg(); return await call.message.answer(f"📊 <b>Статус</b>\n\nЭталон чека: {'есть' if has_ref('payment') else 'нет'}\nЭталон профиля: {'есть' if has_ref('profile') else 'нет'}\nДопуск времени: {cfg['time_tolerance_minutes']} мин\nВладелец: @{OWNER_USERNAME}",parse_mode="HTML")
    if action=="adm_clear":
        for p in (ref_file("payment"),ref_file("profile")):
            try: p.unlink()
            except FileNotFoundError: pass
        return await call.message.answer("🗑 Эталоны удалены.",reply_markup=admin_kb())

@dp.message(CashierStates.admin_payment_example,F.photo)
async def admin_payment_save(message:types.Message,state:FSMContext):
    info=await bot.get_file(message.photo[-1].file_id); await bot.download_file(info.file_path,destination=ref_file("payment"))
    await state.set_state(CashierStates.admin_menu); await message.answer("✅ Эталон чека сохранён.",reply_markup=admin_kb())

@dp.message(CashierStates.admin_profile_example,F.photo)
async def admin_profile_save(message:types.Message,state:FSMContext):
    info=await bot.get_file(message.photo[-1].file_id); await bot.download_file(info.file_path,destination=ref_file("profile"))
    await state.set_state(CashierStates.admin_menu); await message.answer("✅ Эталон профиля сохранён. Теперь при проверке ИИ будет сравнивать второй скрин с этим эталоном.",reply_markup=admin_kb())

@dp.message(CashierStates.admin_tolerance)
async def admin_tolerance_save(message:types.Message,state:FSMContext):
    try: value=int(message.text.strip())
    except Exception: return await message.answer("Введите целое число от 1 до 60.")
    if not 1<=value<=60: return await message.answer("Введите число от 1 до 60.")
    save_config({"time_tolerance_minutes":value}); await state.set_state(CashierStates.admin_menu); await message.answer(f"✅ Допуск времени установлен: {value} минут.",reply_markup=admin_kb())

@dp.message(Command("set_examples"))
async def legacy_examples(message:types.Message,state:FSMContext):
    if message.from_user.id!=OWNER_ID: return
    await state.set_state(CashierStates.admin_payment_example); await message.answer("🧾 Пришли эталонный скрин чека.")

@dp.message(Command("cancel"))
async def cancel(message:types.Message,state:FSMContext): await state.clear(); await message.answer("↩️ Текущее действие отменено. Напиши /start или /admin.")

async def main():
    if not CASHIER_TOKEN: raise RuntimeError("CASHIER_TOKEN не задан")
    if not GROQ_API_KEY: logging.warning("GROQ_API_KEY не задан")
    if not SYNC_CHANNEL_ID or not SYNC_SECRET: logging.warning("SYNC_CHANNEL_ID/SYNC_SECRET не заданы")
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("ИИ-Кассир запускается (vision: %s)",os.environ.get("GROQ_VISION_MODEL","qwen/qwen3.8-27b"))
    await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
