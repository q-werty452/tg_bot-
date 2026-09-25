"""
providers/gemini.py — работа с Gemini через официальный SDK google-genai.

Особенности Google API (третий вариант того же самого, сравни с соседями):
1. Системный промпт передаётся в объекте config -> system_instruction.
   (У Anthropic это отдельный параметр system, у OpenAI — первое сообщение.)
2. Роли называются "user" и "model". Слова "assistant" тут НЕТ —
   историю приходится конвертировать.
3. Сообщение — это не просто строка, а объект с частями (parts).
   Так сделано, чтобы в одном сообщении могли лежать текст, картинки, файлы.
4. Асинхронный вызов живёт в client.aio — то есть client.aio.models.generate_content().
5. Готовый текст ответа лежит в response.text.
"""

import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import settings
from providers.base import LLMProvider, ProviderError
from utils import read_image

logger = logging.getLogger(__name__)

# Как отключить «размышления» в диалоговом режиме. Название параметра
# менялось между поколениями моделей, поэтому держим оба варианта и
# пробуем их по очереди — какой примет сервер, тот и запомним:
#   Gemini 3.x  -> thinking_level (MINIMAL / LOW / MEDIUM / HIGH)
#   Gemini 2.5  -> thinking_budget (число токенов, 0 = выключить)
# Третий вариант (None) — модель вообще не умеет управлять размышлениями.
_THINKING_VARIANTS = ("level", "budget", None)


