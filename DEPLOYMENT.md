# BotHost v9.0 — надёжный деплой

## Основной BotHost
Создай **один** Railway service из этого репозитория и используй `Dockerfile`.

Environment Variables:
```text
BOT_TOKEN=токен основного BotHost
OWNER_ID=твой Telegram ID
OWNER_USERNAME=твой username
GROQ_API_KEY=ключ Groq (необязательно, для AI-дебаггера)
VERIFIER_BOT=BotHostoplatiBot
SYNC_CHANNEL_ID=-100...
SYNC_SECRET=длинная случайная строка
DATA_DIR=/app/data
```

**Replicas = 1.** Не запускай второй экземпляр с тем же `BOT_TOKEN`.

## Кассир отдельным Railway service
Создай второй service из того же репозитория и задай Dockerfile Path: `Dockerfile.cashier`.

Variables кассира:
```text
CASHIER_TOKEN=токен кассира
OWNER_ID=твой Telegram ID
OWNER_USERNAME=твой username
GROQ_API_KEY=ключ Groq
SYNC_CHANNEL_ID=-100...
SYNC_SECRET=та же длинная строка, что и у Host
DATA_DIR=/app/data
```

Для кассира также **1 replica**.

Кассир не использует SQLite для работы с пользователями. Он отправляет подтверждённые платежи в sync-канал; Host уже атомарно выдаёт слот в своей базе.

## Важно
- Не хранить токены в `bot.py`.
- Не копировать `bot.db` между двумя сервисами.
- `SYNC_SECRET` должен совпадать у Host и кассира.
- Оба бота должны быть добавлены в закрытый sync-канал с правом отправки/чтения сообщений.
