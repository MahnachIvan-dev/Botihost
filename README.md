# BotHost v8.0 — fixed build

## Что исправлено
- Убран секретный токен из исходников: теперь только Environment Variables.
- Исправлен главный `TelegramConflictError`: добавлен локальный lock и понятное сообщение. В облаке должен быть **только 1 экземпляр/replica** каждого polling-бота.
- Кассир переведён с устаревшей Groq Vision-модели на `qwen/qwen3.8-27b`.
- Добавлен JSON mode для ответа Vision.
- Связь кассира с Host защищена HMAC-подписью и идемпотентным `grant_id`; повторная выдача одного платежа не создаёт второй слот.
- Безопасная распаковка ZIP без Zip Slip, ограничения количества файлов и распакованного размера.
- Улучшен поиск точки входа проекта.
- `requirements.txt` устанавливается только при изменении его хеша.
- Улучшены сообщения интерфейса.
- SQLite: WAL + busy timeout для меньшего количества блокировок.
- Сохранены логи, AI-дебаггер, .env, авто-рестарт, промокоды, админка и управление файлами.

## ВАЖНО: TelegramConflictError
Ошибка `Conflict: terminated by other getUpdates request` означает, что **тот же BOT_TOKEN уже получает updates другим процессом**. Удаление webhook не устраняет второй polling-процесс. Telegram допускает либо `getUpdates`, либо webhook, а не два конкурирующих polling-процесса.

На Railway:
1. Для основного Host-сервиса оставьте **1 replica**.
2. Не запускайте этот же `BOT_TOKEN` локально/Replit/Render одновременно.
3. Если старый сервис ещё работает — остановите его.
4. Для кассира нужен отдельный BOT_TOKEN и отдельный Railway service.

## Environment — Host
- `BOT_TOKEN` — токен основного BotHost из @BotFather.
- `OWNER_ID` — Telegram ID владельца.
- `OWNER_USERNAME` — username владельца без @.
- `GROQ_API_KEY` — ключ Groq, если нужен AI-дебаггер.
- `VERIFIER_BOT` — username кассира без @.
- `SYNC_CHANNEL_ID` — ID приватного канала, куда кассир отправляет служебную команду.
- `SYNC_SECRET` — длинная случайная строка, одинаковая у Host и кассира.
- `DATA_DIR` — обычно `/app/data`.

## Environment — Cashier
- `CASHIER_TOKEN` — отдельный токен кассира.
- `OWNER_ID`
- `OWNER_USERNAME`
- `GROQ_API_KEY`
- `SYNC_CHANNEL_ID`
- `SYNC_SECRET`
- `GROQ_VISION_MODEL` — по умолчанию `qwen/qwen3.8-27b`.

## Railway
Для Host: Dockerfile `Dockerfile` или Start Command `python -u bot.py`.

Для Cashier: отдельный сервис с Dockerfile `Dockerfile.cashier` или Start Command `python -u cashier.py`.

Не делайте два сервиса, которые используют один и тот же `BOT_TOKEN`.

## Безопасность
Токены из старой версии были видны прямо в исходнике. Если этот код когда-либо публиковался/передавался третьим лицам, старый токен основного бота и любые другие опубликованные токены следует **отозвать через @BotFather и выпустить новые**.

Скриншотная AI-проверка не является доказательством транзакции уровня Telegram API: модель может ошибиться. Для максимально надёжной оплаты лучше использовать нативные Telegram Stars invoices и `successful_payment`, а AI оставить как вспомогательный механизм проверки.