class GeminiProvider(LLMProvider):
    name = "gemini"
    title = "Gemini (Google)"

    def __init__(self) -> None:
        from providers import override_for
        override = override_for("gemini")
        api_key = override.get("key") or settings.google_api_key
        model = override.get("model") or settings.google_model
        if not api_key:
            raise ProviderError("Не задан GOOGLE_API_KEY в файле .env")
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                # timeout здесь в МИЛЛИсекундах, в отличие от других SDK.
                timeout=int(settings.request_timeout * 1000),
                # Временные сбои Google (перегрузка модели, короткий всплеск
                # лимита) SDK переиграет сам, с нарастающей паузой. До нашего
                # переключения на другую модель дело дойдёт, только если
                # и повторы не помогли.
                retry_options=types.HttpRetryOptions(
                    attempts=3,
                    initial_delay=1.0,
                    max_delay=8.0,
                    http_status_codes=[429, 500, 502, 503, 504],
                ),
            ),
        )
        self._model = model
        # Какой из вариантов отключения размышлений понимает эта модель.
        # Выясняем при первом запросе и дальше используем его.
        self._thinking_variant: str | None = _THINKING_VARIANTS[0]
        self._thinking_checked = False

    @staticmethod
    def _to_gemini_history(history: list[dict]) -> list[types.Content]:
        """
        Переводит нашу общую историю в формат Google.

        Было:  {"role": "assistant", "content": "текст"}
        Стало: Content(role="model", parts=[Part(text="текст")])

        Сообщение с фото (item["images"]) получает дополнительный
        Part.from_bytes на каждую картинку — так Gemini реально видит,
        что на фото, а не только читает текстовую пометку о нём.

        Именно поэтому мы храним историю в нейтральном формате (см. storage.py):
        каждый провайдер конвертирует её под себя, и переключение модели
        посреди диалога ничего не ломает.
        """
        contents = []
        for item in history:
            parts: list[types.Part] = []
            if item["content"]:
                parts.append(types.Part(text=item["content"]))
            for path in item.get("images") or []:
                encoded = read_image(path)
                if encoded is None:
                    continue
                mime, data = encoded
                parts.append(types.Part.from_bytes(data=data, mime_type=mime))
            if not parts:
                parts = [types.Part(text="")]
            contents.append(types.Content(
                role="model" if item["role"] == "assistant" else "user",
                parts=parts,
            ))
        return contents

    def _build_config(
        self, system: str, max_tokens: int, variant: str | None
    ) -> types.GenerateContentConfig:
        """Собрать config запроса под выбранный способ отключения размышлений."""
        thinking = None
        if variant == "level":
            thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
        elif variant == "budget":
            thinking = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(
            system_instruction=system,   # <- системный промпт живёт здесь
            max_output_tokens=max_tokens,
            thinking_config=thinking,
            # Инструментов мы модели не даём, а SDK по умолчанию готовится
            # их вызывать и пишет об этом предупреждение в лог при каждом
            # запросе. Отключаем, чтобы не засорять bot.log.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def ask(
        self,
        system: str,
        history: list[dict],
        max_tokens: int,
        detailed: bool,
    ) -> str:
        contents = self._to_gemini_history(history)

        # В развёрнутом режиме thinking_config не задаём вообще —
        # модель сама решит, сколько думать (это её поведение по умолчанию).
        # В диалоговом режиме размышления только мешают: они замедляют ответ
        # и съедают лимит max_output_tokens, которого там всего 500.
        if detailed:
            variants: tuple[str | None, ...] = (None,)
        elif self._thinking_checked:
            variants = (self._thinking_variant,)
        else:
            variants = _THINKING_VARIANTS

        last_error: Exception | None = None
        response = None

        for variant in variants:
            config = self._build_config(system, max_tokens, variant)
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model, contents=contents, config=config
                )
            except genai_errors.ClientError as e:
                # Часть моделей Google (обычно preview) умеет отвечать только
                # на одиночный вопрос и падает с 400 на переписке. Житель не
                # должен из-за этого остаться без ответа: повторяем запрос
                # с одним последним сообщением.
                if (e.code == 400 and len(contents) > 1
                        and "multiturn" in str(getattr(e, "message", e)).lower()):
                    logger.warning(
                        "Модель %s не ведёт диалог — отвечаю без истории. "
                        "Смените модель в панели.", self._model)
                    try:
                        response = await self._client.aio.models.generate_content(
                            model=self._model, contents=contents[-1:], config=config)
                    except genai_errors.APIError as retry_error:
                        raise self._to_provider_error(retry_error)
                    if not detailed:
                        self._thinking_variant = variant
                        self._thinking_checked = True
                    break
                # 400 на способе отключения размышлений — пробуем следующий.
                if e.code == 400 and not detailed and variant is not None:
                    logger.info(
                        "Gemini: модель %s не приняла thinking_%s, пробую следующий вариант",
                        self._model,
                        variant,
                    )
                    last_error = e
                    continue
                raise self._to_provider_error(e)
            except genai_errors.ServerError as e:
                raise ProviderError(
                    f"Gemini: сервер недоступен ({e.code}). Попробуй позже.", retryable=True
                )
            except genai_errors.APIError as e:
                raise ProviderError(f"Gemini: ошибка API — {_short(e)}", retryable=True)

            if not detailed:
                # Запомнили рабочий вариант — больше не перебираем.
                self._thinking_variant = variant
                self._thinking_checked = True
            break

        if response is None:
            raise self._to_provider_error(last_error)

        text = (response.text or "").strip()
        if not text:
            raise ProviderError(self._explain_empty(response))
        return text

    @staticmethod
    def _to_provider_error(error: Exception | None) -> ProviderError:
        """Перевод ошибки Google в понятный пользователю текст."""
        if error is None:
            return ProviderError("Gemini: запрос не удался. Попробуй ещё раз.", retryable=True)
        code = getattr(error, "code", None)
        if code in (401, 403):
            return ProviderError(
                "Gemini: неверный API-ключ (проверь GOOGLE_API_KEY).", retryable=True
            )
        if code == 404:
            return ProviderError(
                f"Gemini: модель {settings.google_model} не найдена. "
                "Проверь GOOGLE_MODEL в .env.",
                retryable=True,
            )
        if code == 429:
            return ProviderError(
                "Gemini: превышен лимит запросов, подожди минуту.", retryable=True
            )
        return ProviderError(f"Gemini вернул ошибку {code}: {_short(error)}")

    @staticmethod
    def _explain_empty(response) -> str:
        """
        Пустой ответ у Gemini значит одно из трёх: сработал фильтр безопасности,
        ответ упёрся в лимит токенов, либо заблокирован сам запрос.
        Смотрим служебные поля, чтобы сказать пользователю что-то осмысленное.
        """
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            return "Gemini не стал отвечать на этот запрос. Попробуй переформулировать."

        candidates = getattr(response, "candidates", None) or []
        reason = str(getattr(candidates[0], "finish_reason", "")) if candidates else ""
        if "MAX_TOKENS" in reason:
            return (
                "Gemini не уложился в лимит длины ответа. "
                "Задай вопрос конкретнее или переключись в развёрнутый режим."
            )
        if "SAFETY" in reason or "PROHIBITED" in reason or "RECITATION" in reason:
            return "Gemini отказался отвечать: сработал фильтр безопасности."
        return "Gemini вернул пустой ответ. Попробуй переформулировать."


def _short(error: Exception, limit: int = 200) -> str:
    """Короткий текст ошибки: полное тело ответа API в чат тащить незачем."""
    message = getattr(error, "message", None) or str(error)
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[:limit] + "…"
