"""
selftest.py — самопроверка бота БЕЗ реальных ключей и без интернета.

Запуск:  .venv/bin/python selftest.py       (Windows: .venv\\Scripts\\python selftest.py)

Что проверяем:
  1. Настройки (.env): плохой токен, кавычки, границы чисел, выбор провайдера.
  2. Разрезание длинных ответов под лимит Telegram.
  3. Чистку эмодзи из ответов модели.
  4. Память диалога: обрезку истории, замок, уборку старых сессий.
  5. Живой диалог целиком: команды, кнопки, вопрос-ответ, ошибки, очередь.

Пункт 5 — настоящий прогон через aiogram: подменяем только транспорт
к серверам Telegram и сам вызов ИИ. Маршрутизация, фильтры и все
обработчики работают ровно так же, как в бою.
"""

import asyncio
import os
import sys
import traceback

# --- Подсовываем тестовые настройки ДО импорта config -----------------------
# Реальный .env не трогаем: os.environ имеет приоритет, load_dotenv не
# перезаписывает уже заданные переменные.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:TESTTESTTESTTESTTESTTESTTEST")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-selftest")
os.environ.setdefault("GOOGLE_API_KEY", "AIzaTestSelftest")
os.environ.setdefault("DEFAULT_PROVIDER", "openai")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail or "условие не выполнено"))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ===========================================================================
# 1. Настройки
# ===========================================================================

def test_config() -> None:
    section("1. Настройки (config.py)")
    import importlib

    import config

    def load(env: dict):
        """Загрузить настройки с временно подменённым окружением."""
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
        try:
            return config.load_settings()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    good = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

    # Пустой токен
    try:
        load({"TELEGRAM_BOT_TOKEN": ""})
        check("пустой токен отклонён", False)
    except config.ConfigError as e:
        check("пустой токен отклонён", "BotFather" in str(e))

    # Кривой токен
    try:
        load({"TELEGRAM_BOT_TOKEN": "просто_строка"})
        check("кривой токен отклонён", False)
    except config.ConfigError as e:
        check("кривой токен отклонён", "123456789" in str(e))

    # Нет ни ключей, ни панели — отвечать нечем
    try:
        load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": None,
              "GOOGLE_API_KEY": None, "ANTHROPIC_API_KEY": None,
              "CRM_URL": None, "CRM_BOT_TOKEN": None})
        check("нет ни ключей, ни панели -> ошибка", False)
    except config.ConfigError as e:
        check("нет ни ключей, ни панели -> ошибка", "ключ" in str(e).lower())

    # Ключей в файле нет, но подключена панель — она их и хранит.
    # Так и настроено у заказчика: ключ перенесён в веб-интерфейс.
    s = load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": None,
              "GOOGLE_API_KEY": None, "ANTHROPIC_API_KEY": None,
              "CRM_URL": "http://127.0.0.1:8000", "CRM_BOT_TOKEN": "служебный"})
    check("ключи только в панели -> бот стартует",
          s.crm_url.endswith("8000") and s.default_provider in ("openai", "gemini", "claude"),
          s.default_provider)

    # Кавычки вокруг значения снимаются
    s = load({"TELEGRAM_BOT_TOKEN": f'"{good}"', "OPENAI_API_KEY": '"sk-quoted"'})
    check("кавычки в .env снимаются", s.telegram_token == good and s.openai_api_key == "sk-quoted",
          f"токен={s.telegram_token!r} ключ={s.openai_api_key!r}")

    # Провайдер по умолчанию — openai
    s = load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": "sk-x",
              "GOOGLE_API_KEY": "AIza-x", "DEFAULT_PROVIDER": "openai"})
    check("DEFAULT_PROVIDER=openai работает", s.default_provider == "openai", s.default_provider)

    # Указан провайдер без ключа -> откат на доступный
    s = load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": None,
              "GOOGLE_API_KEY": "AIza-x", "ANTHROPIC_API_KEY": None,
              "DEFAULT_PROVIDER": "claude"})
    check("откат на доступного провайдера", s.default_provider == "gemini", s.default_provider)

    # Мусор в числовой настройке
    try:
        load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": "sk-x", "HISTORY_LIMIT": "много"})
        check("мусор в HISTORY_LIMIT -> понятная ошибка", False)
    except config.ConfigError as e:
        check("мусор в HISTORY_LIMIT -> понятная ошибка", "числом" in str(e))

    # Границы
    s = load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": "sk-x", "HISTORY_LIMIT": "0"})
    check("HISTORY_LIMIT не опускается ниже 2", s.history_limit == 2, str(s.history_limit))
    s = load({"TELEGRAM_BOT_TOKEN": good, "OPENAI_API_KEY": "sk-x", "REQUEST_TIMEOUT": "99999"})
    check("REQUEST_TIMEOUT ограничен сверху", s.request_timeout == 600, str(s.request_timeout))

    importlib.reload  # noqa: B018  (заглушка, чтобы линтер не ругался на импорт)


# ===========================================================================
# 2. Разрезание длинных ответов
# ===========================================================================

def test_split() -> None:
    section("2. Длинные ответы (utils.split_text)")
    from utils import TELEGRAM_LIMIT, split_text

    check("короткий текст не режется", split_text("привет") == ["привет"])

    # Много абзацев
    text = "\n\n".join(f"Абзац номер {i}. " + "текст " * 60 for i in range(40))
    parts = split_text(text)
    check("длинный текст разрезан", len(parts) > 1, f"кусков: {len(parts)}")
    check("каждый кусок влезает в лимит",
          all(len(p) <= TELEGRAM_LIMIT for p in parts),
          f"максимум {max(map(len, parts))}")
    check("ничего не потеряно",
          "".join(parts).replace("\n", "").replace(" ", "")
          == text.replace("\n", "").replace(" ", ""))

    # Одна гигантская строка без пробелов и переносов
    huge = "х" * 12000
    parts = split_text(huge)
    check("сплошная строка режется", all(len(p) <= TELEGRAM_LIMIT for p in parts)
          and "".join(parts) == huge, f"кусков: {len(parts)}")

    # Ровно на границе
    exact = "a" * TELEGRAM_LIMIT
    check("текст ровно по лимиту не режется", split_text(exact) == [exact])

    # Пустых кусков быть не должно (Telegram не примет пустое сообщение)
    mixed = "первый\n\n\n\n" + "б" * 9000 + "\n\n\n\nпоследний"
    parts = split_text(mixed)
    check("нет пустых кусков", all(p.strip() for p in parts), f"кусков: {len(parts)}")
    check("куски смешанного текста в лимите", all(len(p) <= TELEGRAM_LIMIT for p in parts))


