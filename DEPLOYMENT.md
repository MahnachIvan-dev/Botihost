# 🚀 ПОЛНАЯ ИНСТРУКЦИЯ ПО ДЕПЛОЮ И ПОДКЛЮЧЕНИЮ

**BotHost — хостинг Telegram-ботов с оплатой через Telegram Stars**

**Стоимость:** $5/месяц (только runner, всё остальное бесплатно!)

---

## 📋 Содержание

1. [Архитектура проекта](#-архитектура-проекта)
2. [Что нужно для старта](#-что-нужно-для-старта)
3. [Регистрация на сервисах](#-регистрация-на-сервисах)
4. [Создание Telegram-бота](#-создание-telegram-бота)
5. [Получение API credentials](#-получение-api-credentials)
6. [Настройка базы данных на Supabase](#-настройка-базы-данных-на-supabase)
7. [Деплой сайта на Vercel](#-деплой-сайта-на-vercel)
8. [Применение схемы БД](#-применение-схемы-бд)
9. [Деплой Runner-сервера на Railway](#-деплой-runner-сервера-на-railway)
10. [Подключение и проверка](#-подключение-и-проверка)
11. [Настройка администратора](#-настройка-администратора)
12. [Как работает оплата через Stars](#-как-работает-оплата-через-stars)
13. [Код Telegram-бота с оплатой](#-код-telegram-бота-с-оплатой)
14. [Тестирование системы](#-тестирование-системы)
15. [Частые проблемы](#-частые-проблемы)

---

## 🏗️ Архитектура проекта

### Как это работает:

```
┌─────────────────────────────────────────────────────────────┐
│                      ПОЛЬЗОВАТЕЛЬ                            │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ Telegram
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              TELEGRAM-БОТ (интерфейс)                        │
│  • Главное меню                                             │
│  • Оплата через Stars (sendInvoice)                         │
│  • Загрузка .py файлов                                      │
│  • Управление ботами                                        │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ API запросы
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              САЙТ (Vercel) = СЕРВЕР                          │
│  • Фронтенд (админка, дашборд)                              │
│  • API endpoints                                            │
│  • Валидация кода ботов                                     │
│  • Хранение кода и метаданных                               │
└─────────────────────────────────────────────────────────────┘
         │                                      │
         │                                      │
         ▼                                      ▼
┌─────────────────────┐              ┌─────────────────────┐
│   Supabase (БД)     │              │   Railway (runner)  │
│   PostgreSQL        │              │   Запуск ботов      │
│   БЕСПЛАТНО         │              │   $5/мес            │
└─────────────────────┘              └─────────────────────┘
```

### Сервисы:

| Сервис | Роль | Стоимость |
|--------|------|-----------|
| **GitHub** | Код проекта | **$0** |
| **Vercel** | Сайт (сервер) | **$0** |
| **Supabase** | База данных PostgreSQL | **$0** |
| **Railway** | Runner (запуск ботов) | **$5/мес** |

**Итого: $5/месяц** (только за runner, всё остальное бесплатно!)

### Ключевые моменты:

✅ **Telegram-бот** — это интерфейс пользователя (не сайт!)  
✅ **Оплата только через Telegram Stars** (валюта XTR)  
✅ **Сайт** — это сервер, который хранит код и управляет запуском  
✅ **Runner** — реально запускает ботов в изолированных контейнерах  
✅ **Звёзды идут напрямую владельцу** бота (без комиссий)  
✅ **Supabase** — бесплатная БД с удобным интерфейсом

---

## 🎯 Что нужно для старта

### Аккаунты (4 штуки):
1. **GitHub** — https://github.com (бесплатно)
2. **Vercel** — https://vercel.com (бесплатно)
3. **Supabase** — https://supabase.com (бесплатно)
4. **Railway** — https://railway.app ($5 trial бесплатно)

### Программы (установить на компьютер):
1. **Git** — https://git-scm.com
2. **Node.js 20+** — https://nodejs.org
3. **VSCode** — https://code.visualstudio.com (опционально)

### Telegram:
1. **Telegram-бот** — создать через @BotFather
2. **Telegram ID** — получить через @userinfobot
3. **API credentials** — получить на my.telegram.org (для подарков)

---

## 📝 Регистрация на сервисах

### 1. GitHub (бесплатно)

1. Откройте https://github.com/signup
2. Введите email, придумайте пароль и username
3. Подтвердите email
4. **Готово!** ✅

---

### 2. Vercel (бесплатно)

1. Откройте https://vercel.com/signup
2. Нажмите **"Continue with GitHub"**
3. Разрешите доступ
4. **Готово!** ✅

---

### 3. Supabase (бесплатно)

1. Откройте https://supabase.com
2. Нажмите **"Start your project"**
3. Выберите **"Sign in with GitHub"**
4. Разрешите доступ
5. **Готово!** ✅

**Supabase даёт бесплатно:**
- 500 MB базы данных
- 5 GB bandwidth
- 2 бесплатных проекта

---

### 4. Railway ($5 trial)

1. Откройте https://railway.app
2. Нажмите **"Start a New Project"**
3. Выберите **"Login with GitHub"**
4. **Railway даёт $5 бесплатно** на первые проекты

---

## 🤖 Создание Telegram-бота

### Шаг 1: Открыть BotFather

1. Откройте Telegram
2. Найдите **@BotFather** (с синей галочкой ✓)
3. Нажмите **Start**

### Шаг 2: Создать бота

Отправьте команду:
```
/newbot
```

BotFather спросит:
1. **Имя бота** (отображаемое): `BotHost - Хостинг ботов`
2. **Username** (должен заканчиваться на `bot`): `MyBotHost_bot`

### Шаг 3: Получить токен

BotFather ответит:
```
Done! Congratulations on your new bot.
Use this token to access the HTTP API:
7123456789:AAHq1234567890abcdefghijklmnopqrstuv
```

**⚠️ СОХРАНИТЕ ЭТОТ ТОКЕН!** Это `BOT_TOKEN`.

### Шаг 4: Настроить команды (опционально)

Отправьте BotFather:
```
/setcommands
```

Выберите своего бота и отправьте:
```
start - Главное меню
buy - Купить слот
upload - Загрузить бота
mybots - Мои боты
promo - Активировать промокод
help - Помощь
```

---

## 🔑 Получение API credentials

**Это нужно для отслеживания подарков владельцу.**

### Шаг 1: Открыть my.telegram.org

1. Откройте https://my.telegram.org
2. Введите **номер телефона владельца** (тот же, что в Telegram)
3. Получите код подтверждения в Telegram
4. Введите код

### Шаг 2: Создать приложение

1. Нажмите **"API development tools"**
2. Если впервые — заполните форму:
   - **App title:** `BotHost Runner`
   - **Short name:** `bothost` (3-32 символа, только маленькие буквы и цифры)
   - **URL:** (оставьте пустым)
   - **Platform:** `Desktop`
   - **Description:** `Runner for hosting Telegram bots`
3. Нажмите **"Create application"**

### Шаг 3: Сохранить credentials

На странице появятся:
```
App api_id: 2937156
App api_hash: a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
```

**⚠️ СОХРАНИТЕ ОБА ЗНАЧЕНИЯ:**
- `OWNER_API_ID` = `2937156`
- `OWNER_API_HASH` = `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6`

---

## 🆔 Получение Telegram ID

1. В Telegram найдите **@userinfobot**
2. Нажмите **Start**
3. Получите свой ID:
```
Id: 5291847362
First: Иван
Lang: ru
```

**⚠️ СОХРАНИТЕ:** `5291847362` — это ваш `OWNER_TELEGRAM_ID` и `ADMIN_TELEGRAM_ID`

---

## 🗄️ Настройка базы данных на Supabase

### Шаг 1: Создать проект

1. Откройте https://supabase.com/dashboard
2. Нажмите **"New Project"**
3. Заполните:
   - **Organization:** выберите вашу организацию
   - **Name:** `bothost`
   - **Database Password:** придумайте надёжный пароль (сохраните его!)
   - **Region:** выберите ближайший (например, Frankfurt)
   - **Pricing Plan:** Free (бесплатный)
4. Нажмите **"Create new project"**
5. Подождите 2-3 минуты пока проект создастся

### Шаг 2: Получить строку подключения

1. В левом меню нажмите **"Project Settings"** (шестерёнка)
2. Выберите **"Database"**
3. Найдите раздел **"Connection string"**
4. Выберите **"Transaction"** pooler
5. Скопируйте строку:

```
postgresql://postgres.abcdefgh:your_password@aws-0-eu-central-1.pooler.supabase.com:6543/postgres
```

**⚠️ СОХРАНИТЕ ЭТУ СТРОКУ КАК `DATABASE_URL`**

### Шаг 3: Проверить подключение

1. В левом меню нажмите **"SQL Editor"**
2. Нажмите **"New query"**
3. Введите: `SELECT 1;`
4. Нажмите **"Run"**
5. Должно показать результат: `1`

**Готово!** ✅ База данных работает.

---

## 🌐 Деплой сайта на Vercel

### Шаг 1: Загрузить код на GitHub

```bash
# На вашем компьютере
cd bothost
git add .
git commit -m "Initial commit"
git push -u origin main
```

### Шаг 2: Импортировать проект в Vercel

1. Откройте https://vercel.com/new
2. Найдите репозиторий `bothost`
3. Нажмите **"Import"**

### Шаг 3: Добавить переменные окружения

В разделе **"Environment Variables"** добавьте:

| Key | Value | Где взять |
|-----|-------|-----------|
| `DATABASE_URL` | `postgresql://postgres.abcdefgh:password@aws-0-eu-central-1.pooler.supabase.com:6543/postgres` | Supabase → Settings → Database → Connection string |
| `ADMIN_TELEGRAM_ID` | `5291847362` | @userinfobot |
| `RUNNER_SECRET` | `mysupersecret123` | Придумайте сами (20+ символов) |
| `BOT_USERNAME` | `MyBotHost_bot` | Username вашего бота БЕЗ @ |
| `RUNNER_URL` | `https://runner.up.railway.app` | Пока оставьте так, обновим позже |

### Шаг 4: Задеплоить

1. Нажмите **"Deploy"**
2. Подождите 2-3 минуты
3. **⚠️ СОХРАНИТЕ URL сайта:** `https://bothost-xxxx.vercel.app`

---

## 🗃️ Применение схемы БД

### На вашем компьютере:

```bash
cd bothost

# Создайте файл .env
echo "DATABASE_URL=postgresql://postgres.abcdefgh:password@aws-0-eu-central-1.pooler.supabase.com:6543/postgres" > .env

# Установите зависимости
npm install

# Примените схему (создаст таблицы в БД)
npx drizzle-kit push
```

### Проверка в Supabase:

1. Откройте https://supabase.com/dashboard
2. Выберите ваш проект `bothost`
3. В левом меню нажмите **"Table Editor"**
4. Должны появиться таблицы:
   - `users`
   - `bots`
   - `subscriptions`
   - `promo_codes`
   - `promo_uses`
   - `payments`

**Готово!** ✅ Схема применена.

---

## 🚀 Деплой Runner-сервера на Railway

### Шаг 1: Создать проект на Railway

1. Откройте https://railway.app
2. Нажмите **"+ New Project"**
3. Выберите **"Deploy from GitHub repo"**
4. Найдите `bothost`

### Шаг 2: Настроить Root Directory

1. После импорта откройте **Settings**
2. Найдите **"Root Directory"**
3. Впишите: `runner`
4. Нажмите **Save**

### Шаг 3: Добавить переменные окружения

В Railway → ваш сервис → **"Variables"**:

| Variable | Value | Где взять |
|----------|-------|-----------|
| `BOT_TOKEN` | `7123456789:AAHq1234567890abcdefghijklmnopqrstuv` | BotFather |
| `OWNER_TELEGRAM_ID` | `5291847362` | @userinfobot |
| `OWNER_PHONE` | `+79991234567` | Номер владельца |
| `OWNER_API_ID` | `2937156` | my.telegram.org |
| `OWNER_API_HASH` | `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6` | my.telegram.org |
| `WEBSITE_URL` | `https://bothost-xxxx.vercel.app` | URL сайта из Vercel |
| `RUNNER_SECRET` | `mysupersecret123` | **ТОТ ЖЕ** что на Vercel! |
| `PORT` | `8000` | Фиксированное значение |

**⚠️ ВАЖНО:** Railway **НЕ** добавляет `DATABASE_URL` автоматически — runner не использует БД напрямую!

### Шаг 4: Получить публичный URL

1. В Railway → ваш runner-сервис
2. Перейдите в **Settings** → **Networking**
3. Нажмите **"Generate Domain"**
4. Получите: `https://bothost-runner-xxxx.up.railway.app`

**⚠️ СОХРАНИТЕ ЭТОТ URL!**

### Шаг 5: Обновить RUNNER_URL на Vercel

1. Откройте https://vercel.com → ваш проект
2. **Settings** → **Environment Variables**
3. Найдите `RUNNER_URL`
4. Измените на: `https://bothost-runner-xxxx.up.railway.app`
5. Нажмите **Save**
6. Перейдите в **Deployments**
7. Нажмите три точки у последнего деплоя → **"Redeploy"**
8. Подождите 2-3 минуты

---

## 🔗 Подключение и проверка

### 1. Проверка сайта

Откройте:
```
https://bothost-xxxx.vercel.app
```

Должна загрузиться главная страница.

### 2. Проверка healthcheck

Откройте:
```
https://bothost-xxxx.vercel.app/api/health
```

Должно показать:
```json
{"ok":true}
```

### 3. Проверка runner'а

Откройте:
```
https://bothost-runner-xxxx.up.railway.app/health
```

Должно показать:
```json
{
  "status": "ok",
  "docker_available": true,
  "running_bots": 0
}
```

### 4. Проверка Telegram-бота

1. Откройте `@MyBotHost_bot` в Telegram
2. Нажмите **Start**
3. Должно появиться приветственное сообщение с кнопками

---

## 👤 Настройка администратора

### Шаг 1: Открыть админку

1. Откройте `https://bothost-xxxx.vercel.app/admin`
2. Введите свой Telegram ID
3. Увидите: "Недостаточно прав"

### Шаг 2: Сделать себя админом через Supabase

1. Откройте https://supabase.com/dashboard
2. Выберите ваш проект
3. В левом меню нажмите **"SQL Editor"**
4. Нажмите **"New query"**
5. Вставьте:

```sql
UPDATE users SET is_admin = true WHERE telegram_id = '5291847362';
```

(замените `5291847362` на свой ID)

6. Нажмите **"Run"**
7. Должно показать: `Success. No rows returned`

### Шаг 3: Перезайти в админку

Перезагрузите страницу `/admin` — теперь должно работать! ✅

---

## ⭐ Как работает оплата через Stars

### Что такое Telegram Stars?

**Telegram Stars (XTR)** — внутренняя валюта Telegram для оплаты цифровых товаров и услуг.

### Как это работает в BotHost:

1. **Пользователь** нажимает "💎 Купить слот" в Telegram-боте
2. **Бот** отправляет invoice через `sendInvoice`:
   - `provider_token` = `""` (пустой для Stars)
   - `currency` = `"XTR"` (Telegram Stars)
   - `amount` = количество звёзд (15, 25 или 50)
3. **Telegram** показывает окно оплаты
4. **Пользователь** подтверждает оплату
5. **Telegram** отправляет `successful_payment` боту
6. **Бот** вызывает API сайта: `POST /api/subscriptions`
7. **Сайт** создаёт подписку в БД
8. **Бот** уведомляет пользователя: "✅ Слот активирован!"
9. **Звёзды поступают владельцу бота** (напрямую, без комиссий)

### Тарифы:

| План | Длительность | Цена | Слотов |
|------|--------------|------|--------|
| Неделя | 7 дней | 15 ⭐ | 1 |
| 2 недели | 14 дней | 25 ⭐ | 1 |
| Месяц | 30 дней | 50 ⭐ | 1 |

### Как вывести звёзды?

Владелец бота может вывести звёзды через:
1. Откройте @BotFather
2. Выберите своего бота
3. **Bot Settings** → **Payments** → **Telegram Stars Balance**
4. Нажмите **"Withdraw Stars"**

---

## 🤖 Код Telegram-бота с оплатой

### Оплата через Stars (ключевой код):

```python
from aiogram.types import LabeledPrice

@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: types.CallbackQuery):
    plan = call.data.split(":")[1]
    plans = {
        "week": ("Неделя", 15),
        "2weeks": ("2 недели", 25),
        "month": ("Месяц", 50),
    }
    title, stars = plans[plan]

    # Отправляем invoice для оплаты Stars
    await call.message.answer_invoice(
        title=f"Слот на {title}",
        description="1 слот для Telegram-бота",
        payload=f"slot_{plan}_{call.from_user.id}",
        provider_token="",  # ПУСТОЙ для Telegram Stars!
        currency="XTR",     # Telegram Stars
        prices=[LabeledPrice(label=f"Слот {title}", amount=stars)],
    )


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    # Подтверждаем pre-checkout
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    # Оплата прошла успешно
    payload = message.successful_payment.invoice_payload
    plan = payload.split("_")[1]
    
    # Создаём подписку через API сайта
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{WEBSITE_URL}/api/subscriptions", json={
            "telegramId": str(message.from_user.id),
            "username": message.from_user.username,
            "planId": plan,
            "confirmed": True,
        }) as r:
            await r.json()

    # Уведомляем владельца о платеже
    await bot.send_message(
        OWNER_ID,
        f"💰 Оплата от {message.from_user.full_name}\n"
        f"План: {plan}, звёзд: {message.successful_payment.total_amount}"
    )

    # Уведомляем пользователя
    await message.answer(
        f"✅ Оплата прошла! Слот активирован.",
        reply_markup=main_kb()
    )
```

**Полный код бота** находится в файле `runner/bot.py` и на странице `/bot-setup` на сайте.

---

## 🧪 Тестирование системы

### Тест 1: Оплата Stars

1. Откройте бота в Telegram
2. Нажмите **"💎 Купить слот"**
3. Выберите тариф (например, "Неделя — 15⭐")
4. Подтвердите оплату
5. Должно прийти: "✅ Оплата прошла! Слот активирован."

**Проверка в Supabase:**
```sql
SELECT * FROM subscriptions ORDER BY created_at DESC LIMIT 1;
```

### Тест 2: Загрузка бота

1. Создайте тестовый файл `test_bot.py`:

```python
import os
from aiogram import Bot, Dispatcher

BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message()
async def echo(message):
    await message.answer(f"Echo: {message.text}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
```

2. Отправьте этот файл боту через `/upload`
3. Должно прийти: "✅ Бот загружен!"

**Проверка в Supabase:**
```sql
SELECT * FROM bots ORDER BY created_at DESC LIMIT 1;
```

### Тест 3: Запуск бота

1. Нажмите **"🤖 Мои боты"**
2. Выберите загруженного бота
3. Нажмите **"▶️ Запустить"**
4. Откройте вашего тестового бота и отправьте сообщение
5. Должен ответить: "Echo: ваше сообщение"

### Тест 4: Админка

1. Откройте `https://bothost-xxxx.vercel.app/admin`
2. Войдите с вашим Telegram ID
3. Проверьте разделы:
   - 📊 Статистика
   - 👥 Пользователи
   - 🤖 Боты
   - 💳 Подписки
   - 🎁 Промокоды

---

## 🆘 Частые проблемы

### ❌ "DATABASE_URL is required"

**Причина:** Не добавлена переменная в Vercel

**Решение:**
1. Vercel → Settings → Environment Variables
2. Добавьте `DATABASE_URL` со строкой из Supabase

---

### ❌ Supabase: "Connection refused"

**Причина:** Используется неправильный pooler

**Решение:**
- Используйте **Transaction** pooler (порт 6543)
- НЕ используйте Direct connection (порт 5432) для Vercel

---

### ❌ Бот не отвечает в Telegram

**Причина:** Runner-сервер не запущен или ошибка в коде

**Решение:**
1. Проверьте логи Railway: ваш сервис → Deployments → последний → View Logs
2. Убедитесь что `BOT_TOKEN` правильный
3. Проверьте что Root Directory = `runner`

---

### ❌ "403 Invalid secret"

**Причина:** `RUNNER_SECRET` разный на Vercel и Railway

**Решение:**
- Убедитесь что значение **одинаковое** в обоих местах

---

### ❌ Оплата Stars не работает

**Причина:** Неправильные параметры в `sendInvoice`

**Решение:**
- `provider_token` должен быть **пустой** (`""`)
- `currency` должен быть `"XTR"`
- Проверьте что у пользователя достаточно звёзд

---

### ❌ Runner: "No Dockerfile found"

**Причина:** Railway не видит Dockerfile

**Решение:**
- В Railway → Settings → Root Directory должно быть: `runner`

---

### ❌ Подарки не отслеживаются

**Причина:** Не настроен Userbot (Telethon)

**Решение:**
- Проверьте что добавлены переменные:
  - `OWNER_PHONE` (с +7)
  - `OWNER_API_ID`
  - `OWNER_API_HASH`
- Номер должен совпадать с my.telegram.org
- При первом запуске Telethon попросит код — смотрите логи Railway

---

### ❌ Supabase: "relation does not exist"

**Причина:** Схема БД не применена

**Решение:**
```bash
npx drizzle-kit push
```

---

### ❌ Админка не работает

**Причина:** Вы не администратор в БД

**Решение:**
Выполните в Supabase SQL Editor:
```sql
UPDATE users SET is_admin = true WHERE telegram_id = 'ВАШ_ID';
```

---

## 📊 Итоговая архитектура

```
┌───────────────┐
│    GitHub     │  ← Код (бесплатно)
└───────────────┘
       │
       ├──────────────────┐
       │                  │
       ▼                  ▼
┌───────────────┐  ┌───────────────┐
│    Vercel     │  │   Railway     │
│   (сайт)      │  │   (runner)    │
│  бесплатно    │  │   $5/мес      │
└───────────────┘  └───────────────┘
       │
       ▼
┌───────────────┐
│   Supabase    │
│    (БД)       │
│  бесплатно    │
└───────────────┘
```

**Всего 4 сервиса, из них 3 бесплатных!** 🎯

---

## 🎉 Готово!

Ваш BotHost полностью работает:

✅ Сайт на Vercel (бесплатно)  
✅ База данных на Supabase (бесплатно)  
✅ Runner-сервер на Railway ($5/мес)  
✅ Telegram-бот с оплатой через Stars  
✅ Админ-панель  
✅ Промокоды  
✅ Валидация кода  

**Стоимость:** ~$5/месяц (только за runner!)

---

## 📞 Поддержка

- **Supabase Docs:** https://supabase.com/docs
- **Vercel Docs:** https://vercel.com/docs
- **Railway Docs:** https://docs.railway.app
- **Telegram Bot API:** https://core.telegram.org/bots/api
- **Telegram Stars:** https://core.telegram.org/bots/payments#telegram-stars
- **aiogram Docs:** https://docs.aiogram.dev

---

**Удачи! 🚀**
