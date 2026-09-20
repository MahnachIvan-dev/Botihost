# Деплой BotHost v8.0

## 1. Основной Host
Создайте отдельный Railway service.

Variables:
```text
BOT_TOKEN=НОВЫЙ_ТОКЕН_ОСНОВНОГО_БОТА
OWNER_ID=ВАШ_TELEGRAM_ID
OWNER_USERNAME=ВАШ_USERNAME
GROQ_API_KEY=...
VERIFIER_BOT=BotHostoplatiBot
SYNC_CHANNEL_ID=-100...
SYNC_SECRET=СЛУЧАЙНАЯ_ДЛИННАЯ_СТРОКА
```

Deploy с `Dockerfile`.

**Replicas = 1.**

## 2. ИИ-Кассир
Создайте второй Railway service из того же репозитория, но используйте `Dockerfile.cashier`.

Variables:
```text
CASHIER_TOKEN=НОВЫЙ_ТОКЕН_КАССИРА
OWNER_ID=ВАШ_TELEGRAM_ID
OWNER_USERNAME=ВАШ_USERNAME
GROQ_API_KEY=...
SYNC_CHANNEL_ID=-100...
SYNC_SECRET=ТА ЖЕ СТРОКА
GROQ_VISION_MODEL=qwen/qwen3.8-27b
```

**Replicas = 1.**

## 3. Закрытый канал
Добавьте оба бота в приватный канал и убедитесь, что они могут отправлять/читать нужные сообщения. ID канала должен быть одинаковым в обоих сервисах.

## 4. Если видите Conflict
Не перезапускайте бесконечно. Найдите второй процесс с тем же токеном:
- старый Railway service;
- Replit;
- локальный `python bot.py`;
- второй Railway service/replica;
- webhook + polling конфигурацию.

Оставьте только один polling-процесс.

## 5. Первый запуск
Откройте основной бот → `/start`.

Проверьте:
- меню;
- загрузку `.py`;
- загрузку `.zip`;
- запуск;
- логи;
- авто-рестарт;
- `.env`;
- AI-дебаггер.

Потом проверьте кассира отдельным тестовым платежным сценарием.

## 6. Не храните токены в Git
Все токены — только Railway Variables/Secrets.
