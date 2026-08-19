"""
config.py — все настройки в одном месте.

Зачем отдельный файл: чтобы ключи и названия моделей не были разбросаны
по коду. Код читает настройки только отсюда, а сюда они приходят из файла
.env (через библиотеку python-dotenv).

Правило: секреты (токены, ключи) живут в .env, а не в коде.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Читает файл .env из папки проекта и кладёт его строки в переменные окружения.
# После этого os.getenv("ИМЯ") умеет их доставать.
load_dotenv()

# Порядок важен: если провайдер по умолчанию недоступен, берём первый
# доступный из этого списка.
PROVIDER_ORDER = ("openai", "gemini", "claude")


class ConfigError(RuntimeError):
    """Ошибка настройки — показывается человеку при запуске понятным текстом."""


@dataclass(frozen=True)  # frozen=True => настройки нельзя случайно изменить в рантайме
class Settings:
    telegram_token: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    google_api_key: str | None
    default_provider: str
    anthropic_model: str
    openai_model: str
    google_model: str
    history_limit: int
    request_timeout: float
    log_level: str
    crm_url: str
    crm_bot_token: str


def _env(name: str, default: str = "") -> str:
    """Значение переменной окружения без лишних пробелов и кавычек по краям.

    Кавычки снимаем специально: их часто дописывают руками при заполнении .env
    (OPENAI_API_KEY="sk-..."), и ключ с кавычками молча не работает.
    """
    return os.getenv(name, default).strip().strip('"').strip("'").strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Число из .env с проверкой границ. При мусоре в значении — понятная ошибка."""
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} в .env должно быть числом, а там: {raw!r}")
    return max(minimum, min(maximum, value))


def load_settings() -> Settings:
    """Собирает объект настроек и сразу проверяет самое критичное."""
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError(
            "Не задан TELEGRAM_BOT_TOKEN. Открой файл .env и вставь токен от @BotFather."
        )
    # Токен всегда имеет вид "<цифры>:<буквы и цифры>". Проверяем форму заранее,
    # чтобы не ловить непонятную ошибку сети при старте.
    head, sep, tail = token.partition(":")
    if not (sep and head.isdigit() and len(tail) > 20):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN выглядит неправильно. Ожидается вид 123456789:AAH... — "
            "скопируй токен из @BotFather целиком, без пробелов и кавычек."
        )

    keys = {
        "openai": _env("OPENAI_API_KEY") or None,
        "gemini": _env("GOOGLE_API_KEY") or None,
        "claude": _env("ANTHROPIC_API_KEY") or None,
    }

    # Какие провайдеры реально можно использовать (есть ключ).
    usable = [name for name in PROVIDER_ORDER if keys[name]]
    if not usable:
        raise ConfigError(
            "Не задан ни один ключ ИИ (OPENAI_API_KEY / GOOGLE_API_KEY). "
            "Нужен хотя бы один, иначе боту нечем отвечать. Открой .env и заполни."
        )

    default_provider = _env("DEFAULT_PROVIDER", "openai").lower()
    if default_provider not in usable:
        default_provider = usable[0]

    return Settings(
        telegram_token=token,
        anthropic_api_key=keys["claude"],
        openai_api_key=keys["openai"],
        google_api_key=keys["gemini"],
        default_provider=default_provider,
        anthropic_model=_env("ANTHROPIC_MODEL") or "claude-opus-5",
        openai_model=_env("OPENAI_MODEL") or "gpt-4o",
        google_model=_env("GOOGLE_MODEL") or "gemini-3.6-flash",
        # Нечётный лимит истории обрежется до пары "вопрос-ответ" сам,
        # но снизу держим 2, иначе модель не увидит даже текущий вопрос.
        history_limit=_env_int("HISTORY_LIMIT", 20, minimum=2, maximum=200),
        request_timeout=float(_env_int("REQUEST_TIMEOUT", 90, minimum=10, maximum=600)),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        # Панель управления: пусто — бот работает автономно, без неё.
        crm_url=_env("CRM_URL"),
        crm_bot_token=_env("CRM_BOT_TOKEN"),
    )


# Один общий объект настроек на всё приложение.
# Если .env заполнен неправильно, показываем понятное сообщение и выходим,
# а не пугаем администратора многоэтажным traceback.
try:
    settings = load_settings()
except ConfigError as error:
    raise SystemExit(
        "\n" + "-" * 70 + "\n"
        f"Бот не запущен: {error}\n"
        + "-" * 70
    ) from None