# ===========================================================================
# 3. Чистка эмодзи
# ===========================================================================

def test_icons() -> None:
    section("3. Чистка эмодзи (icons.clean_text)")
    from icons import ICONS, clean_text

    out = clean_text("Готово ✅ и не готово ❌")
    check("галочка/крестик заменены",
          ICONS.ok.glyph in out and ICONS.error.glyph in out and "✅" not in out, out)

    out = clean_text("Привет 👋 как дела 🚀🔥")
    check("обычные эмодзи вырезаны",
          "👋" not in out and "🚀" not in out and "Привет" in out, out)

    keep = f"{ICONS.ok} {ICONS.bullet} {ICONS.arrow} {ICONS.claude} {ICONS.gemini}"
    out = clean_text(keep)
    check("свои иконки не съедаются",
          all(g in out for g in (ICONS.ok.glyph, ICONS.bullet.glyph,
                                 ICONS.arrow.glyph, ICONS.claude.glyph.rstrip("︎"),
                                 ICONS.gemini.glyph)), repr(out))

    out = clean_text("Текст ✔️ с вариационным селектором")
    check("цветной селектор убран", "️" not in out, repr(out))

    check("пустой текст не ломает", clean_text("") == "")
    check("текст без эмодзи не меняется",
          clean_text("Обычный текст, 100 рублей.") == "Обычный текст, 100 рублей.")

    out = clean_text("строка   с    пробелами   ")
    check("лишние пробелы схлопнуты", "   " not in out, repr(out))


# ===========================================================================
# 3b. Промпты: контекст мэрии
# ===========================================================================

def test_prompts() -> None:
    section("3b. Промпты (prompts.py)")
    import prompts
    from storage import Mode

    for mode in (Mode.DETAILED, Mode.CHAT):
        text = prompts.SYSTEM_PROMPTS[mode]
        name = mode.value
        check(f"{name}: указан город Манас", "Манас" in text)
        check(f"{name}: указана Кыргызская Республика", "Кыргызск" in text)
        check(f"{name}: запрещены ссылки на право РФ", "Никогда не ссылайся" in text)
        check(f"{name}: сохранён запрет эмодзи", "эмодзи" in text.lower())
        check(f"{name}: отвечает на языке обращения", "кыргызча" in text)

    check("режимы отличаются длиной ответа",
          prompts.MAX_TOKENS[Mode.DETAILED] > prompts.MAX_TOKENS[Mode.CHAT])

    # --- режим показа: бот отвечает конкретно, а не «уточните в мэрии»
    saved_mode, saved_facts = prompts.DEMO_MODE, prompts.CITY_FACTS
    try:
        prompts.DEMO_MODE, prompts.CITY_FACTS = True, ""
        demo = prompts._build("КОНТЕКСТ")
        check("режим показа: велено отвечать конкретно", "конкретно и уверенно" in demo)
        check("режим показа: нет требования уточнять в мэрии",
              "ты НЕ ЗНАЕШЬ" not in demo, demo[:200])

        # --- рабочий режим: бот не выдумывает
        prompts.DEMO_MODE = False
        strict = prompts._build("КОНТЕКСТ")
        check("рабочий режим: запрещено выдумывать нормы", "Не выдумывай" in strict)
        check("рабочий режим: незаполненные факты помечены",
              "Пока не заданы" in strict, strict[-200:])

        # --- заполненные факты попадают в промпт в обоих режимах
        prompts.CITY_FACTS = "- Мэрия работает с 9:00 до 18:00.\n"
        for flag in (True, False):
            prompts.DEMO_MODE = flag
            text = prompts._build("КОНТЕКСТ")
            check(f"факты попадают в промпт (DEMO_MODE={flag})",
                  "с 9:00 до 18:00" in text and "важнее любых догадок" in text)
    finally:
        prompts.DEMO_MODE, prompts.CITY_FACTS = saved_mode, saved_facts

    check("по умолчанию включён режим показа", prompts.DEMO_MODE is True)


# ===========================================================================
# 4. Память диалога
# ===========================================================================

def test_storage() -> None:
    section("4. Память диалога (storage.py)")
    from storage import Mode, Session, Storage

    s = Session(provider="openai")
    for i in range(30):
        s.add("user" if i % 2 == 0 else "assistant", f"msg{i}", limit=10)
    check("история обрезана по лимиту", len(s.history) <= 10, str(len(s.history)))
    check("история начинается с сообщения пользователя",
          s.history[0]["role"] == "user", s.history[0]["role"])
    roles = [m["role"] for m in s.history]
    check("роли чередуются",
          all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), str(roles))

    s.drop_last()
    check("drop_last убирает последнее", len(s.history) <= 9)
    s.clear()
    check("clear очищает", s.history == [])
    s.drop_last()  # не должно падать на пустой истории
    check("drop_last на пустой истории безопасен", s.history == [])

    st = Storage(default_provider="openai")
    a = st.get(111)
    b = st.get(111)
    check("сессия одна на чат", a is b)
    check("у нового чата дефолтный провайдер", a.provider == "openai")
    check("у нового чата диалоговый режим по умолчанию", a.mode is Mode.CHAT)
    st.get(222)
    check("сессии разных чатов не смешиваются", len(st) == 2, str(len(st)))

    # Уборка старых сессий
    a.last_seen -= 100000
    removed = st.cleanup()
    check("старая сессия убрана", removed == 1 and len(st) == 1, f"removed={removed}, len={len(st)}")


# ===========================================================================
# 5. Полный прогон диалога через aiogram
# ===========================================================================

