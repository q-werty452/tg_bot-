"""
transcribe.py — расшифровка голосовых сообщений жителей (речь -> текст).

Нужен обоим каналам: bot.py (Telegram) и whatsapp_bot.py (WhatsApp) скачивают
звук, отдают его сюда и дальше работают с полученным текстом как с обычным
сообщением жителя.

Почему отдельная модель, а не «послушать» сам чат-моделью: основной ИИ бота
может быть любым (GPT, Gemini, Claude), а надёжная расшифровка речи у нас
одна — OpenAI (по умолчанию gpt-4o-transcribe). Ключ берётся тот же, что у
GPT: из панели управления, а если там пусто — из .env.

Язык НЕ фиксируется: жители говорят по-русски и по-кыргызски, нередко
вперемешку и со сленгом. Вместо этого модели даётся короткая подсказка со
словарём — названиями районов области и местными словами, чтобы имена
собственные писались правильно (Жалал-Абад, а не «Джелалабад»).
"""

import asyncio
import logging
import os
import re
from pathlib import Path

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

import providers
from config import _env, settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-transcribe"

# OpenAI не принимает файлы тяжелее 25 МБ — проверяем заранее, не тратя трафик.
MAX_FILE_BYTES = 25 * 1024 * 1024
# Дольше этого не расшифровываем: платим за минуту записи, а обращение
# жителя редко требует больше десяти минут речи. Сотрудник всё равно
# получит саму запись в карточке.
MAX_DURATION_SECONDS = 10 * 60
# Ждать ответа дольше минуты нет смысла: житель ждёт, а SDK ещё и повторит запрос.
TIMEOUT_SECONDS = 60.0
# WhatsApp длительность не сообщает. Его голосовые — opus на 16-24 кбит/с,
# так что длительность хорошо оценивается по размеру файла. Берём 32 кбит/с
# (4 КБ в секунду) с запасом: оценка выходит заниженной, и запись короче
# лимита по ошибке не отклонится. Без этой оценки час речи в WhatsApp
# (около 10 МБ) ушёл бы на платную расшифровку целиком.
OPUS_BYTES_PER_SECOND = 4000

# Подсказка для модели: словарь, а не инструкция. Модели транскрипции
# «подхватывают» написание слов из prompt — так имена собственные
# не искажаются.
PROMPT = (
    "Обращение жителя Жалал-Абадской области, речь на русском и кыргызском "
    "языках, возможно вперемешку. Населённые пункты: Жалал-Абад, Манас, "
    "Ноокен, Сузак, Базар-Коргон, Аксы, Ала-Бука, Токтогул, Тогуз-Торо, "
    "Чаткал, Кара-Көл, Майлуу-Суу, Таш-Көмүр, Кочкор-Ата, Кербен, Масы, "
    "Казарман. Слова: айыл өкмөтү, акимиат, мэрия, полпред, ЦОН, "
    "Социальный фонд."
)

# Тексты для жителя (двуязычные, без технических деталей) лежат здесь,
# чтобы Telegram и WhatsApp говорили одно и то же.
UNRECOGNIZED_REPLY = (
    "Не удалось разобрать голосовое сообщение. Пожалуйста, повторите или "
    "напишите текстом.\n"
    "Үн билдирүүнү түшүнө алган жокмун. Кайра жибериңиз же жазып жибериңиз."
)
TOO_LONG_REPLY = (
    "Голосовое сообщение слишком длинное. Пожалуйста, сократите его "
    "(до 10 минут) или напишите текстом.\n"
    "Үн билдирүү өтө узун. Кыскартып кайра жибериңиз (10 мүнөткө чейин) "
    "же жазып жибериңиз."
)
# Что записываем в карточку, когда речь разобрать не удалось: сотрудник
# видит, что к обращению приложена запись, которую нужно прослушать.
UNRECOGNIZED_CARD_TEXT = "[Голосовое, не распознано]"


