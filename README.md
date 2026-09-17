# Бот мэрии — Telegram + WhatsApp + CRM

Три программы работают вместе:

- **`bot.py`** — Telegram-бот (этот репозиторий)
- **`whatsapp_bot.py`** — WhatsApp-бот (этот репозиторий)
- **`manas-crm`** — панель управления, куда попадают все обращения
  ([отдельный репозиторий, на GitHub называется tg-bot_srm](https://github.com/q-werty452/tg-bot_srm))

Жители пишут боту в Telegram или WhatsApp — оба канала сходятся в одну и ту
же панель, сотрудники отвечают из одного места.

---

## Быстрый запуск

Каждая программа — свой процесс, свой терминал. Порядок запуска не важен.

### 1. Панель (CRM) — обязательна

```bash
cd manas-crm
./venv/bin/python manage.py migrate
./venv/bin/python manage.py runserver
```

Откроется на `http://127.0.0.1:8000`. Логин создаётся командой
`./venv/bin/python manage.py createsuperuser` (один раз).

### 2. Telegram-бот

```bash
cd tg_bot-
cp .env.example .env   # один раз, потом вписать токены
.venv/bin/python bot.py
```

Минимум в `.env`: `TELEGRAM_BOT_TOKEN` (от [@BotFather](https://t.me/BotFather))
и один ключ ИИ (`OPENAI_API_KEY` / `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY`).

### 3. WhatsApp-бот — по желанию

```bash
cd tg_bot-
.venv/bin/python whatsapp_bot.py
```

Нужны ключи Meta в `.env` (`META_VERIFY_TOKEN`, `META_ACCESS_TOKEN`,
`META_APP_SECRET`, `META_PHONE_NUMBER_ID`) и публичный HTTPS-адрес для
вебхука (для теста — `ngrok http 8081`). Без этих ключей процесс сразу
откажется стартовать с понятной ошибкой — Telegram-бот при этом никак не
страдает, это два независимых процесса.


## Проверка, что всё работает

```bash
cd tg_bot-  && .venv/bin/python selftest.py        # без ключей и интернета
cd manas-crm && ./venv/bin/python manage.py test    # без ключей и интернета
```

Оба должны закончиться без ошибок — это самый быстрый способ убедиться,
что ничего не сломалось перед запуском.

---

## Подробнее

Подробный разбор файлов бота, режимов ответа, настроек `.env` и разбор
частых неполадок — в `docs/details.md` (перенесено туда из этого файла,
чтобы сам README оставался коротким).