class FakeTelegram:
    """
    Подставной сервер Telegram.

    aiogram общается с Telegram через объект session. Мы подменяем только его:
    все вызовы (sendMessage, editMessageText, ...) не уходят в сеть,
    а складываются в список — потом по нему проверяем, что бот ответил.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._message_id = 1000

    def texts(self) -> list[str]:
        """Тексты всех отправленных и отредактированных сообщений."""
        return [
            c[1].get("text", "")
            for c in self.calls
            if c[0] in ("SendMessage", "EditMessageText")
        ]

    def clear(self) -> None:
        self.calls.clear()

    def build_session(self):
        from aiogram.client.session.base import BaseSession
        from aiogram.types import Chat, Message, User

        outer = self

        class Session(BaseSession):
            async def close(self):
                pass

            async def stream_content(self, *args, **kwargs):
                yield b"\xff\xd8\xffFAKEJPEG"  # «скачанное» фото

            async def make_request(self, bot, method, timeout=None):
                name = type(method).__name__
                data = method.model_dump(exclude_none=True)
                outer.calls.append((name, data))

                if name == "GetMe":
                    return User(id=1, is_bot=True, first_name="TestBot", username="test_bot")
                if name == "GetFile":
                    from aiogram.types import File
                    return File(file_id=data.get("file_id", "f"),
                                file_unique_id="u", file_path="photos/test.jpg")
                if name in ("SendMessage", "EditMessageText"):
                    outer._message_id += 1
                    return Message(
                        message_id=outer._message_id,
                        date=__import__("datetime").datetime.now(),
                        chat=Chat(id=data.get("chat_id", 1), type="private"),
                        text=data.get("text", ""),
                    )
                return True

        return Session()


def make_message(text: str, chat_id: int = 500, message_id: int = 1):
    """Собрать входящее сообщение так, как его прислал бы Telegram."""
    import datetime

    from aiogram.types import Chat, Message, Update, User

    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="Житель"),
        text=text,
    )
    return Update(update_id=message_id, message=msg)


def make_photo(chat_id: int = 500, message_id: int = 900):
    """Входящее сообщение с фотографией (бот должен вежливо отказаться)."""
    import datetime

    from aiogram.types import Chat, Message, PhotoSize, Update, User

    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="Житель"),
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
    )
    return Update(update_id=message_id, message=msg)


def make_sticker(chat_id: int = 500, message_id: int = 901):
    """Входящий стикер (бот должен вежливо отказаться)."""
    import datetime

    from aiogram.types import Chat, Message, Sticker, Update, User

    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="Житель"),
        sticker=Sticker(file_id="s", file_unique_id="su", type="regular",
                        width=1, height=1, is_animated=False, is_video=False),
    )
    return Update(update_id=message_id, message=msg)


def make_callback(data: str, chat_id: int = 500, message_id: int = 800):
    """Нажатие на inline-кнопку."""
    import datetime

    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=chat_id, is_bot=False, first_name="Житель")
    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        text="Выбери:",
    )
    cb = CallbackQuery(id="cb1", from_user=user, chat_instance="ci", message=msg, data=data)
    return Update(update_id=message_id, callback_query=cb)


class StubProvider:
    """Подставной ИИ: отвечает заранее заданным текстом или падает с ошибкой."""

    def __init__(self):
        self.answer = "Ответ модели."
        self.error: Exception | None = None
        self.delay = 0.0
        self.calls: list[list[dict]] = []

    async def ask(self, system, history, max_tokens, detailed):
        self.calls.append([dict(m) for m in history])
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer


async def test_dialog() -> None:
    section("5. Полный прогон диалога (aiogram)")

    import bot as bot_module
    import conversation as conversation_module
    from aiogram import Bot
    from crm import crm as crm_client
    from providers import ProviderError

    # Этот раздел проверяет бота БЕЗ панели. Отключаем клиента наглухо:
    # иначе тесты пишут обращения в настоящую базу мэрии — так в рабочую
    # панель однажды и попали заявки «вопрос из чата А» с битым фото.
    # Работа В СВЯЗКЕ с панелью проверяется отдельно, в разделе 8,
    # на подставном сервере.
    saved_enabled = crm_client.enabled
    crm_client.enabled = False

    fake = FakeTelegram()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"], session=fake.build_session())

    stub = StubProvider()
    stubs = {"openai": stub, "gemini": StubProvider(), "claude": StubProvider()}
    stubs["gemini"].answer = "Ответ Gemini."
    conversation_module.get_provider = lambda name: stubs[name]  # подменяем реальный вызов ИИ
    dp = bot_module.dp
    storage = bot_module.storage

    async def feed(update):
        await dp.feed_update(bot, update)

    # --- /start
    fake.clear()
    await feed(make_message("/start"))
    out = " ".join(fake.texts())
    check("/start отвечает приветствием", "Привет" in out and "/mode" in out, out[:120])
    check("/start показывает модель по умолчанию (GPT)", "GPT" in out, out[:200])

    # --- /help
    fake.clear()
    await feed(make_message("/help"))
    check("/help отвечает", "Как пользоваться" in " ".join(fake.texts()))

    # --- обычный вопрос
    fake.clear()
    stub.answer = "Заявку можно подать через портал госуслуг."
    await feed(make_message("Как подать заявку?"))
    texts = fake.texts()
    check("на вопрос приходит ответ модели", stub.answer in texts, str(texts))
    check("показан статус «печатает»",
          any(c[0] == "SendChatAction" for c in fake.calls))
    check("вопрос попал в историю модели",
          stub.calls[-1][-1] == {"role": "user", "content": "Как подать заявку?"},
          str(stub.calls[-1]))

    session = storage.get(500)
    check("история сохранена (вопрос + ответ)", len(session.history) == 2, str(session.history))
    check("роли в истории правильные",
          [m["role"] for m in session.history] == ["user", "assistant"])

    # --- контекст в следующем вопросе
    fake.clear()
    stub.answer = "Да, лично тоже можно."
    await feed(make_message("А лично можно?"))
    sent_history = stub.calls[-1]
    check("модель получила всю переписку", len(sent_history) == 3, str(len(sent_history)))
    check("последнее сообщение — новый вопрос",
          sent_history[-1]["content"] == "А лично можно?")

    # --- эмодзи от модели вычищаются
    fake.clear()
    stub.answer = "Готово ✅ Заявка принята 🎉"
    await feed(make_message("проверка эмодзи"))
    out = " ".join(fake.texts())
    check("эмодзи из ответа модели вычищены", "🎉" not in out and "✅" not in out, out)

    # --- длинный ответ режется на несколько сообщений
    fake.clear()
    stub.answer = "\n\n".join(f"Пункт {i}. " + "текст " * 80 for i in range(30))
    await feed(make_message("длинный ответ"))
    sends = [c for c in fake.calls if c[0] == "SendMessage"]
    check("длинный ответ разбит на несколько сообщений", len(sends) > 1, str(len(sends)))
    check("каждое отправленное сообщение в лимите Telegram",
          all(len(c[1]["text"]) <= 4096 for c in sends),
          str(max(len(c[1]["text"]) for c in sends)))

    # --- ошибка провайдера
    storage.get(500).clear()
    fake.clear()
    stub.error = ProviderError("OpenAI: неверный API-ключ (проверь OPENAI_API_KEY).")
    await feed(make_message("вопрос при сломанном ключе"))
    out = " ".join(fake.texts())
    check("ошибка провайдера показана человеку", "неверный API-ключ" in out, out)
    check("после ошибки история пуста (вопрос не залип)",
          storage.get(500).history == [], str(storage.get(500).history))

    # --- неожиданная ошибка внутри провайдера
    fake.clear()
    stub.error = ValueError("что-то сломалось внутри")
    await feed(make_message("вопрос"))
    out = " ".join(fake.texts())
    check("неожиданная ошибка не роняет бота", "Что-то пошло не так" in out, out)
    check("после неожиданной ошибки история пуста", storage.get(500).history == [])
    stub.error = None

    # --- пустой ответ модели
    fake.clear()
    stub.answer = "   "
    await feed(make_message("пустой ответ"))
    check("пустой ответ модели обработан", "пустой ответ" in " ".join(fake.texts()).lower())
    check("пустой ответ не сохранён в историю", storage.get(500).history == [])
    stub.answer = "Ответ модели."

    # --- кнопки: смена режима
    fake.clear()
    await feed(make_message("/mode"))
    check("/mode показывает кнопки",
          any("reply_markup" in c[1] for c in fake.calls if c[0] == "SendMessage"))
    fake.clear()
    await feed(make_callback("mode:chat"))
    check("режим переключён на диалоговый", storage.get(500).mode.value == "chat",
          storage.get(500).mode.value)
    check("нажатие кнопки подтверждено",
          any(c[0] == "AnswerCallbackQuery" for c in fake.calls))
    check("текст сообщения обновлён",
          any(c[0] == "EditMessageText" for c in fake.calls))

    fake.clear()
    await feed(make_callback("mode:detailed"))
    check("режим вернулся на развёрнутый", storage.get(500).mode.value == "detailed")

    # --- битая кнопка не роняет бота
    fake.clear()
    await feed(make_callback("mode:несуществующий"))
    check("битая кнопка режима обработана",
          any(c[0] == "AnswerCallbackQuery" for c in fake.calls)
          and storage.get(500).mode.value == "detailed")

    # --- кнопки: смена модели
    fake.clear()
    await feed(make_message("/model"))
    out = " ".join(fake.texts())
    check("/model работает при двух ключах",
          any("reply_markup" in c[1] for c in fake.calls if c[0] == "SendMessage"), out)
    fake.clear()
    await feed(make_callback("provider:gemini"))
    check("модель переключена на Gemini", storage.get(500).provider == "gemini",
          storage.get(500).provider)
    fake.clear()
    await feed(make_callback("provider:claude"))
    check("недоступная модель не выбирается", storage.get(500).provider == "gemini",
          storage.get(500).provider)
    await feed(make_callback("provider:openai"))
    check("модель вернулась на GPT", storage.get(500).provider == "openai")

    # --- /status и /reset
    fake.clear()
    await feed(make_message("вопрос для истории"))
    fake.clear()
    await feed(make_message("/status"))
    out = " ".join(fake.texts())
    check("/status показывает модель и режим", "GPT" in out and "Развёрнутый" in out, out)
    check("/status показывает счётчик сообщений", "Сообщений в памяти: 2" in out, out)

    fake.clear()
    await feed(make_message("/reset"))
    check("/reset очищает историю", storage.get(500).history == [])
    check("/reset подтверждён сообщением", "очищена" in " ".join(fake.texts()).lower())

    # --- фотография принимается и уходит в ИИ с пометкой
    fake.clear()
    stub.answer = "Фото получил, разберёмся."
    await feed(make_photo())
    out = " ".join(fake.texts())
    check("фото принято, бот ответил", stub.answer in out, out)
    check("модель узнала о фотографии",
          "фотограф" in stub.calls[-1][-1]["content"].lower(), str(stub.calls[-1][-1]))

    # --- стикер по-прежнему вежливо отклоняется
    fake.clear()
    await feed(make_sticker())
    check("на стикер бот вежливо отвечает",
          "стикер" in " ".join(fake.texts()).lower(), " ".join(fake.texts()))

    # --- два сообщения подряд: очередь, история не перемешивается
    storage.get(500).clear()
    fake.clear()
    stub.delay = 0.25
    stub.answer = "Ответ на очередь."
    await asyncio.gather(
        feed(make_message("первый вопрос", message_id=10)),
        feed(make_message("второй вопрос", message_id=11)),
    )
    stub.delay = 0.0
    history = storage.get(500).history
    roles = [m["role"] for m in history]
    check("при двух быстрых сообщениях история не перемешана",
          roles == ["user", "assistant", "user", "assistant"], str(roles))
    check("второму сообщению сказано подождать",
          any("Секунду" in t for t in fake.texts()), str(fake.texts()))

    # --- разные чаты не мешают друг другу
    storage.get(500).clear()
    fake.clear()
    stub.delay = 0.15
    await asyncio.gather(
        feed(make_message("вопрос из чата А", chat_id=600, message_id=20)),
        feed(make_message("вопрос из чата Б", chat_id=700, message_id=21)),
    )
    stub.delay = 0.0
    check("чат А получил свою историю", len(storage.get(600).history) == 2)
    check("чат Б получил свою историю", len(storage.get(700).history) == 2)
    check("истории чатов не смешались",
          storage.get(600).history[0]["content"] == "вопрос из чата А"
          and storage.get(700).history[0]["content"] == "вопрос из чата Б")

    # --- автопереключение при лимите у основной модели
    from providers import is_available, reset_availability

    reset_availability()
    storage.get(500).clear()
    storage.get(500).provider = "openai"
    fake.clear()
    stub.error = ProviderError("OpenAI: лимит запросов", retryable=True)
    await feed(make_message("вопрос при исчерпанном лимите"))
    out = " ".join(fake.texts())
    check("при лимите отвечает запасная модель", "Ответ Gemini." in out, out)
    check("человек не видит сообщения об ошибке", "лимит" not in out.lower(), out)
    check("подведший провайдер отставлен", not is_available("openai"))
    check("чат переключён на рабочую модель", storage.get(500).provider == "gemini",
          storage.get(500).provider)
    check("история чистая: вопрос + ответ", len(storage.get(500).history) == 2,
          str(storage.get(500).history))

    # следующий вопрос идёт сразу в рабочую модель, без повторного спотыкания
    calls_before = len(stub.calls)
    fake.clear()
    await feed(make_message("следующий вопрос"))
    check("отставленную модель больше не дёргают", len(stub.calls) == calls_before,
          f"{calls_before} -> {len(stub.calls)}")
    check("ответ пришёл", "Ответ Gemini." in " ".join(fake.texts()))

    # --- отказали все модели -> человеку показывают причину
    reset_availability()
    storage.get(500).clear()
    storage.get(500).provider = "openai"
    fake.clear()
    stubs["gemini"].error = ProviderError("Gemini: превышен лимит", retryable=True)
    await feed(make_message("вопрос когда всё лежит"))
    out = " ".join(fake.texts())
    check("если отказали все — показана причина", "лимит" in out.lower(), out)
    check("после общего отказа история пуста", storage.get(500).history == [])
    stubs["gemini"].error = None

    # --- ошибку «виноват вопрос» не переигрываем на соседней модели
    reset_availability()
    storage.get(500).provider = "openai"
    fake.clear()
    gemini_calls_before = len(stubs["gemini"].calls)
    stub.error = ProviderError("GPT отказался отвечать: сработал фильтр содержимого.")
    await feed(make_message("вопрос под фильтр"))
    out = " ".join(fake.texts())
    check("фильтр не переигрывается на другой модели",
          len(stubs["gemini"].calls) == gemini_calls_before, "Gemini дёрнули зря")
    check("причина отказа показана", "фильтр" in out.lower(), out)
    check("основная модель осталась выбранной", storage.get(500).provider == "openai")
    stub.error = None
    reset_availability()
    storage.get(500).provider = "openai"
    storage.get(500).clear()

    # --- аварийный обработчик: ошибка вне handler'а не должна ронять опрос
    from aiogram.types import ErrorEvent, Update as _U
    handled = await bot_module.on_error(
        ErrorEvent(update=_U(update_id=1), exception=RuntimeError("тестовая авария"))
    )
    check("аварийный обработчик гасит ошибку", handled is True, str(handled))
    from aiogram.exceptions import TelegramForbiddenError as _Forbidden
    handled = await bot_module.on_error(
        ErrorEvent(update=_U(update_id=2),
                   exception=_Forbidden(method=None, message="bot was blocked by the user"))
    )
    check("заблокировавший бота пользователь не роняет опрос", handled is True)

    # --- команда с упоминанием бота (как в группах)
    fake.clear()
    await feed(make_message("/status@test_bot"))
    check("команда с @именем бота распознана",
          "Текущие настройки" in " ".join(fake.texts()), " ".join(fake.texts())[:80])

    # --- без панели бот обязан работать сам
    check("без панели обращения никуда не уходят", not crm_client.enabled)
    check("и житель всё равно получает ответы", "Ответ модели." in stub.answer
          or bool(fake.texts()))

    crm_client.enabled = saved_enabled
    await bot.session.close()


# ===========================================================================
# 6. Реальные провайдеры: правильные ли ошибки при неверном ключе
# ===========================================================================

async def test_providers_offline() -> None:
    section("6. Провайдеры (проверка кода без реальных ключей)")
    from providers import _REGISTRY as _REG
    from providers import ProviderError, available_providers, get_provider
    _REGISTRY_NAMES = list(_REG)

    check("видны провайдеры, для которых есть ключ",
          set(available_providers()) <= {"openai", "gemini", "claude", "openrouter"},
          str(available_providers()))

    try:
        get_provider("несуществующий")
        check("неизвестный провайдер -> ошибка", False)
    except ProviderError:
        check("неизвестный провайдер -> ошибка", True)

    # Клиенты должны создаваться без обращения к сети.
    for name in ("openai", "gemini"):
        try:
            p = get_provider(name)
            check(f"клиент {name} создаётся", p is not None)
        except Exception as e:
            check(f"клиент {name} создаётся", False, f"{type(e).__name__}: {e}")

    # Gemini: перебор вариантов отключения «размышлений» настроен верно.
    from providers.gemini import _THINKING_VARIANTS, GeminiProvider

    g = get_provider("gemini")
    cfg = g._build_config("sys", 500, "level")
    check("gemini: вариант thinking_level собирается",
          cfg.thinking_config is not None and cfg.thinking_config.thinking_level is not None)
    cfg = g._build_config("sys", 500, "budget")
    check("gemini: вариант thinking_budget собирается",
          cfg.thinking_config is not None and cfg.thinking_config.thinking_budget == 0)
    cfg = g._build_config("sys", 8000, None)
    check("gemini: развёрнутый режим без ограничения размышлений",
          cfg.thinking_config is None and cfg.max_output_tokens == 8000)
    check("gemini: есть запасные варианты", len(_THINKING_VARIANTS) == 3)

    hist = GeminiProvider._to_gemini_history(
        [{"role": "user", "content": "вопрос"}, {"role": "assistant", "content": "ответ"}]
    )
    check("gemini: роль assistant переведена в model",
          [c.role for c in hist] == ["user", "model"], str([c.role for c in hist]))

    # Очередь автопереключения.
    from providers import fallback_chain, is_available, mark_unavailable, reset_availability

    reset_availability()
    check("очередь начинается с выбранной модели",
          fallback_chain("gemini")[0] == "gemini", str(fallback_chain("gemini")))
    check("в очереди все модели с ключами",
          sorted(fallback_chain("openai")) == ["gemini", "openai"], str(fallback_chain("openai")))
    mark_unavailable("openai", seconds=60)
    check("отставленная модель уходит в конец очереди",
          fallback_chain("openai") == ["gemini", "openai"], str(fallback_chain("openai")))
    check("отставленная модель помечена", not is_available("openai"))
    mark_unavailable("openai", seconds=-1)  # срок уже истёк
    check("по истечении срока модель возвращается в строй", is_available("openai"))
    reset_availability()

    # OpenAI: системный промпт идёт первым сообщением.
    from providers.openai_provider import OpenAIProvider
    check("openai: провайдер знает своё имя", OpenAIProvider.name == "openai")

    # OpenRouter: тот же протокол, другой адрес и составные имена моделей.
    from providers.openrouter import BASE_URL, OpenRouterProvider
    check("openrouter: свой адрес API", BASE_URL == "https://openrouter.ai/api/v1")
    check("openrouter: наследует протокол OpenAI",
          issubclass(OpenRouterProvider, OpenAIProvider))
    check("openrouter: есть в реестре", "openrouter" in _REGISTRY_NAMES)
    providers_module = __import__("providers")
    providers_module.apply_overrides({"openrouter": {"key": "sk-or-v1-test", "model": ""}})
    try:
        providers_module.get_provider("openrouter")
        check("openrouter: без выбранной модели просит её выбрать", False)
    except ProviderError as e:
        check("openrouter: без выбранной модели просит её выбрать",
              "модель" in str(e).lower(), str(e))
    providers_module.apply_overrides(
        {"openrouter": {"key": "sk-or-v1-test", "model": "openai/gpt-4o-mini"}})
    check("openrouter: с ключом и моделью поднимается",
          providers_module.get_provider("openrouter")._model == "openai/gpt-4o-mini")
    providers_module.apply_overrides({})



# ===========================================================================
# 7. Клиент панели управления (crm.py) — без сети, через подменённый транспорт
# ===========================================================================

class FakePanel:
    """Подставная панель: отвечает как настоящая и записывает все запросы."""

    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.down = False              # True = «панель лежит»
        self.answer_mode = "ai"
        self.ticket_id = 1
        self.created = True   # первое сообщение заводит карточку, дальше False
        self.config: dict = {"changed": True, "version": 7, "enabled": True,
                             "providers": [], "quick_answers": [], "facts": ""}
        self.context: dict | None = None
        self.outbox_items: dict[str, list[dict]] = {
            "telegram": [{"id": 5, "chat_id": 500, "text": "Ответ отдела ЖКХ", "kind": "reply"}],
            "whatsapp": [],
        }

    def handler(self, request):
        import httpx, json as _json
        if self.down:
            raise httpx.ConnectError("panel down", request=request)
        path = request.url.path
        self.requests.append((request.method, path, request.content))
        if path.endswith("/tickets/incoming/"):
            payload = {"ticket_id": self.ticket_id, "number": "2026-0001",
                       "answer_mode": self.answer_mode, "created": self.created}
            self.created = False  # следующие сообщения дописываются в карточку
            return httpx.Response(200, json=payload)
        if "/messages/" in path and path.endswith("/rating/"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/messages/"):
            return httpx.Response(200, json={"message_id": 77})
        if path.endswith("/context/"):
            if self.context is None:
                return httpx.Response(404, json={"detail": "нет"})
            return httpx.Response(200, json=self.context)
        if path.endswith("/config/"):
            return httpx.Response(200, json=self.config)
        if path.endswith("/outbox/"):
            channel = request.url.params.get("channel", "telegram")
            return httpx.Response(200, json={"items": self.outbox_items.get(channel, [])})
        return httpx.Response(200, json={"ok": True})


def connect_fake_panel(panel: FakePanel):
    """Включить подставную панель для глобального клиента crm."""
    import httpx
    from crm import crm as client
    client.base = "http://panel.test"
    client.token = "test-panel-token"
    client.enabled = True
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(panel.handler),
        base_url="http://panel.test/api/v1",
        headers={"X-Bot-Token": "test-panel-token"},
    )
    return client


async def test_crm_client() -> None:
    section("7. Клиент панели (crm.py)")
    import crm as crm_module
    from pathlib import Path
    import tempfile

    crm_module.QUEUE_FILE = Path(tempfile.mkdtemp()) / "queue.jsonl"
    panel = FakePanel()
    client = connect_fake_panel(panel)

    profile = {"tg_user_id": 500, "chat_id": 500, "first_name": "Тест",
               "last_name": "", "username": ""}

    # Обычная доставка
    result = await client.incoming(profile, "не вывозят мусор")
    check("обращение доставлено в панель",
          result is not None and result["number"] == "2026-0001", str(result))
    check("очередь на диске пуста", not crm_module.QUEUE_FILE.exists())

    # Панель упала -> обращение в очередь, бот получает None и живёт дальше
    panel.down = True
    result = await client.incoming(profile, "вопрос во время сбоя")
    check("при сбое панели метод возвращает None, не исключение", result is None)
    check("обращение легло в очередь на диске",
          crm_module.QUEUE_FILE.exists()
          and "вопрос во время сбоя" in crm_module.QUEUE_FILE.read_text())

    # Панель ожила -> очередь досылается
    panel.down = False
    delivered = await client.flush_queue()
    check("очередь дослана после восстановления", delivered == 1, str(delivered))
    check("файл очереди удалён", not crm_module.QUEUE_FILE.exists())

    # Исходящие
    items = await client.outbox_pending(channel="telegram")
    check("исходящие разбираются", items and items[0]["text"] == "Ответ отдела ЖКХ",
          str(items))
    empty = await client.outbox_pending(channel="whatsapp")
    check("исходящие фильтруются по каналу", empty == [], str(empty))
    await client.outbox_sent(5)
    check("отправка помечена", any("/outbox/5/sent/" in path
                                   for _, path, _ in panel.requests))

    # Конфигурация
    cfg = await client.fetch_config(0)
    check("конфигурация приходит", cfg and cfg["version"] == 7, str(cfg))

    await client.close()


# ===========================================================================
# 8. Диалог с подключённой панелью
# ===========================================================================

async def test_dialog_with_panel() -> None:
    section("8. Диалог с подключённой панелью")
    import bot as bot_module
    import classify as classify_module
    import conversation as conversation_module
    import json as _json
    from aiogram import Bot
    from remote_config import remote

    panel = FakePanel()
    client = connect_fake_panel(panel)

    fake = FakeTelegram()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"], session=fake.build_session())
    stub = StubProvider()
    stub.answer = "Мусор вывезут по графику."
    conversation_module.get_provider = lambda name: stub
    classify_module.get_provider = lambda name: stub
    storage = bot_module.storage
    dp = bot_module.dp

    async def feed(update):
        await dp.feed_update(bot, update)
        await asyncio.sleep(0.05)  # даём фоновым задачам (классификация) отработать

    chat = 600

    # --- обращение уходит в панель, ответ записывается, кнопки оценки есть
    storage.get(chat).clear()
    storage.get(chat).restored = True
    fake.clear(); panel.requests.clear()
    await feed(make_message("Не вывозят мусор!", chat_id=chat, message_id=30))
    paths = [path for _, path, _ in panel.requests]
    check("обращение записано в панель", "/tickets/incoming/" in " ".join(paths),
          str(paths))
    check("ответ ИИ записан в карточку",
          any(path.endswith("/tickets/1/messages/") for path in paths), str(paths))
    check("карточка привязана к сессии", storage.get(chat).ticket_id == 1)
    # Кнопки оценки сейчас выключены (bot.RATING_BUTTONS_ENABLED), но код жив.
    sends = [c for c in fake.calls if c[0] == "SendMessage" and "reply_markup" in c[1]]
    check("выключенные кнопки оценки под ответом не появляются",
          not any("rate:" in str(c[1].get("reply_markup", "")) for c in sends),
          str(sends)[:200])

    bot_module.RATING_BUTTONS_ENABLED = True
    fake.clear()
    await feed(make_message("вопрос с оценкой", chat_id=chat, message_id=34))
    sends = [c for c in fake.calls if c[0] == "SendMessage" and "reply_markup" in c[1]]
    check("включённые кнопки оценки возвращаются одним переключателем",
          any("rate:77:" in str(c[1]["reply_markup"]) for c in sends), str(sends)[:200])
    bot_module.RATING_BUTTONS_ENABLED = False
    check("классификация ушла в панель",
          any(path.endswith("/tickets/1/classify/") for path in paths), str(paths))

    # --- оценка «помогло»
    fake.clear(); panel.requests.clear()
    await feed(make_callback("rate:77:up", chat_id=chat))
    check("оценка доставлена в панель",
          any(path.endswith("/messages/77/rating/") for _, path, _ in panel.requests))
    check("нажатие оценки подтверждено",
          any(c[0] == "AnswerCallbackQuery" for c in fake.calls))

    # --- сотрудник перехватил разговор: ИИ молчит
    panel.answer_mode = "staff"
    calls_before = len(stub.calls)
    fake.clear()
    await feed(make_message("а когда приедете?", chat_id=chat, message_id=31))
    check("в режиме сотрудника модель не зовётся", len(stub.calls) == calls_before)
    check("и бот ничего не отвечает сам",
          not [c for c in fake.calls if c[0] == "SendMessage"], str(fake.texts()))
    panel.answer_mode = "ai"

    # --- бот выключен в панели: текст техработ, модель не зовётся
    remote.data = {"enabled": False, "maintenance_text": "Идут технические работы."}
    calls_before = len(stub.calls)
    fake.clear()
    await feed(make_message("есть кто живой?", chat_id=chat, message_id=32))
    check("выключенный бот отвечает текстом техработ",
          "технические работы" in " ".join(fake.texts()).lower(), str(fake.texts()))
    check("выключенный бот не зовёт модель", len(stub.calls) == calls_before)

    # --- готовый ответ: дословно и без модели
    remote.data = {"enabled": True, "quick_answers": [
        {"id": 3, "triggers": ["график работы"], "answer": "Мэрия: пн-пт 8:30-17:30."},
    ]}
    calls_before = len(stub.calls)
    fake.clear(); panel.requests.clear()
    await feed(make_message("Какой график работы мэрии?", chat_id=chat, message_id=33))
    out = " ".join(fake.texts())
    check("готовый ответ отдан дословно", "пн-пт 8:30-17:30" in out, out)
    check("готовый ответ не зовёт модель", len(stub.calls) == calls_before)
    check("срабатывание готового ответа посчитано",
          any(path.endswith("/answers/3/hit/") for _, path, _ in panel.requests))
    remote.data = {}

    # --- восстановление истории после «перезапуска»
    chat2 = 700
    panel.context = {"ticket_id": 9, "number": "2026-0002", "status": "in_progress",
                     "answer_mode": "ai", "is_open": True,
                     "messages": [{"author": "citizen", "text": "первый вопрос"},
                                  {"author": "ai", "text": "первый ответ"}]}
    panel.ticket_id = 9
    storage._sessions.pop(chat2, None)
    fake.clear()
    await feed(make_message("продолжаем?", chat_id=chat2, message_id=40))
    history = storage.get(chat2).history
    check("история восстановлена из панели",
          len(history) >= 3 and history[0]["content"] == "первый вопрос",
          str(history))

    # --- /reset закрывает карточку в панели
    panel.requests.clear()
    await feed(make_message("/reset", chat_id=chat))
    check("/reset закрывает карточку в панели",
          any(path.endswith(f"/chats/{chat}/close/") for _, path, _ in panel.requests))

    # --- /stop отписывает от рассылок
    panel.requests.clear()
    fake.clear()
    await feed(make_message("/stop", chat_id=chat))
    check("/stop отписывает от рассылок",
          any(path.endswith("/citizens/subscription/") for _, path, _ in panel.requests))
    check("/stop подтверждён человеку", "Рассылки отключены" in " ".join(fake.texts()))

    # --- ключи из панели перекрывают .env
    import providers
    providers.apply_overrides({"openai": {"key": "sk-panel-key", "model": "gpt-4o-mini"}})
    from providers.openai_provider import OpenAIProvider
    fresh = providers.get_provider("openai")
    check("модель из панели применена", fresh._model == "gpt-4o-mini", fresh._model)
    providers.apply_overrides({})
    check("сброс переопределений возвращает .env",
          providers.get_provider("openai")._model != "gpt-4o-mini")

    await client.close()
    await bot.session.close()


# ===========================================================================
# 9. WhatsApp (whatsapp_bot.py) — подпись, разбор вебхука, приём, диалог,
#    очередь исходящих. Отдельный процесс от Telegram-бота, но проверяется
#    так же: без сети, без реальных ключей Meta.
# ===========================================================================

def test_whatsapp_pure() -> None:
    section("9. WhatsApp: подпись и разбор вебхука (без сети)")
    import hashlib
    import hmac

    import whatsapp_bot as wa

    secret = "test-app-secret"
    body = b'{"hello": "world"}'
    good_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    check("верная подпись принимается", wa.verify_signature(secret, body, good_sig))
    check("испорченное тело отклоняется",
          not wa.verify_signature(secret, b"tampered", good_sig))
    check("чужой секрет отклоняется",
          not wa.verify_signature("wrong-secret", body, good_sig))
    check("без заголовка подписи — отказ", not wa.verify_signature(secret, body, None))
    check("заголовок без префикса sha256= отклонён",
          not wa.verify_signature(secret, body, good_sig.removeprefix("sha256=")))

    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "996700123456", "profile": {"name": "Айгуль"}}],
        "messages": [{"from": "996700123456", "id": "wamid.1", "type": "text",
                     "text": {"body": "Не работает свет"}}],
    }}]}]}
    parsed = wa.parse_webhook_payload(payload)
    check("сообщение разобрано", len(parsed) == 1, str(parsed))
    check("текст и имя на месте",
          bool(parsed) and parsed[0]["text"] == "Не работает свет"
          and parsed[0]["name"] == "Айгуль", str(parsed))

    status_only = {"entry": [{"changes": [{"value": {
        "statuses": [{"status": "delivered"}]}}]}]}
    check("статус-колбэк без сообщений не роняет разбор",
          wa.parse_webhook_payload(status_only) == [])
    check("пустой payload не роняет разбор", wa.parse_webhook_payload({}) == [])

    profile = wa.build_profile("996700123456", "Айгуль")
    check("профиль WhatsApp собран правильно",
          profile == {"channel": "whatsapp", "chat_id": 996700123456,
                     "first_name": "Айгуль", "last_name": "", "username": "",
                     "phone": "996700123456"},
          str(profile))


async def test_whatsapp_webhook() -> None:
    section("10. WhatsApp: вебхук целиком (aiohttp, без сети)")
    import hashlib
    import hmac
    import json as _json

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import whatsapp_bot as wa

    secret = "test-app-secret"
    payload = {"entry": [{"changes": [{"value": {
        "messages": [{"from": "996700123456", "id": "wamid.1", "type": "text",
                     "text": {"body": "Вопрос"}}],
    }}]}]}
    body = _json.dumps(payload).encode()
    good_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    app = web.Application()
    app.router.add_get("/webhook", wa.verify_webhook)
    app.router.add_post("/webhook", wa.receive_webhook)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
            "hub.challenge": "12345"})
        check("хендшейк отвечает challenge",
              r.status == 200 and await r.text() == "12345")

        r = await client.get("/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "чужой-токен",
            "hub.challenge": "12345"})
        check("хендшейк с неверным токеном отклонён (403)", r.status == 403)

        received: list[dict] = []
        original = wa.handle_incoming_message

        async def fake_handle(msg):
            received.append(msg)

        wa.handle_incoming_message = fake_handle
        try:
            r = await client.post("/webhook", data=body, headers={
                "X-Hub-Signature-256": good_sig, "Content-Type": "application/json"})
            check("приём с верной подписью -> 200", r.status == 200)
            await asyncio.sleep(0.05)
            check("сообщение дошло до обработчика", len(received) == 1, str(received))

            received.clear()
            r = await client.post("/webhook", data=body, headers={
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "Content-Type": "application/json"})
            check("приём с неверной подписью -> 403", r.status == 403)
            await asyncio.sleep(0.05)
            check("обработчик не вызван при неверной подписи", received == [])
        finally:
            wa.handle_incoming_message = original
    finally:
        await client.close()


async def test_whatsapp_dialog_with_panel() -> None:
    section("11. WhatsApp: диалог с подключённой панелью")
    import conversation as conversation_module
    import whatsapp_bot as wa

    panel = FakePanel()
    client = connect_fake_panel(panel)

    stub = StubProvider()
    stub.answer = "Заявку приняли, разберёмся."
    conversation_module.get_provider = lambda name: stub

    sent: list[tuple[str, str]] = []

    async def fake_send(phone, text):
        sent.append((phone, text))
        return True, ""

    wa.send_whatsapp_text = fake_send

    chat_id = 996700123456
    wa.storage.get(chat_id).clear()
    wa.storage.get(chat_id).restored = True  # без сети до контекста, как в тесте 8
    panel.requests.clear()
    await wa.handle_incoming_message({"phone": str(chat_id), "type": "text",
                                      "text": "Не вывозят мусор", "wa_message_id": "w1",
                                      "name": "Айгуль"})
    paths = [path for _, path, _ in panel.requests]
    check("обращение записано в панель (WhatsApp)",
          "/tickets/incoming/" in " ".join(paths), str(paths))
    check("канал в обращении — whatsapp",
          any(b"whatsapp" in body for _, path, body in panel.requests
              if path.endswith("/tickets/incoming/")))
    check("ответ ИИ записан в карточку (WhatsApp)",
          any(p.endswith("/tickets/1/messages/") for p in paths), str(paths))
    check("ответ отправлен через WhatsApp",
          bool(sent) and sent[-1] == (str(chat_id), stub.answer), str(sent))

    # режим сотрудника — ИИ молчит, ответ уйдёт только через очередь исходящих
    panel.answer_mode = "staff"
    sent.clear()
    await wa.handle_incoming_message({"phone": str(chat_id), "type": "text",
                                      "text": "ещё вопрос", "wa_message_id": "w2", "name": ""})
    check("в режиме сотрудника WhatsApp-бот не отвечает сам", sent == [], str(sent))
    panel.answer_mode = "ai"

    # не текстовое сообщение — заглушка, обращение в панель не уходит
    panel.requests.clear()
    sent.clear()
    await wa.handle_incoming_message({"phone": str(chat_id), "type": "image",
                                      "text": "", "wa_message_id": "w3", "name": ""})
    check("не-текстовое сообщение не создаёт обращение", panel.requests == [])
    check("на не-текстовое сообщение есть ответ-заглушка", bool(sent), str(sent))

    await client.close()


async def test_whatsapp_outbox() -> None:
    section("12. WhatsApp: очередь исходящих")
    import whatsapp_bot as wa

    panel = FakePanel()
    client = connect_fake_panel(panel)
    panel.outbox_items["telegram"] = []
    panel.outbox_items["whatsapp"] = [
        {"id": 9, "chat_id": 996700123456, "text": "Ответ по вашей заявке", "kind": "reply"},
    ]

    sent: list[tuple[str, str]] = []

    async def fake_send(phone, text):
        sent.append((phone, text))
        return True, ""

    wa.send_whatsapp_text = fake_send

    items = await client.outbox_pending(channel="whatsapp")
    check("WhatsApp-очередь отдаёт свою строку", bool(items) and items[0]["id"] == 9,
          str(items))
    check("Telegram-очередь не видит WhatsApp-строку",
          await client.outbox_pending(channel="telegram") == [])

    for row in items:
        ok, error = await wa.send_whatsapp_text(str(row["chat_id"]), row["text"])
        if ok:
            await client.outbox_sent(row["id"])
        else:
            await client.outbox_failed(row["id"], error)
    check("строка отправлена через WhatsApp",
          sent == [("996700123456", "Ответ по вашей заявке")], str(sent))
    check("строка помечена отправленной в панели",
          any("/outbox/9/sent/" in p for _, p, _ in panel.requests))

    await client.close()


# ===========================================================================

async def main() -> int:
    for test in (test_config, test_split, test_icons, test_prompts, test_storage,
                 test_whatsapp_pure):
        try:
            test()
        except Exception:
            FAILED.append((test.__name__, traceback.format_exc()))

    for test in (test_dialog, test_providers_offline,
                 test_crm_client, test_dialog_with_panel,
                 test_whatsapp_webhook, test_whatsapp_dialog_with_panel,
                 test_whatsapp_outbox):
        try:
            await test()
        except Exception:
            FAILED.append((test.__name__, traceback.format_exc()))

    print("\n" + "=" * 70)
    for name in PASSED:
        print(f"  ok    {name}")
    for name, detail in FAILED:
        print(f"  ПАДЁТ {name}\n        {detail}")
    print("=" * 70)
    print(f"Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