class TranscribeError(Exception):
    """
    Не получилось расшифровать запись.

    message — понятное объяснение причины (для лога и для администратора).
    public_text — готовый текст для ЖИТЕЛЯ, если у этой ошибки он особый
    (например, «запись слишком длинная»). Если пусто, вызывающий показывает
    общий UNRECOGNIZED_REPLY: про ключи и баланс жителю знать незачем.
    """

    def __init__(self, message: str, public_text: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.public_text = public_text


# Какое расширение дать файлу при отправке в OpenAI. Принимаются только
# ogg/mp3/mp4/m4a/wav/webm/mpga/flac, и определяется формат по имени файла,
# поэтому «audio/ogg; codecs=opus» из WhatsApp нужно превратить в «.ogg».
_MIME_TO_EXT = {
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mpga": ".mpga",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "video/mp4": ".mp4",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/webm": ".webm",
    "video/webm": ".webm",
}
_ALLOWED_EXT = {".ogg", ".mp3", ".mp4", ".m4a", ".wav", ".webm", ".mpga", ".flac"}
# Расширения, которые OpenAI не знает, хотя формат тот же.
_EXT_ALIASES = {".oga": ".ogg", ".opus": ".ogg", ".mpeg": ".mp3"}


def ext_for_mime(mime: str | None) -> str:
    """Расширение файла по mime-типу ('' если тип неизвестен). Параметры
    вроде '; codecs=opus' отбрасываются."""
    base = (mime or "").split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(base, "")


def upload_extension(path: str, mime: str | None = None) -> str:
    """Расширение для отправки в OpenAI: сначала по mime (ему верим больше,
    чем имени файла), затем по суффиксу самого файла. '' — формат не поддерживается."""
    ext = ext_for_mime(mime)
    if ext:
        return ext
    suffix = Path(path).suffix.lower()
    suffix = _EXT_ALIASES.get(suffix, suffix)
    return suffix if suffix in _ALLOWED_EXT else ""


# Один клиент на ключ: ключ может смениться из панели без перезапуска бота,
# а пересоздавать клиента на каждое сообщение незачем.
_client_cache: dict[str, AsyncOpenAI] = {}


def _get_client() -> AsyncOpenAI:
    key = providers.override_for("openai").get("key") or settings.openai_api_key
    if not key:
        raise TranscribeError(
            "Не задан ключ OpenAI (ни в панели, ни в OPENAI_API_KEY) — "
            "расшифровка голосовых недоступна."
        )
    client = _client_cache.get(key)
    if client is None:
        _client_cache.clear()  # старый ключ уже не нужен
        client = AsyncOpenAI(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=1)
        _client_cache[key] = client
    return client


def _norm(text: str) -> str:
    """Слова в нижнем регистре через пробел — для сравнения без учёта знаков."""
    return " ".join(re.findall(r"\w+", text.lower()))


def _is_prompt_echo(text: str) -> bool:
    """
    Модели транскрипции на тишине и шуме иногда выдают в ответ кусок
    подсказки. Это не речь жителя — считаем запись пустой, иначе в карточку
    и в ИИ пойдёт список районов вместо обращения.

    Короткие совпадения («Манас», «ЦОН») не трогаем: так мог сказать и сам
    человек; отсекаем только длинные куски подсказки.
    """
    norm = _norm(text)
    return len(norm) >= 25 and norm in _norm(PROMPT)


async def transcribe(path: str, *, duration: int | float | None = None,
                     mime: str | None = None) -> str:
    """
    Расшифровать аудиофайл и вернуть текст.

    duration — длительность в секундах, если канал её сообщает (Telegram да,
    WhatsApp нет). mime — тип файла от канала; по нему подбирается расширение.
    Пустая строка — речь не распознана (тишина, шум): вызывающий решает,
    что сказать жителю. Всё остальное, что пошло не так, — TranscribeError.
    """
    if duration and duration > MAX_DURATION_SECONDS:
        raise TranscribeError(
            f"Запись длиннее {MAX_DURATION_SECONDS // 60} минут ({duration} с) — "
            "не расшифровываем.",
            public_text=TOO_LONG_REPLY,
        )

    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise TranscribeError(f"Не удалось открыть файл записи: {e}") from e
    if size > MAX_FILE_BYTES:
        raise TranscribeError(
            f"Файл записи {size / 1024 / 1024:.1f} МБ — больше лимита OpenAI "
            f"({MAX_FILE_BYTES // 1024 // 1024} МБ).",
            public_text=TOO_LONG_REPLY,
        )

    ext = upload_extension(path, mime)
    if not ext:
        raise TranscribeError(
            f"Неподдерживаемый формат записи (mime={mime!r}, файл {Path(path).name!r})."
        )
    if not duration and ext == ".ogg" and size / OPUS_BYTES_PER_SECOND > MAX_DURATION_SECONDS:
        raise TranscribeError(
            f"Запись около {size // OPUS_BYTES_PER_SECOND // 60} минут или дольше "
            f"(оценка по размеру {size / 1024 / 1024:.1f} МБ) — не расшифровываем.",
            public_text=TOO_LONG_REPLY,
        )

    client = _get_client()
    model = _env("TRANSCRIBE_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL
    data = await asyncio.to_thread(Path(path).read_bytes)

    try:
        # Кортеж (имя, байты): имя с правильным расширением — по нему OpenAI
        # понимает формат, а настоящее имя на диске для этого не годится.
        response = await client.audio.transcriptions.create(
            model=model,
            file=(f"audio{ext}", data),
            prompt=PROMPT,
        )
    except AuthenticationError as e:
        raise TranscribeError("OpenAI: неверный API-ключ для расшифровки голоса.") from e
    except PermissionDeniedError as e:
        raise TranscribeError(
            f"OpenAI: нет доступа к модели расшифровки {model} "
            "(проверь TRANSCRIBE_MODEL и права ключа)."
        ) from e
    except RateLimitError as e:
        raise TranscribeError(
            "OpenAI: лимит запросов или закончились средства на балансе."
        ) from e
    except APITimeoutError as e:  # раньше APIConnectionError: это его подкласс
        raise TranscribeError("OpenAI: расшифровка заняла слишком много времени.") from e
    except APIConnectionError as e:
        raise TranscribeError("OpenAI: нет связи с сервером. Проверь интернет/VPN.") from e
    except BadRequestError as e:
        raise TranscribeError(
            f"OpenAI отклонил запись (формат {ext}, повреждённый файл?): "
            f"{_short(e)}"
        ) from e
    except APIStatusError as e:
        raise TranscribeError(
            f"OpenAI вернул ошибку {e.status_code} при расшифровке: {_short(e)}"
        ) from e

    raw = getattr(response, "text", response)
    text = raw.strip() if isinstance(raw, str) else ""
    if not text or _is_prompt_echo(text):
        return ""
    return text


def _short(error: Exception, limit: int = 200) -> str:
    """Короткий текст ошибки: полное тело ответа API тащить в лог незачем."""
    message = getattr(error, "message", None) or str(error)
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[:limit] + "…"
