"""
providers/__init__.py — «фабрика» провайдеров.

Наличие файла __init__.py превращает папку в пакет Python,
и мы можем писать `from providers import get_provider`.

Здесь же лежит кэш созданных клиентов: клиент к API создаём один раз
и переиспользуем, а не на каждое сообщение (иначе тратим время
на установку соединений).

Импорт модулей провайдеров — ЛЕНИВЫЙ (внутри функции, а не сверху файла).
Так неустановленная или сломанная библиотека одного провайдера не мешает
работать остальным: бот на GPT поднимется, даже если SDK Anthropic нет.
"""

import importlib
import logging
import time

from config import settings
from providers.base import LLMProvider, ProviderError

logger = logging.getLogger(__name__)

# Реестр: имя -> (модуль, класс). Добавить нового провайдера = добавить строчку.
_REGISTRY: dict[str, tuple[str, str]] = {
    "openai": ("providers.openai_provider", "OpenAIProvider"),
    "gemini": ("providers.gemini", "GeminiProvider"),
    "claude": ("providers.claude", "ClaudeProvider"),
}

# Кэш уже созданных экземпляров.
_CACHE: dict[str, LLMProvider] = {}

# Ключи и модели, пришедшие из панели управления. Имеют приоритет над .env.
# {"openai": {"key": "...", "model": "..."}, ...}
_OVERRIDES: dict[str, dict] = {}

# На сколько секунд отставляем провайдера, который только что подвёл
# (кончился лимит, отвалилась сеть). Минутные квоты за это время
# восстанавливаются, а бот тем временем отвечает через соседнюю модель.
COOLDOWN = 180

# Имя провайдера -> момент времени, до которого его не трогаем.
_COOLDOWN_UNTIL: dict[str, float] = {}

# Какой ключ из настроек нужен каждому провайдеру.
_KEY_ATTR = {
    "openai": "openai_api_key",
    "gemini": "google_api_key",
    "claude": "anthropic_api_key",
}


def has_key(name: str) -> bool:
    """Есть ли ключ для этого провайдера (панель приоритетнее .env)."""
    if _OVERRIDES.get(name, {}).get("key"):
        return True
    attr = _KEY_ATTR.get(name)
    return bool(attr and getattr(settings, attr, None))


def override_for(name: str) -> dict:
    """Ключ и модель из панели для провайдера (пусто — берём из .env)."""
    return _OVERRIDES.get(name, {})


def apply_overrides(overrides: dict[str, dict]) -> None:
    """
    Применить ключи и модели из панели управления.

    Сравниваем с тем, что уже действует: у кого ключ или модель изменились —
    выбрасываем готовый клиент из кэша, он пересоздастся с новыми данными
    при первом же обращении. Перезапуск бота не нужен.
    """
    global _OVERRIDES
    changed = [name for name in set(_OVERRIDES) | set(overrides)
               if _OVERRIDES.get(name) != overrides.get(name)]
    _OVERRIDES = {k: dict(v) for k, v in overrides.items()}
    for name in changed:
        _CACHE.pop(name, None)
    if changed:
        reset_availability()
        logger.info("Обновлены ключи/модели из панели: %s", ", ".join(sorted(changed)))


def available_providers() -> list[str]:
    """Список провайдеров, для которых реально есть API-ключ (в порядке реестра)."""
    return [name for name in _REGISTRY if has_key(name)]


def mark_unavailable(name: str, seconds: float = COOLDOWN) -> None:
    """Временно отставить провайдера, который подвёл."""
    _COOLDOWN_UNTIL[name] = time.monotonic() + seconds
    logger.warning("Провайдер %s отставлен на %.0f с", name, seconds)


def is_available(name: str) -> bool:
    """Провайдер не в «отставке» прямо сейчас?"""
    until = _COOLDOWN_UNTIL.get(name)
    if until is None:
        return True
    if time.monotonic() >= until:
        del _COOLDOWN_UNTIL[name]  # срок вышел, возвращаем в строй
        return True
    return False


def reset_availability() -> None:
    """Вернуть в строй всех (нужно тестам и ручному сбросу)."""
    _COOLDOWN_UNTIL.clear()


def fallback_chain(preferred: str) -> list[str]:
    """
    Порядок, в котором пробовать провайдеров для одного вопроса.

    Сначала тот, который выбрал пользователь, затем остальные с ключами.
    Провайдеры, недавно подводившие, уходят в конец очереди, но совсем
    не выбрасываются: если сломались все, лучше попробовать хоть кого-то,
    чем ответить «не могу».
    """
    names = available_providers()
    if preferred in names:
        names = [preferred] + [n for n in names if n != preferred]
    healthy = [n for n in names if is_available(n)]
    resting = [n for n in names if not is_available(n)]
    return healthy + resting


def get_provider(name: str) -> LLMProvider:
    """Вернуть готовый объект провайдера по имени."""
    if name not in _REGISTRY:
        raise ProviderError(f"Неизвестный провайдер: {name}")
    if not has_key(name):
        raise ProviderError(
            f"Для этой модели не задан ключ в .env. Доступны: "
            f"{', '.join(available_providers()) or 'ни одной'}."
        )

    if name not in _CACHE:
        module_name, class_name = _REGISTRY[name]
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            # Библиотека не установлена — говорим об этом человеческим языком.
            logger.error("Не удалось импортировать %s: %s", module_name, e)
            raise ProviderError(
                f"Библиотека для провайдера '{name}' не установлена. "
                "Выполни: pip install -r requirements.txt"
            ) from e
        _CACHE[name] = getattr(module, class_name)()  # создаём при первом обращении
    return _CACHE[name]


def warm_up() -> list[str]:
    """
    Создать клиентов всех доступных провайдеров при старте бота.

    Смысл: если ключ битый или библиотеки нет, узнать об этом сразу в логе,
    а не в момент, когда житель напишет первое сообщение.
    Возвращает имена провайдеров, которые поднялись успешно.
    """
    ready = []
    for name in available_providers():
        try:
            get_provider(name)
            ready.append(name)
        except ProviderError as e:
            logger.warning("Провайдер %s недоступен: %s", name, e)
    return ready


__all__ = [
    "LLMProvider",
    "ProviderError",
    "get_provider",
    "available_providers",
    "fallback_chain",
    "has_key",
    "is_available",
    "mark_unavailable",
    "reset_availability",
    "warm_up",
]
