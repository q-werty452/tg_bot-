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


def citizen_part(content: str) -> str:
    """Само сообщение жителя без служебных блоков (справочник, «О жителе»),
    которые бот приклеивает к нему для модели (prompts.compose_user_turn)."""
    return content.split("[Сообщение жителя]\n", 1)[-1]


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
        check(f"{name}: указана Джалал-Абадская область", "Джалал-Абад" in text)
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

    # Бот отвечает жителям всерьёз: режим показа (додумывать телефоны и
    # адреса) по умолчанию выключен.
    check("по умолчанию рабочий режим — без выдумок", prompts.DEMO_MODE is False)
    strict = prompts.build_system(Mode.CHAT, invent=False)
    check("рабочий режим: поиск в интернете по служебной строке",
          prompts.SEARCH_MARKER in strict)
    check("рабочий режим: ФИО и место — только при обращении",
          "Справочный вопрос" in strict and "отчество" in strict)
    check("рабочий режим: Конституция — только из данного текста",
          "Конституция КР" in strict)
    check("рабочий режим: просьба переспросить непонятное", "переспроси" in strict)


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


def test_vision_pure() -> None:
    section("4b. Фото для модели (utils.read_image + сборка сообщений)")
    import base64
    import shutil
    import tempfile
    from pathlib import Path

    from utils import VISION_IMAGE_LIMIT, read_image

    # Валидный 1x1 PNG — реального декодирования картинки нам не нужно,
    # важно только что read_image() честно вернёт байты и mime.
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
        "42YAAAAASUVORK5CYII="
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="selftest_vision_"))
    try:
        good_path = tmpdir / "photo.png"
        good_path.write_bytes(png_bytes)

        encoded = read_image(str(good_path))
        check("read_image читает валидную картинку", encoded is not None)
        check("read_image возвращает верный mime",
              encoded is not None and encoded[0] == "image/png", str(encoded and encoded[0]))
        check("read_image возвращает те же байты",
              encoded is not None and encoded[1] == png_bytes)

        check("read_image: нет файла -> None",
              read_image(str(tmpdir / "нет-такого.png")) is None)

        doc_path = tmpdir / "doc.pdf"
        doc_path.write_bytes(b"%PDF-1.4 fake")
        check("read_image: не картинка по расширению -> None", read_image(str(doc_path)) is None)

        big_path = tmpdir / "big.jpg"
        big_path.write_bytes(b"\xff" * (VISION_IMAGE_LIMIT + 1))
        check("read_image: слишком большой файл -> None", read_image(str(big_path)) is None)

        # storage.Session.add сохраняет пути к фото в истории
        from storage import Session
        s = Session(provider="openai")
        s.add("user", "текст", limit=10, images=[str(good_path)])
        check("Session.add сохраняет images", s.history[-1].get("images") == [str(good_path)])
        s.add("user", "без фото", limit=10)
        check("Session.add без фото не добавляет ключ", "images" not in s.history[-1])

        # Сборка сообщения для OpenAI (и OpenRouter — тот же код, он наследует ask())
        from providers.openai_provider import _to_message as openai_to_message
        plain = openai_to_message({"role": "user", "content": "просто текст"})
        check("openai: без фото content остаётся строкой", plain["content"] == "просто текст")

        with_photo = openai_to_message(
            {"role": "user", "content": "подпись", "images": [str(good_path)]})
        check("openai: с фото content становится списком блоков",
              isinstance(with_photo["content"], list))
        kinds = [b["type"] for b in with_photo["content"]]
        check("openai: есть текстовый и image_url блоки", kinds == ["text", "image_url"], str(kinds))
        check("openai: картинка закодирована как data: URL с верным mime",
              with_photo["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

        missing_photo = openai_to_message(
            {"role": "user", "content": "подпись", "images": [str(tmpdir / "нет.png")]})
        check("openai: несуществующее фото не роняет сборку, остаётся текст",
              missing_photo["content"] == [{"type": "text", "text": "подпись"}],
              str(missing_photo["content"]))

        # Сборка истории для Gemini
        from providers.gemini import GeminiProvider
        hist = GeminiProvider._to_gemini_history(
            [{"role": "user", "content": "подпись", "images": [str(good_path)]}])
        check("gemini: у сообщения с фото два part (текст + картинка)",
              len(hist[0].parts) == 2, str(len(hist[0].parts)))
        check("gemini: картинка передана как inline_data с верным mime",
              hist[0].parts[1].inline_data.mime_type == "image/png")

        # Сборка сообщения для Claude
        from providers.claude import _to_message as claude_to_message
        claude_with_photo = claude_to_message(
            {"role": "user", "content": "подпись", "images": [str(good_path)]})
        check("claude: content становится списком блоков с фото",
              isinstance(claude_with_photo["content"], list)
              and claude_with_photo["content"][1]["type"] == "image")
        check("claude: media_type верный",
              claude_with_photo["content"][1]["source"]["media_type"] == "image/png")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    """
    Подставной ИИ: отвечает заранее заданным текстом или падает с ошибкой.

    Разбор переписки для карточки (classify.py) — тоже вызов модели, но
    другого рода: его ответы и вызовы живут отдельно (extract_answer,
    extract_calls), чтобы проверки «модель вызвана N раз» по-прежнему
    считали только ответы жителю.
    """

    def __init__(self):
        self.answer = "Ответ модели."
        self.error: Exception | None = None
        self.delay = 0.0
        self.calls: list[list[dict]] = []
        self.systems: list[str] = []
        self.extract_answer = ('{"kind": "appeal", "title": "Тестовое обращение", '
                               '"summary": "Житель сообщил о проблеме", "category": "other"}')
        self.extract_calls: list[str] = []

    async def ask(self, system, history, max_tokens, detailed):
        import classify as _classify
        if system.startswith(_classify.EXTRACT_PROMPT[:40]):
            self.extract_calls.append(history[-1]["content"])
            return self.extract_answer
        self.calls.append([dict(m) for m in history])
        self.systems.append(system)
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
        self.history_messages: list[dict] = []
        self.retitled = False
        self.retitle_calls: list[str] = []
        self.message_id = 500        # id сообщений жителя в «панели»
        self.split_calls: list[tuple[str, dict]] = []
        self.split_to = 2            # номер заявки, которую «создаст» разделение

    def handler(self, request):
        import httpx, json as _json
        if self.down:
            raise httpx.ConnectError("panel down", request=request)
        path = request.url.path
        self.requests.append((request.method, path, request.content))
        if path.endswith("/tickets/incoming/"):
            self.message_id += 1
            payload = {"ticket_id": self.ticket_id, "number": "2026-0001",
                       "answer_mode": self.answer_mode, "created": self.created,
                       "message_id": self.message_id}
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
        if path.endswith("/history/"):
            return httpx.Response(200, json={"ticket_id": self.ticket_id,
                                             "messages": self.history_messages})
        if path.endswith("/split/"):
            body = _json.loads(request.content or b"{}")
            self.split_calls.append((path, body))
            self.ticket_id = self.split_to
            return httpx.Response(200, json={"ticket_id": self.split_to,
                                             "number": "2026-0002"})
        if path.endswith("/retitle/"):
            body = _json.loads(request.content or b"{}")
            applied = not self.retitled
            self.retitled = self.retitled or applied
            self.retitle_calls.append(body.get("title"))
            return httpx.Response(200, json={"applied": applied})
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

    import classify as classify_module
    stub = StubProvider()
    stub.answer = "Заявку приняли, разберёмся."
    conversation_module.get_provider = lambda name: stub
    classify_module.get_provider = lambda name: stub

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


class SequencedStub:
    """Подставной ИИ, отвечающий по очереди из списка — для проверки двух
    последовательных запросов внутри одной фоновой задачи (заголовок,
    затем категория/район/адрес)."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.systems: list[str] = []

    async def ask(self, system, history, max_tokens, detailed):
        self.systems.append(system)
        return self.answers.pop(0) if self.answers else "НЕЯСНО"


async def settle() -> None:
    """Дождаться фоновых задач бота (разбор переписки, геометка в панель)."""
    import conversation as conversation_module
    for _ in range(20):
        pending = [t for t in conversation_module._BACKGROUND if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def classify_bodies(panel, ticket_id: int | None = None) -> list[dict]:
    """Тела запросов /classify/ в подставную панель (по заявке, если указана)."""
    import json as _json
    suffix = f"/tickets/{ticket_id}/classify/" if ticket_id else "/classify/"
    return [_json.loads(body) for _, path, body in panel.requests if path.endswith(suffix)]


async def test_update_ticket() -> None:
    section("13. Карточка по переписке (classify.update_ticket)")
    import asyncio as _asyncio

    import classify as classify_module
    from providers import ProviderError
    from remote_config import remote
    from storage import Session

    # Фоновые разборы из прошлых разделов (там тоже заявка №1) не должны
    # подхватить подменённую здесь модель — начинаем с чистого листа.
    await settle()
    classify_module._RUNNING.clear()
    classify_module._DIRTY.clear()

    panel = FakePanel()
    client = connect_fake_panel(panel)
    saved_data = remote.data
    remote.data = {**(saved_data or {}),
                   "categories": [{"slug": "water", "name": "Вода"},
                                  {"slug": "roads", "name": "Дороги"},
                                  {"slug": "other", "name": "Прочее"}],
                   "districts": [{"slug": "nooken", "name": "Ноокенский район"}]}

    # --- разбор ответа модели: ничему не верим
    r = classify_module.parse_extract(
        'вот: {"kind": "APPEAL", "category": "water", "district": "Ноокен", '
        '"executor": "org-x", "phone": "0555 12-34-56", "new_topic": "yes"} конец',
        {"water"}, {"nooken"}, {"org-1"})
    check("разбор: тип приведён к нижнему регистру", r["kind"] == "appeal", str(r))
    check("разбор: район не по коду — выброшен", r["district"] == "", str(r))
    check("разбор: исполнитель не из кандидатов — выброшен", r["executor"] == "", str(r))
    check("разбор: телефон 0555... -> 996555...", r["phone"] == "996555123456", str(r))
    check("разбор: new_topic засчитан только при true", r["new_topic"] is False, str(r))
    check("разбор: не-JSON -> None",
          classify_module.parse_extract("не знаю", set(), set(), set()) is None)

    def new_session(*lines) -> Session:
        session = Session(provider="openai")
        session.ticket_id = 1
        crm_id = 500
        for role, text in lines:
            crm_id += 1
            session.add(role, text, 20, crm_id=crm_id if role == "user" else None)
            if role == "user":
                session.last_message_id = crm_id
        return session

    # --- приветствие: тип есть, темы и категории ещё нет
    stub = SequencedStub(['{"kind": "other", "title": "Приветствие", "category": "other"}'])
    classify_module.get_provider = lambda name: stub
    session = new_session(("user", "Здравствуйте"), ("assistant", "Здравствуйте! Чем помочь?"))
    panel.requests.clear()
    await classify_module.update_ticket(client, session, "openai")
    body = (classify_bodies(panel, 1) or [{}])[0]
    check("приветствие: в панель ушёл тип обращения", body.get("kind") == "other", str(body))
    check("приветствие: тема и категория не ставятся (заявка не застрянет на «Прочее»)",
          "title" not in body and "category" not in body, str(body))

    # --- полноценное обращение: все поля, исполнитель из кандидатов
    full = ('{"kind": "appeal", "title": "Нет воды в Масы", "summary": "Третий день нет воды",'
            ' "category": "water", "district": "nooken", "settlement": "Масы",'
            ' "address": "ул. Ленина 5", "last_name": "Асанов", "first_name": "Бакыт",'
            ' "middle_name": "Асанович", "phone": "0555123456", "executor": "org-1"}')
    stub = SequencedStub([full, full])
    classify_module.get_provider = lambda name: stub
    session = new_session(("user", "В Масы нет воды третий день, я Асанов Бакыт Асанович, 0555123456"),
                          ("assistant", "Передам в водоканал."))
    session.candidates = [{"id": "org-1", "name": "Водоканал Ноокен"}]
    panel.requests.clear()
    await classify_module.update_ticket(client, session, "openai")
    body = (classify_bodies(panel, 1) or [{}])[0]
    check("обращение: в панель ушли ФИО с отчеством",
          (body.get("last_name"), body.get("first_name"), body.get("middle_name"))
          == ("Асанов", "Бакыт", "Асанович"), str(body))
    check("обращение: место (район, село, адрес)",
          (body.get("district"), body.get("settlement"), body.get("address"))
          == ("nooken", "Масы", "ул. Ленина 5"), str(body))
    check("обращение: исполнитель из справочника и категория",
          body.get("executor") == "org-1" and body.get("category") == "water", str(body))
    check("обращение: суть уходит как описание",
          body.get("description") == "Третий день нет воды", str(body))
    check("обращение: продиктованный телефон", body.get("phone") == "996555123456", str(body))
    check("обращение: тема отправлена", body.get("title") == "Нет воды в Масы", str(body))
    known = classify_module.describe_known(session.known)
    check("модель дальше знает ФИО, номер и место",
          "Асанов Бакыт Асанович" in known and "номер телефона известен" in known
          and "Ноокенский район" in known and "Масы" in known, known)

    session.add("user", "А ещё напор слабый был всю неделю до этого, это важно", 20, crm_id=600)
    session.last_message_id = 600
    panel.requests.clear()
    await classify_module.update_ticket(client, session, "openai")
    body = (classify_bodies(panel, 1) or [{}])[0]
    check("тема заявки отправляется один раз", "title" not in body, str(body))

    # --- обращение заполнено: короткое «спасибо» не перечитываем
    calls = len(stub.systems)
    session.add("user", "спасибо", 20, crm_id=601)
    await classify_module.update_ticket(client, session, "openai")
    check("заполненное обращение: «спасибо» не тратит запрос к модели",
          len(stub.systems) == calls, str(len(stub.systems)))

    # --- номер из WhatsApp надёжнее продиктованного
    stub = SequencedStub([full])
    classify_module.get_provider = lambda name: stub
    session = new_session(("user", "нет воды"), ("assistant", "Где именно?"))
    session.known.update(phone="996700111222", phone_source="channel")
    panel.requests.clear()
    await classify_module.update_ticket(client, session, "openai")
    body = (classify_bodies(panel, 1) or [{}])[0]
    check("номер из канала не подменяется продиктованным",
          body.get("phone") == "996700111222", str(body))

    # --- житель заговорил о другой проблеме: новая заявка
    split = ('{"kind": "appeal", "title": "Яма на дороге", "summary": "Яма",'
             ' "category": "roads", "new_topic": true}')
    after = ('{"kind": "appeal", "title": "Яма на дороге", "summary": "Яма у школы",'
             ' "category": "roads", "settlement": "Кочкор-Ата"}')
    stub = SequencedStub([split, after])
    classify_module.get_provider = lambda name: stub
    session = new_session(("user", "Нет воды в Масы"), ("assistant", "Передали в водоканал."))
    session.known.update(kind="appeal", summary="Нет воды в Масы", last_name="Асанов",
                         first_name="Бакыт", settlement="Масы")
    session.add("user", "И ещё: у школы в Кочкор-Ате огромная яма на дороге", 20, crm_id=777)
    session.last_message_id = 777
    session.add("assistant", "Понял, запишу.", 20)
    panel.requests.clear(); panel.split_calls.clear(); panel.ticket_id = 1
    await classify_module.update_ticket(client, session, "openai")
    check("смена темы: панель просят разделить заявку с нужного сообщения",
          bool(panel.split_calls) and panel.split_calls[0][0].endswith("/tickets/1/split/")
          and panel.split_calls[0][1].get("from_message_id") == 777, str(panel.split_calls))
    check("смена темы: бот ведёт уже новую заявку", session.ticket_id == 2, str(session.ticket_id))
    check("смена темы: ФИО жителя сохранились, место старой заявки забыто",
          session.known.get("last_name") == "Асанов"
          and session.known.get("settlement") == "Кочкор-Ата", str(session.known))
    check("смена темы: новая заявка сразу разобрана по новой теме",
          bool(classify_bodies(panel, 2))
          and classify_bodies(panel, 2)[0].get("category") == "roads",
          str(classify_bodies(panel, 2)))
    check("смена темы: старую заявку классификация не трогала",
          not classify_bodies(panel, 1), str(classify_bodies(panel, 1)))
    marked = [m for m in session.history if m.get("topic_start")]
    check("смена темы: начало новой темы помечено в истории",
          len(marked) == 1 and marked[0].get("crm_id") == 777, str(marked))
    check("смена темы: разбор новой заявки видит только новую тему",
          "Нет воды в Масы" not in classify_module._conversation(session.history))

    # --- пока модель думала, заявка сменилась: старый разбор выбрасывается
    class Switcher:
        def __init__(self, session):
            self.session = session

        async def ask(self, system, history, max_tokens, detailed):
            self.session.ticket_id = 5
            return full

    session = new_session(("user", "нет воды"), ("assistant", "Где?"))
    classify_module.get_provider = lambda name: Switcher(session)
    panel.requests.clear()
    await classify_module.update_ticket(client, session, "openai")
    check("устаревший разбор не пишется в старую заявку",
          not classify_bodies(panel, 1), str(classify_bodies(panel, 1)))

    # --- два сообщения подряд: второй разбор не запускается параллельно,
    #     а прогоняется ещё раз после первого — уже по свежей переписке
    gate = _asyncio.Event()

    class Slow:
        def __init__(self):
            self.calls = 0

        async def ask(self, system, history, max_tokens, detailed):
            self.calls += 1
            await gate.wait()
            return '{"kind": "question"}'

    slow = Slow()
    classify_module.get_provider = lambda name: slow
    session = new_session(("user", "где ЦОН"), ("assistant", "ЦОН на Ленина"))
    first = _asyncio.create_task(classify_module.update_ticket(client, session, "openai"))
    await _asyncio.sleep(0.01)
    await classify_module.update_ticket(client, session, "openai")
    check("параллельный разбор той же заявки не запускается", slow.calls == 1, str(slow.calls))
    gate.set()
    await first
    check("после первого разбора — ещё один по свежей переписке", slow.calls == 2, str(slow.calls))
    check("после разбора заявка снята с учёта", 1 not in classify_module._RUNNING)

    # --- сбой модели не роняет задачу и ничего не пишет
    class Broken:
        async def ask(self, *a, **kw):
            raise ProviderError("временная ошибка")

    classify_module.get_provider = lambda name: Broken()
    session = new_session(("user", "нет воды"), ("assistant", "Где?"))
    panel.requests.clear()
    try:
        await classify_module.update_ticket(client, session, "openai")
        ok = True
    except Exception:
        ok = False
    check("сбой модели не роняет фоновую задачу", ok)
    check("при сбое в панель ничего не ушло", not classify_bodies(panel), str(panel.requests))

    remote.data = saved_data
    await client.close()


# ===========================================================================
# 14. Голосовые сообщения: transcribe.py, Telegram и WhatsApp
#     Платных запросов нет: клиент OpenAI и сама расшифровка подменены.
# ===========================================================================

class FakeOpenAI:
    """Подставной AsyncOpenAI: запоминает, с чем его создали и что спросили."""

    instances: list["FakeOpenAI"] = []
    text = "Расшифрованный текст"
    error: Exception | None = None

    def __init__(self, **kwargs):
        import types
        self.kwargs = kwargs
        self.calls: list[dict] = []
        self.audio = types.SimpleNamespace(
            transcriptions=types.SimpleNamespace(create=self._create))
        FakeOpenAI.instances.append(self)

    async def _create(self, **kwargs):
        import types
        self.calls.append(kwargs)
        if FakeOpenAI.error:
            raise FakeOpenAI.error
        return types.SimpleNamespace(text=FakeOpenAI.text)


async def test_transcribe_module() -> None:
    section("14. Расшифровка голоса (transcribe.py, без сети)")
    import tempfile
    import types
    from pathlib import Path

    import httpx
    import openai

    import providers
    import transcribe as tr

    check("расширение по mime из WhatsApp (с codecs=opus) — .ogg",
          tr.upload_extension("/x/file", "audio/ogg; codecs=opus") == ".ogg")
    check("mime mp3/m4a/mp4/wav распознаются",
          [tr.upload_extension("/x/f", m) for m in
           ("audio/mpeg", "audio/mp4", "video/mp4", "audio/x-wav")]
          == [".mp3", ".m4a", ".mp4", ".wav"])
    check("mime важнее неверного суффикса",
          tr.upload_extension("/x/voice.bin", "audio/ogg") == ".ogg")
    check("без mime берётся суффикс файла, .oga -> .ogg",
          tr.upload_extension("/x/a.oga") == ".ogg" and tr.upload_extension("/x/a.MP3") == ".mp3")
    check("неизвестный формат -> пусто",
          tr.upload_extension("/x/a.xyz") == "" and tr.upload_extension("/x/a", "image/png") == "")

    saved_client_cls = tr.AsyncOpenAI
    saved_settings = tr.settings
    saved_model_env = os.environ.pop("TRANSCRIBE_MODEL", None)
    tr.AsyncOpenAI = FakeOpenAI
    tr._client_cache.clear()
    FakeOpenAI.instances.clear()
    FakeOpenAI.error = None
    providers.apply_overrides({})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "voice.ogg"
            audio.write_bytes(b"OggS-fake-audio")

            # --- успешный вызов: модель, имя файла, подсказка, язык не задан
            FakeOpenAI.text = "  Нет воды в Ноокене  "
            text = await tr.transcribe(str(audio), mime="audio/ogg; codecs=opus")
            call = FakeOpenAI.instances[-1].calls[-1]
            check("расшифровка возвращает текст без пробелов по краям",
                  text == "Нет воды в Ноокене", repr(text))
            check("модель по умолчанию — gpt-4o-transcribe", call["model"] == "gpt-4o-transcribe",
                  str(call.get("model")))
            check("файл уходит с правильным расширением в имени",
                  call["file"][0] == "audio.ogg" and call["file"][1] == b"OggS-fake-audio",
                  str(call["file"][0]))
            check("язык не фиксируется (речь смешанная ru/ky)", "language" not in call)
            check("подсказка содержит словарь области",
                  all(w in call["prompt"] for w in
                      ("Жалал-Абад", "Ноокен", "Кербен", "айыл өкмөтү", "Социальный фонд")))
            check("таймаут клиента — 60 с",
                  FakeOpenAI.instances[-1].kwargs.get("timeout") == 60.0,
                  str(FakeOpenAI.instances[-1].kwargs))

            # --- модель из окружения
            os.environ["TRANSCRIBE_MODEL"] = "whisper-1"
            await tr.transcribe(str(audio), mime="audio/ogg")
            check("TRANSCRIBE_MODEL переопределяет модель",
                  FakeOpenAI.instances[-1].calls[-1]["model"] == "whisper-1")
            os.environ.pop("TRANSCRIBE_MODEL")

            # --- клиент кэшируется по ключу и пересоздаётся при смене ключа
            created = len(FakeOpenAI.instances)
            await tr.transcribe(str(audio), mime="audio/ogg")
            check("клиент переиспользуется, пока ключ тот же",
                  len(FakeOpenAI.instances) == created)
            providers.apply_overrides({"openai": {"key": "sk-from-panel"}})
            await tr.transcribe(str(audio), mime="audio/ogg")
            check("ключ из панели перекрывает .env и даёт нового клиента",
                  FakeOpenAI.instances[-1].kwargs.get("api_key") == "sk-from-panel"
                  and len(FakeOpenAI.instances) == created + 1)
            providers.apply_overrides({})

            # --- пустая расшифровка и эхо подсказки
            FakeOpenAI.text = "   "
            check("пустая расшифровка -> пустая строка",
                  await tr.transcribe(str(audio), mime="audio/ogg") == "")
            FakeOpenAI.text = "Населённые пункты: Жалал-Абад, Манас, Ноокен, Сузак, Базар-Коргон"
            check("кусок подсказки вместо речи (тишина) -> пусто",
                  await tr.transcribe(str(audio), mime="audio/ogg") == "")
            FakeOpenAI.text = "Манас"
            check("короткое слово из словаря — настоящая речь, не отбрасывается",
                  await tr.transcribe(str(audio), mime="audio/ogg") == "Манас")

            # --- слишком большой файл: ошибка ДО обращения к сети
            big = Path(tmp) / "big.ogg"
            with big.open("wb") as f:
                f.truncate(26 * 1024 * 1024)
            calls_before = sum(len(i.calls) for i in FakeOpenAI.instances)
            try:
                await tr.transcribe(str(big), mime="audio/ogg")
                check("файл > 25 МБ -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("файл > 25 МБ -> TranscribeError", "лимита" in e.message, e.message)
                check("для большого файла есть текст жителю (сократить)",
                      "сократите" in e.public_text and "Кыскартып" in e.public_text)
            check("большой файл не отправлялся в OpenAI",
                  sum(len(i.calls) for i in FakeOpenAI.instances) == calls_before)

            # --- слишком длинная запись (длительность известна от Telegram)
            try:
                await tr.transcribe(str(audio), duration=601, mime="audio/ogg")
                check("запись > 10 минут -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("запись > 10 минут -> TranscribeError с просьбой сократить",
                      "сократите" in e.public_text, e.public_text)
            check("ровно 10 минут ещё принимаются",
                  await tr.transcribe(str(audio), duration=600, mime="audio/ogg") == "Манас")

            # --- неподдерживаемый формат
            weird = Path(tmp) / "voice.xyz"
            weird.write_bytes(b"x")
            try:
                await tr.transcribe(str(weird))
                check("неподдерживаемый формат -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("неподдерживаемый формат -> TranscribeError",
                      "формат" in e.message, e.message)

            # --- ошибки OpenAI -> понятный текст, а не traceback
            request = httpx.Request("POST", "https://api.openai.test/v1/audio/transcriptions")
            FakeOpenAI.error = openai.RateLimitError(
                "quota", response=httpx.Response(429, request=request), body=None)
            try:
                await tr.transcribe(str(audio), mime="audio/ogg")
                check("лимит/баланс -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("лимит/баланс -> TranscribeError", "баланс" in e.message, e.message)
                check("у ошибки баланса нет текста жителю (будет общий)", e.public_text == "")
            FakeOpenAI.error = openai.APIConnectionError(request=request)
            try:
                await tr.transcribe(str(audio), mime="audio/ogg")
                check("обрыв сети -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("обрыв сети -> TranscribeError", "нет связи" in e.message.lower(), e.message)
            FakeOpenAI.error = openai.APITimeoutError(request=request)
            try:
                await tr.transcribe(str(audio), mime="audio/ogg")
                check("таймаут -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("таймаут -> TranscribeError", "времени" in e.message, e.message)
            FakeOpenAI.error = openai.AuthenticationError(
                "bad key", response=httpx.Response(401, request=request), body=None)
            try:
                await tr.transcribe(str(audio), mime="audio/ogg")
                check("неверный ключ -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("неверный ключ -> TranscribeError", "ключ" in e.message, e.message)
            FakeOpenAI.error = None

            # --- ключа нет ни в панели, ни в .env
            tr._client_cache.clear()
            tr.settings = types.SimpleNamespace(openai_api_key=None)
            try:
                await tr.transcribe(str(audio), mime="audio/ogg")
                check("нет ключа -> TranscribeError", False)
            except tr.TranscribeError as e:
                check("нет ключа -> TranscribeError", "ключ" in e.message.lower(), e.message)
    finally:
        tr.AsyncOpenAI = saved_client_cls
        tr.settings = saved_settings
        tr._client_cache.clear()
        providers.apply_overrides({})
        if saved_model_env is not None:
            os.environ["TRANSCRIBE_MODEL"] = saved_model_env


def make_voice_update(kind: str = "voice", chat_id: int = 610, message_id: int = 950):
    """Входящее голосовое / аудиофайл / видеокружок так, как их присылает Telegram."""
    import datetime

    from aiogram.types import Audio, Chat, Message, Update, User, VideoNote, Voice

    fields: dict = {}
    if kind == "voice":
        fields["voice"] = Voice(file_id="v", file_unique_id="vu", duration=7,
                                mime_type="audio/ogg", file_size=4000)
    elif kind == "audio":
        fields["audio"] = Audio(file_id="a", file_unique_id="au", duration=30,
                                file_name="zapis.m4a", mime_type="audio/mp4",
                                file_size=90000)
    elif kind == "video_note":
        fields["video_note"] = VideoNote(file_id="n", file_unique_id="nu", length=240,
                                         duration=12, file_size=50000)
    elif kind == "big_voice":
        fields["voice"] = Voice(file_id="v", file_unique_id="vu", duration=7,
                                mime_type="audio/ogg", file_size=30 * 1024 * 1024)
    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="Житель"),
        **fields,
    )
    return Update(update_id=message_id, message=msg)


async def test_voice_telegram() -> None:
    section("15. Telegram: голосовые сообщения")
    import tempfile
    from pathlib import Path

    import bot as bot_module
    import classify as classify_module
    import conversation as conversation_module
    import transcribe as tr
    from aiogram import Bot
    from remote_config import remote

    panel = FakePanel()
    client = connect_fake_panel(panel)

    fake = FakeTelegram()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"], session=fake.build_session())
    stub = StubProvider()
    stub.answer = "Передали в водоканал."
    conversation_module.get_provider = lambda name: stub
    classify_module.get_provider = lambda name: stub
    storage = bot_module.storage
    dp = bot_module.dp

    # Что «услышал» подставной распознаватель и что ему передали.
    heard = {"text": "В Ноокене нет воды третий день", "error": None, "calls": []}

    async def fake_transcribe(path, *, duration=None, mime=None):
        heard["calls"].append({"path": path, "duration": duration, "mime": mime,
                               "exists": Path(path).exists()})
        if heard["error"]:
            raise heard["error"]
        return heard["text"]

    saved_transcribe = tr.transcribe
    saved_media_dir = bot_module.MEDIA_DIR
    tr.transcribe = fake_transcribe
    tmp = tempfile.TemporaryDirectory()
    bot_module.MEDIA_DIR = Path(tmp.name)

    async def feed(update):
        await dp.feed_update(bot, update)
        await asyncio.sleep(0.05)  # даём фоновым задачам (классификация) отработать

    def incoming_bodies():
        return [body for _, path, body in panel.requests if path.endswith("/tickets/incoming/")]

    chat = 610
    try:
        # --- голосовое: расшифровка -> модель -> ответ, в панель текст и файл
        storage.get(chat).clear()
        storage.get(chat).restored = True
        fake.clear(); panel.requests.clear(); heard["calls"].clear()
        calls_before = len(stub.calls)
        await feed(make_voice_update("voice", chat))
        sent_to_model = (citizen_part(stub.calls[-1][-1]["content"])
                         if len(stub.calls) > calls_before else "")
        check("TG голосовое: расшифровка вызвана с mime и длительностью",
              bool(heard["calls"]) and heard["calls"][0]["mime"] == "audio/ogg"
              and heard["calls"][0]["duration"] == 7, str(heard["calls"]))
        check("TG голосовое: файл был скачан до расшифровки",
              bool(heard["calls"]) and heard["calls"][0]["exists"]
              and heard["calls"][0]["path"].endswith(".ogg"), str(heard["calls"]))
        check("TG голосовое: модель получила текст с пометкой об автоматической расшифровке",
              sent_to_model.startswith("[Голосовое сообщение, автоматическая расшифровка")
              and "возможны ошибки распознавания" in sent_to_model
              and sent_to_model.endswith(heard["text"]), sent_to_model)
        check("TG голосовое: житель получил ответ модели", stub.answer in fake.texts(),
              str(fake.texts()))
        check("TG голосовое: показан статус «печатает» на время расшифровки",
              any(c[0] == "SendChatAction" for c in fake.calls))
        bodies = incoming_bodies()
        check("TG голосовое: в карточку ушла расшифровка с пометкой [Голосовое]",
              bool(bodies) and f"[Голосовое] {heard['text']}".encode() in bodies[0], str(bodies)[:200])
        check("TG голосовое: сам аудиофайл приложен к карточке",
              bool(bodies) and b"_voice.ogg" in bodies[0])
        await settle()   # карточка дозаполняется фоном после ответа
        check("TG голосовое: тема карточки определяется (классификация ушла)",
              any(p.endswith("/tickets/1/classify/") for _, p, _ in panel.requests))
        check("TG голосовое: ответ ИИ записан в карточку",
              any(p.endswith("/tickets/1/messages/") for _, p, _ in panel.requests))

        # --- аудиофайл и видеокружок тоже маршрутизируются
        for kind, suffix, seconds in (("audio", ".m4a", 30), ("video_note", ".mp4", 12)):
            storage.get(chat).clear()
            fake.clear(); panel.requests.clear(); heard["calls"].clear()
            calls_before = len(stub.calls)
            await feed(make_voice_update(kind, chat, message_id=960))
            check(f"TG {kind}: расшифровка вызвана, файл с расширением {suffix}",
                  bool(heard["calls"]) and heard["calls"][0]["path"].endswith(suffix),
                  str(heard["calls"]))
            check(f"TG {kind}: длительность {seconds} с передана расшифровке",
                  bool(heard["calls"]) and heard["calls"][0]["duration"] == seconds,
                  str(heard["calls"]))
            check(f"TG {kind}: модель получила расшифровку",
                  len(stub.calls) > calls_before
                  and stub.calls[-1][-1]["content"].endswith(heard["text"]))
            check(f"TG {kind}: файл приложен к карточке",
                  bool(incoming_bodies()) and suffix.encode() in incoming_bodies()[0])

        # --- пустая расшифровка: вежливый текст, модель не зовётся, файл в панели
        storage.get(chat).clear()
        panel.created = True
        heard["text"] = ""
        fake.clear(); panel.requests.clear()
        calls_before = len(stub.calls)
        await feed(make_voice_update("voice", chat, message_id=970))
        out = " ".join(fake.texts())
        check("TG пустая расшифровка: вежливый двуязычный ответ",
              "Не удалось разобрать голосовое сообщение" in out
              and "Үн билдирүүнү түшүнө алган жокмун" in out, out)
        check("TG пустая расшифровка: модель не вызвана", len(stub.calls) == calls_before)
        check("TG пустая расшифровка: история модели пуста", storage.get(chat).history == [],
              str(storage.get(chat).history))
        bodies = incoming_bodies()
        check("TG пустая расшифровка: в панель ушла запись [Голосовое, не распознано] с файлом",
              bool(bodies) and "[Голосовое, не распознано]".encode() in bodies[0]
              and b"_voice.ogg" in bodies[0], str(bodies)[:200])
        check("TG пустая расшифровка: классификация по пустышке не запускалась",
              not any(p.endswith("/classify/") for _, p, _ in panel.requests))
        check("TG пустая расшифровка: ответ бота записан в карточку",
              any(p.endswith("/tickets/1/messages/") for _, p, _ in panel.requests))

        # --- ошибка расшифровки: общий текст без технических деталей
        heard["error"] = tr.TranscribeError(
            "OpenAI: лимит запросов или закончились средства на балансе, ключ sk-secret")
        fake.clear(); panel.requests.clear()
        calls_before = len(stub.calls)
        await feed(make_voice_update("voice", chat, message_id=971))
        out = " ".join(fake.texts())
        check("TG ошибка расшифровки: вежливый двуязычный ответ",
              "Не удалось разобрать голосовое сообщение" in out
              and "Кайра жибериңиз" in out, out)
        check("TG ошибка расшифровки: жителю не видно технических деталей",
              not any(w in out for w in ("OpenAI", "баланс", "ключ", "sk-", "лимит")), out)
        check("TG ошибка расшифровки: модель не вызвана", len(stub.calls) == calls_before)
        check("TG ошибка расшифровки: запись всё равно ушла сотруднику",
              bool(incoming_bodies()) and b"_voice.ogg" in incoming_bodies()[0])

        # --- слишком длинное: отдельный текст «сократите»
        heard["error"] = tr.TranscribeError("длиннее 10 минут", public_text=tr.TOO_LONG_REPLY)
        fake.clear()
        await feed(make_voice_update("voice", chat, message_id=972))
        check("TG слишком длинная запись: просьба сократить",
              "сократите" in " ".join(fake.texts()), " ".join(fake.texts()))
        heard["error"] = RuntimeError("неожиданный сбой")
        fake.clear()
        await feed(make_voice_update("voice", chat, message_id=973))
        check("TG неожиданный сбой расшифровки не роняет бота и не пугает жителя",
              "Не удалось разобрать" in " ".join(fake.texts())
              and "неожиданный" not in " ".join(fake.texts()))
        heard["error"] = None

        # --- сотрудник ведёт разговор: бот молчит и на голосовое
        heard["text"] = "Где моя справка?"
        panel.answer_mode = "staff"
        fake.clear()
        await feed(make_voice_update("voice", chat, message_id=974))
        check("TG голосовое в режиме сотрудника: бот молчит",
              not [c for c in fake.calls if c[0] == "SendMessage"], str(fake.texts()))
        heard["text"] = ""
        fake.clear()
        await feed(make_voice_update("voice", chat, message_id=975))
        check("TG нераспознанное голосовое в режиме сотрудника: бот тоже молчит",
              not [c for c in fake.calls if c[0] == "SendMessage"], str(fake.texts()))
        panel.answer_mode = "ai"

        # --- бот выключен: техработы, расшифровка (платная) не вызывается
        heard["text"] = "Любой текст"
        remote.data = {"enabled": False, "maintenance_text": "Идут технические работы."}
        heard["calls"].clear()
        fake.clear()
        await feed(make_voice_update("voice", chat, message_id=976))
        check("TG голосовое при выключенном боте: текст техработ",
              "технические работы" in " ".join(fake.texts()).lower(), str(fake.texts()))
        check("TG голосовое при выключенном боте: расшифровка не вызвана", heard["calls"] == [])
        remote.data = {}

        # --- файл больше лимита Telegram: вежливый отказ, ничего не скачивается
        heard["calls"].clear()
        fake.clear()
        await feed(make_voice_update("big_voice", chat, message_id=977))
        check("TG запись > 20 МБ: вежливый отказ",
              "20 МБ" in " ".join(fake.texts()), str(fake.texts()))
        check("TG запись > 20 МБ: расшифровка не вызвана", heard["calls"] == [])

        # --- стикер: текст теперь говорит и про голосовые
        fake.clear()
        await feed(make_message("/start", chat_id=chat, message_id=978))
        check("/start упоминает голосовые сообщения", "голосов" in " ".join(fake.texts()).lower())
        fake.clear()
        await feed(make_sticker(chat_id=chat))
        out = " ".join(fake.texts())
        check("на стикер бот отвечает, что понимает голосовые, а стикеры нет",
              "голосовые сообщения" in out and "Стикеры" in out, out)
    finally:
        tr.transcribe = saved_transcribe
        bot_module.MEDIA_DIR = saved_media_dir
        remote.data = {}
        tmp.cleanup()
        await client.close()
        await bot.session.close()


async def test_voice_whatsapp() -> None:
    section("16. WhatsApp: голосовые сообщения")
    import tempfile
    from pathlib import Path

    import classify as classify_module
    import conversation as conversation_module
    import httpx
    import transcribe as tr
    import whatsapp_bot as wa

    # --- разбор вебхука: audio разбирается как image/document
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "996700123456", "profile": {"name": "Айгуль"}}],
        "messages": [{"from": "996700123456", "id": "wamid.9", "type": "audio",
                      "audio": {"id": "MEDIA42", "mime_type": "audio/ogg; codecs=opus",
                                "sha256": "x", "voice": True}}],
    }}]}]}
    parsed = wa.parse_webhook_payload(payload)
    check("WA вебхук: голосовое разобрано как audio с media_id и mime",
          len(parsed) == 1 and parsed[0]["type"] == "audio"
          and parsed[0]["media_id"] == "MEDIA42"
          and parsed[0]["media_mime"] == "audio/ogg; codecs=opus"
          and parsed[0]["text"] == "", str(parsed))

    panel = FakePanel()
    client = connect_fake_panel(panel)
    stub = StubProvider()
    stub.answer = "Передали в водоканал."
    conversation_module.get_provider = lambda name: stub
    classify_module.get_provider = lambda name: stub

    sent: list[tuple[str, str]] = []

    async def fake_send(phone, text):
        sent.append((phone, text))
        return True, ""

    wa.send_whatsapp_text = fake_send

    heard = {"text": "Свет не дают второй день, Сузак", "error": None, "calls": []}

    async def fake_transcribe(path, *, duration=None, mime=None):
        heard["calls"].append({"path": path, "mime": mime, "exists": Path(path).exists()})
        if heard["error"]:
            raise heard["error"]
        return heard["text"]

    # Двухшаговое скачивание Meta на подставном сервере: media_id -> ссылка -> файл.
    media_requests: list[tuple[str, str | None]] = []

    def media_handler(request):
        media_requests.append((str(request.url), request.headers.get("Authorization")))
        if str(request.url).endswith("/MEDIA42"):
            return httpx.Response(200, json={"url": "https://lookaside.test/f/42",
                                             "file_size": 15})
        if str(request.url) == "https://lookaside.test/f/42":
            return httpx.Response(200, content=b"OggS-voice-bytes")
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        return real_async_client(*args, transport=httpx.MockTransport(media_handler), **kwargs)

    saved_transcribe = tr.transcribe
    saved_media_dir = wa.MEDIA_DIR
    tr.transcribe = fake_transcribe
    tmp = tempfile.TemporaryDirectory()
    wa.MEDIA_DIR = Path(tmp.name)
    httpx.AsyncClient = patched_client

    chat_id = 996700123456
    phone = str(chat_id)
    msg = {"phone": phone, "type": "audio", "text": "", "wa_message_id": "w9",
           "name": "Айгуль", "media_id": "MEDIA42",
           "media_mime": "audio/ogg; codecs=opus", "media_filename": ""}

    def incoming_bodies():
        return [body for _, path, body in panel.requests if path.endswith("/tickets/incoming/")]

    try:
        wa.storage.get(chat_id).clear()
        wa.storage.get(chat_id).restored = True

        # --- полный диалог: скачали, расшифровали, спросили модель, ответили
        panel.requests.clear()
        calls_before = len(stub.calls)
        await wa.handle_incoming_message(dict(msg))
        await asyncio.sleep(0.05)
        check("WA голосовое: файл скачан с токеном Meta на обоих шагах",
              len(media_requests) == 2
              and all(auth == "Bearer test-access-token" for _, auth in media_requests),
              str(media_requests))
        check("WA голосовое: файл сохранён с расширением .ogg и расшифровке передан mime",
              bool(heard["calls"]) and heard["calls"][0]["path"].endswith(".ogg")
              and heard["calls"][0]["exists"]
              and heard["calls"][0]["mime"] == "audio/ogg; codecs=opus", str(heard["calls"]))
        sent_to_model = (citizen_part(stub.calls[-1][-1]["content"])
                         if len(stub.calls) > calls_before else "")
        check("WA голосовое: модель получила текст с пометкой об автоматической расшифровке",
              sent_to_model.startswith("[Голосовое сообщение, автоматическая расшифровка")
              and sent_to_model.endswith(heard["text"]), sent_to_model)
        check("WA голосовое: житель получил ответ модели",
              bool(sent) and sent[-1] == (phone, stub.answer), str(sent))
        bodies = incoming_bodies()
        check("WA голосовое: в карточку ушла расшифровка с пометкой [Голосовое]",
              bool(bodies) and f"[Голосовое] {heard['text']}".encode() in bodies[0])
        check("WA голосовое: сам аудиофайл приложен к карточке",
              bool(bodies) and b".ogg" in bodies[0] and b"OggS-voice-bytes" in bodies[0])
        check("WA голосовое: канал в обращении — whatsapp",
              bool(bodies) and b"whatsapp" in bodies[0])

        # --- пустая расшифровка
        wa.storage.get(chat_id).clear()
        heard["text"] = ""
        sent.clear(); panel.requests.clear(); panel.created = True
        calls_before = len(stub.calls)
        await wa.handle_incoming_message(dict(msg))
        await asyncio.sleep(0.05)
        out = " ".join(t for _, t in sent)
        check("WA пустая расшифровка: вежливый двуязычный ответ",
              "Не удалось разобрать голосовое сообщение" in out
              and "Үн билдирүүнү түшүнө алган жокмун" in out, out)
        check("WA пустая расшифровка: модель не вызвана", len(stub.calls) == calls_before)
        bodies = incoming_bodies()
        check("WA пустая расшифровка: запись ушла в панель с файлом",
              bool(bodies) and "[Голосовое, не распознано]".encode() in bodies[0]
              and b"OggS-voice-bytes" in bodies[0])
        check("WA пустая расшифровка: классификация по пустышке не запускалась",
              not any(p.endswith("/classify/") for _, p, _ in panel.requests))

        # --- ошибка расшифровки
        heard["error"] = tr.TranscribeError("OpenAI: неверный API-ключ sk-secret")
        sent.clear()
        calls_before = len(stub.calls)
        await wa.handle_incoming_message(dict(msg))
        out = " ".join(t for _, t in sent)
        check("WA ошибка расшифровки: общий текст без технических деталей",
              "Не удалось разобрать" in out
              and not any(w in out for w in ("OpenAI", "ключ", "sk-")), out)
        check("WA ошибка расшифровки: модель не вызвана", len(stub.calls) == calls_before)
        heard["error"] = None

        # --- режим сотрудника: бот молчит
        panel.answer_mode = "staff"
        heard["text"] = "Где моя справка?"
        sent.clear()
        await wa.handle_incoming_message(dict(msg))
        check("WA голосовое в режиме сотрудника: бот молчит", sent == [], str(sent))
        panel.answer_mode = "ai"

        # --- бот выключен: расшифровка не вызывается
        wa.remote.data = {"enabled": False, "maintenance_text": "Идут технические работы."}
        heard["calls"].clear(); sent.clear()
        await wa.handle_incoming_message(dict(msg))
        check("WA голосовое при выключенном боте: текст техработ без расшифровки",
              heard["calls"] == [] and bool(sent) and "технические работы" in sent[-1][1],
              str(sent))
        wa.remote.data = {}

        # --- не удалось скачать файл: просьба повторить, обращение не заводится
        sent.clear(); panel.requests.clear()
        bad = dict(msg, media_id="NOPE")
        await wa.handle_incoming_message(bad)
        check("WA голосовое, файл не скачался: вежливая просьба повторить",
              bool(sent) and "голосовое" in sent[-1][1].lower(), str(sent))
        check("WA голосовое, файл не скачался: обращение не создаётся",
              incoming_bodies() == [])

        # --- остальные типы по-прежнему получают заглушку, но уже с упоминанием голоса
        sent.clear()
        await wa.handle_incoming_message({"phone": phone, "type": "sticker", "text": "",
                                          "wa_message_id": "w10", "name": ""})
        check("WA стикер: заглушка упоминает голосовые сообщения",
              bool(sent) and "голосовые" in sent[-1][1], str(sent))
    finally:
        httpx.AsyncClient = real_async_client
        tr.transcribe = saved_transcribe
        wa.MEDIA_DIR = saved_media_dir
        wa.remote.data = {}
        tmp.cleanup()
        await client.close()


# ===========================================================================

# ===========================================================================
# 17. Справочник в ответе и поиск в интернете (conversation.py, без сети)
# ===========================================================================

class FakeKnowledge:
    """Подставной справочник с интерфейсом knowledge.KnowledgeBase."""

    enabled = True

    def __init__(self):
        self.calls: list[tuple] = []
        self.delay = 0.0
        self.error: Exception | None = None

    async def search(self, query, embed=None, territory_hint="", limit=5):
        self.calls.append((query, embed, territory_hint))
        if self.error:
            raise self.error
        if self.delay and embed is not None:
            await asyncio.sleep(self.delay)
        return ["hit"]

    async def search_constitution(self, query, embed=None, limit=2):
        return []

    def candidate_organizations(self, hits):
        return [{"id": "org-1", "name": "Водоканал Ноокен"}]

    def format_context(self, hits, arts=None, max_chars=2400):
        return "Справочник (проверенные данные):\n1. Водоканал Ноокен. Тел.: 0372 50000"


class SearchStub(StubProvider):
    """Подставной OpenAI с поиском: отвечает из списка, поиск — отдельно."""

    def __init__(self, answers: list[str], found: str | Exception = "Найдено в интернете."):
        super().__init__()
        self.answers = list(answers)
        self.found = found
        self.search_systems: list[str] = []

    async def ask(self, system, history, max_tokens, detailed):
        self.calls.append([dict(m) for m in history])
        self.systems.append(system)
        item = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def search_web(self, system, history, max_tokens):
        self.search_systems.append(system)
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


async def test_assistant_pipeline() -> None:
    section("17. Справочник и поиск в интернете (conversation.py)")
    import conversation as conversation_module
    import prompts
    from providers import ProviderError, reset_availability
    from storage import Session

    conv = conversation_module

    # --- служебная строка «ПОИСК:»
    cases = {
        "ПОИСК: график ЦОН Ноокен": "график ЦОН Ноокен",
        "**ПОИСК:** адрес мэрии": "адрес мэрии",
        "Секунду.\nПОИСК: телефон ЦСМ Масы": "телефон ЦСМ Масы",
        "ПОИСК:": "",
        "Здравствуйте! Чем помочь?": None,
        "Мы ведём ПОИСК: решения": None,
    }
    for text, expected in cases.items():
        check(f"разбор «ПОИСК:»: {text[:24]!r}", prompts.parse_search_request(text) == expected,
              repr(prompts.parse_search_request(text)))

    # --- справочные блоки приклеиваются к вопросу, история сессии не меняется
    history = [{"role": "user", "content": "старый вопрос"},
               {"role": "assistant", "content": "ответ"},
               {"role": "user", "content": "нет воды", "images": ["a.jpg"]}]
    out = conv.with_context(history, knowledge="Справочник: Водоканал", citizen="ФИО — Асанов")
    check("блоки справочника перед вопросом жителя",
          out[-1]["content"].startswith("[Справочник: Водоканал]")
          and "[О жителе: ФИО — Асанов]" in out[-1]["content"]
          and out[-1]["content"].endswith("[Сообщение жителя]\nнет воды"), out[-1]["content"])
    check("история сессии не меняется", history[-1]["content"] == "нет воды")
    check("фото к вопросу сохраняются", out[-1].get("images") == ["a.jpg"])
    check("без блоков — та же история", conv.with_context(history) is history)
    check("начало переписки не меняется (кэш промпта OpenAI)", out[:2] == history[:2])

    # --- текст для поиска
    check("служебные пометки не идут в поиск",
          conv.strip_service("[Голосовое сообщение, автоматическая расшифровка — "
                             "возможны ошибки распознавания] суу жок") == "суу жок")
    short = [{"role": "user", "content": "В Масы нет воды"},
             {"role": "assistant", "content": "Понял"},
             {"role": "user", "content": "а куда звонить?"}]
    check("короткий вопрос ищется вместе с предыдущим",
          conv.search_query(short) == "В Масы нет воды а куда звонить?", conv.search_query(short))
    long_q = "Подскажите, как получить земельный участок в Токтогульском районе"
    check("длинный вопрос ищется сам по себе",
          conv.search_query(short + [{"role": "user", "content": long_q}]) == long_q)

    # --- поиск по справочнику
    saved = conv._knowledge, conv._embed, conv.LOOKUP_TIMEOUT
    kb = FakeKnowledge()
    conv._knowledge, conv._embed = kb, "EMBED"
    session = Session(provider="openai")
    session.add("user", "нет воды", 20)
    session.known.update(district_name="Ноокенский район", settlement="Масы")
    text = await conv.knowledge_context(session, "chat")
    check("справочник: блок найден", "Водоканал Ноокен" in text, text)
    check("справочник: район и село подсказаны поиску",
          kb.calls and kb.calls[-1][2] == "Ноокенский район, Масы", str(kb.calls))
    check("справочник: кандидаты в исполнители запомнены",
          session.candidates == [{"id": "org-1", "name": "Водоканал Ноокен"}], str(session.candidates))

    kb.delay = 5
    conv.LOOKUP_TIMEOUT = 0.05
    kb.calls.clear()
    text = await conv.knowledge_context(session, "chat")
    check("справочник: долгий смысловой поиск -> только по словам",
          bool(text) and kb.calls[-1][1] is None, str(kb.calls))
    kb.delay = 0
    kb.error = RuntimeError("сломался индекс")
    check("справочник: сбой поиска не мешает ответу", await conv.knowledge_context(session, "chat") == "")
    kb.error = None
    empty = Session(provider="openai")
    check("справочник: нет вопроса — нет поиска", await conv.knowledge_context(empty, "chat") == "")
    conv._knowledge = None
    check("справочник не подключён — ответ без него",
          await conv.knowledge_context(session, "chat") == "")
    conv._knowledge, conv._embed, conv.LOOKUP_TIMEOUT = saved

    # --- поиск в интернете по требованию модели
    saved_get, saved_has = conv.get_provider, conv.has_key
    reset_availability()

    async def run(stub, preferred="openai"):
        conv.get_provider = lambda name: stub
        return await conv.ask_assistant(preferred, "SYSTEM", [{"role": "user", "content": "q"}], 500, False)

    stub = SearchStub(["Обычный ответ."])
    answer, by = await run(stub)
    check("поиск: обычный ответ отдаётся как есть, без поиска",
          answer == "Обычный ответ." and not stub.search_systems)

    stub = SearchStub(["ПОИСК: график работы ЦОН Ноокен"], found="ЦОН работает с 9 до 18. Источник: tunduk.gov.kg")
    answer, by = await run(stub)
    check("поиск: запрос модели уходит в поиск", answer.startswith("ЦОН работает"), answer)
    check("поиск: запрос подставлен в правила поиска",
          stub.search_systems and "график работы ЦОН Ноокен" in stub.search_systems[0]
          and "официальные источники" in stub.search_systems[0], str(stub.search_systems)[:200])
    check("поиск: житель не видит служебную строку", "ПОИСК" not in answer)
    check("поиск: модель чата не переключается из-за поиска", by == "openai")

    stub = SearchStub(["ПОИСК: телефон ЦСМ", "Точных данных нет, уточните в ЦСМ."],
                      found=ProviderError("поиск недоступен", retryable=True))
    answer, by = await run(stub)
    check("поиск сломался -> честный ответ без поиска",
          answer == "Точных данных нет, уточните в ЦСМ.", answer)
    check("повторный запрос знает, что поиска нет", "Поиск в интернете сейчас недоступен" in stub.systems[-1])

    stub = SearchStub(["ПОИСК: x"], found="ПОИСК: y")
    answer, _ = await run(stub)
    check("модель упорно просит поиск -> безопасная заглушка", answer == conv.NO_DATA_TEXT, answer)

    stub = SearchStub(["ПОИСК: x", ProviderError("лимит", retryable=True)],
                      found=ProviderError("нет поиска"))
    answer, _ = await run(stub)
    check("сбой повторного запроса не показывается жителю", answer == conv.NO_DATA_TEXT, answer)

    conv.has_key = lambda name: False
    stub = SearchStub(["ПОИСК: x", "Ответ без поиска."])
    answer, _ = await run(stub)
    check("нет ключа OpenAI -> без поиска, без служебной строки",
          answer == "Ответ без поиска." and not stub.search_systems, answer)
    conv.get_provider, conv.has_key = saved_get, saved_has

    # --- поиск у OpenAI: инструмент, страна, разметка ссылок (без сети)
    import types

    import httpx
    import openai
    from providers.openai_provider import OpenAIProvider, plain_links

    created: list[dict] = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        if len(created) == 1 and kwargs["tools"][0].get("user_location"):
            raise openai.BadRequestError(
                "user_location not supported",
                response=httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com")),
                body=None)
        return types.SimpleNamespace(
            output_text="ЦОН: пн-пт ([tunduk.gov.kg](https://tunduk.gov.kg/a?utm_source=openai)). **Важно**.")

    provider = OpenAIProvider()
    provider._client = types.SimpleNamespace(responses=types.SimpleNamespace(create=fake_create))
    text = await provider.search_web("SYS", [{"role": "user", "content": "где ЦОН", "images": ["x.jpg"]}], 900)
    check("OpenAI-поиск: инструмент web_search с привязкой к Кыргызстану",
          created[0]["tools"][0]["type"] == "web_search"
          and created[0]["tools"][0]["user_location"]["country"] == "KG", str(created[0]["tools"]))
    check("OpenAI-поиск: при отказе уточнений — голый поиск",
          len(created) == 2 and created[1]["tools"] == [{"type": "web_search"}], str(created[-1:]))
    check("OpenAI-поиск: фото в поиск не передаются",
          all("images" not in m for m in created[-1]["input"]), str(created[-1]["input"]))
    check("OpenAI-поиск: ссылки и выделение — обычным текстом",
          text == "ЦОН: пн-пт (https://tunduk.gov.kg/a). Важно.", text)
    check("ссылка с подписью сохраняет подпись",
          plain_links("[Портал](https://portal.kg/?a=1&utm_source=openai)") == "Портал (https://portal.kg/?a=1)")


# ===========================================================================
# 18. Номер телефона, геометка, смена заявки (Telegram и WhatsApp)
# ===========================================================================

def make_contact(chat_id: int, phone: str, user_id: int | None, message_id: int):
    import datetime

    from aiogram.types import Chat, Contact, Message, Update, User

    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="Житель"),
        contact=Contact(phone_number=phone, first_name="Бакыт", user_id=user_id),
    )
    return Update(update_id=message_id, message=msg)


def make_location(chat_id: int, lat: float, lon: float, message_id: int):
    import datetime

    from aiogram.types import Chat, Location, Message, Update, User

    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="Житель"),
        location=Location(latitude=lat, longitude=lon),
    )
    return Update(update_id=message_id, message=msg)


async def test_contact_location() -> None:
    section("18. Номер телефона, геометка, смена заявки")
    import json as _json

    import bot as bot_module
    import classify as classify_module
    import conversation as conversation_module
    import whatsapp_bot as wa
    from aiogram import Bot

    await settle()
    panel = FakePanel()
    client = connect_fake_panel(panel)
    fake = FakeTelegram()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"], session=fake.build_session())
    stub = StubProvider()
    conversation_module.get_provider = lambda name: stub
    classify_module.get_provider = lambda name: stub
    dp, storage = bot_module.dp, bot_module.storage

    async def feed(update):
        await dp.feed_update(bot, update)
        await settle()

    def markups():
        return [c[1].get("reply_markup") for c in fake.calls if c[0] == "SendMessage"]

    chat = 700
    storage.get(chat).clear()
    storage.get(chat).restored = True

    # --- бот просит номер -> кнопка «Отправить номер телефона»
    check("просьба номера распознаётся",
          bot_module.asks_for_phone("Напишите, пожалуйста, ваш номер телефона для связи.")
          and bot_module.asks_for_phone("Байланыш үчүн телефон номериңизди жазыңыз.")
          and not bot_module.asks_for_phone("Звоните в мэрию: 0372 5-00-00."))
    stub.answer = "Чтобы передать обращение, напишите ваш номер телефона."
    fake.clear()
    await feed(make_message("Нет света третий день", chat_id=chat, message_id=40))
    kb = markups()[-1] if markups() else None
    check("TG: под просьбой номера — кнопка запроса контакта",
          bool(kb) and kb.get("keyboard") and kb["keyboard"][0][0].get("request_contact") is True,
          str(kb))

    # --- житель нажал кнопку: номер в профиль, кнопка убирается
    stub.answer = "Спасибо, номер получен. Обращение передано."
    fake.clear(); panel.requests.clear()
    await feed(make_contact(chat, "+996 555 12-34-56", user_id=chat, message_id=41))
    session = storage.get(chat)
    check("TG: свой номер запомнен как подтверждённый",
          session.known.get("phone") == "996555123456"
          and session.known.get("phone_source") == "shared", str(session.known))
    incoming = [body for _, p, body in panel.requests if p.endswith("/tickets/incoming/")]
    check("TG: номер ушёл в профиль жителя в панели",
          bool(incoming) and b"996555123456" in incoming[0], str(incoming)[:200])
    check("TG: модель знает, что номер получен",
          "номер телефона кнопкой" in citizen_part(stub.calls[-1][-1]["content"]))
    kb = markups()[-1] if markups() else None
    check("TG: после номера кнопка убирается", bool(kb) and kb.get("remove_keyboard") is True, str(kb))
    check("TG: модель больше не спрашивает номер",
          "номер телефона известен" in stub.calls[-1][-1]["content"])

    # --- чужой контакт — просто номер из переписки
    other_chat = 701
    storage.get(other_chat).clear()
    storage.get(other_chat).restored = True
    await feed(make_contact(other_chat, "0555 99 88 77", user_id=999, message_id=42))
    check("TG: чужой контакт не считается подтверждённым номером жителя",
          storage.get(other_chat).known.get("phone_source") == "stated",
          str(storage.get(other_chat).known))

    # --- геометка: координаты в карточку, модель не переспрашивает улицу
    panel.requests.clear()
    await feed(make_location(chat, 41.1234567, 72.7654321, message_id=43))
    pins = [b for b in classify_bodies(panel) if "lat" in b]
    check("TG: геометка ушла в карточку координатами",
          bool(pins) and abs(pins[0]["lat"] - 41.1234567) < 1e-9
          and abs(pins[0]["lon"] - 72.7654321) < 1e-9, str(pins))
    check("TG: в переписке карточки — координаты точки",
          any(b"41.123457" in body for _, p, body in panel.requests if p.endswith("/incoming/")))
    check("TG: модель знает, что место отмечено",
          "точку на карте" in stub.calls[-1][-1]["content"])

    # --- панель завела новую заявку: тема и место заново, ФИО и номер остаются
    session.known.update(last_name="Асанов", kind="appeal", settlement="Масы")
    panel.ticket_id = 9
    await feed(make_message("Другое: у школы яма на дороге", chat_id=chat, message_id=44))
    check("TG: новая заявка — бот ведёт её",
          session.ticket_id == 9, str(session.ticket_id))
    check("TG: при новой заявке место забыто, ФИО и номер — нет",
          session.known.get("last_name") == "Асанов" and session.known.get("phone") == "996555123456"
          and session.known.get("settlement") != "Масы", str(session.known))
    check("TG: id сообщения в панели привязан к истории",
          any(m.get("crm_id") == panel.message_id for m in session.history), str(session.history[-2:]))

    # --- WhatsApp: номер известен всегда, геометка из вебхука
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "996700555444", "profile": {"name": "Гулзат"}}],
        "messages": [{"from": "996700555444", "id": "wamid.L", "type": "location",
                      "location": {"latitude": 41.2, "longitude": 72.9,
                                   "name": "Мектеп №3", "address": "Масы айылы"}}]}}]}]}
    parsed = wa.parse_webhook_payload(payload)
    check("WA: геометка разобрана из вебхука",
          parsed and parsed[0]["location"] == (41.2, 72.9)
          and parsed[0]["text"] == "Мектеп №3, Масы айылы", str(parsed))

    sent: list[tuple[str, str]] = []

    async def fake_send(phone, text):
        sent.append((phone, text))
        return True, ""

    wa.send_whatsapp_text = fake_send
    phone_chat = 996700555444
    wa.storage.get(phone_chat).clear()
    wa.storage.get(phone_chat).restored = True
    panel.ticket_id = 1
    panel.requests.clear()
    stub.answer = "Место получили, передаём."
    await wa.handle_incoming_message(parsed[0])
    await settle()
    wa_session = wa.storage.get(phone_chat)
    check("WA: номер жителя известен из канала",
          wa_session.known.get("phone") == "996700555444"
          and wa_session.known.get("phone_source") == "channel", str(wa_session.known))
    check("WA: модель не спрашивает номер",
          "номер телефона известен" in stub.calls[-1][-1]["content"])
    pins = [b for b in classify_bodies(panel) if "lat" in b]
    check("WA: геометка ушла в карточку", bool(pins) and pins[0]["lat"] == 41.2, str(pins))
    check("WA: название места дошло до модели",
          "Мектеп №3" in citizen_part(stub.calls[-1][-1]["content"]))
    check("WA: житель получил ответ", bool(sent) and sent[-1][1] == stub.answer, str(sent))

    await client.close()


async def test_knowledge_module() -> None:
    """19. Справочник организаций (knowledge.py) — проверки живут в своём файле."""
    from selftest_knowledge import test_knowledge
    await test_knowledge(check, section)


async def main() -> int:
    for test in (test_config, test_split, test_icons, test_prompts, test_storage,
                 test_vision_pure, test_whatsapp_pure):
        try:
            test()
        except Exception:
            FAILED.append((test.__name__, traceback.format_exc()))

    for test in (test_dialog, test_providers_offline,
                 test_crm_client, test_dialog_with_panel,
                 test_whatsapp_webhook, test_whatsapp_dialog_with_panel,
                 test_whatsapp_outbox, test_update_ticket,
                 test_transcribe_module, test_voice_telegram,
                 test_voice_whatsapp, test_assistant_pipeline,
                 test_contact_location, test_knowledge_module):
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
