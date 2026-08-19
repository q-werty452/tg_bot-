"""
providers/openai_provider.py — работа с GPT через официальный SDK openai.

Отличия от Anthropic (полезно видеть рядом):
1. Системный промпт — это ПЕРВОЕ сообщение в списке messages
   с ролью "system", отдельного параметра нет.
2. Ответ лежит в response.choices[0].message.content — сразу строкой.
3. Лимит длины передаём как max_completion_tokens (современное имя;
   старое max_tokens считается устаревшим, но часть моделей и прокси
   до сих пор понимают только его — см. фолбэк ниже).
4. temperature не передаём: часть новых «рассуждающих» моделей его не принимает.
   Стиль задаём промптом — так надёжнее.

Файл называется openai_provider.py, а не openai.py, специально:
иначе Python при import openai нашёл бы наш файл вместо библиотеки.
"""

import logging

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

from config import settings
from providers.base import LLMProvider, ProviderError

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"
    title = "GPT (OpenAI)"

    def __init__(self) -> None:
        from providers import override_for
        override = override_for("openai")
        api_key = override.get("key") or settings.openai_api_key
        model = override.get("model") or settings.openai_model
        if not api_key:
            raise ProviderError("Не задан OPENAI_API_KEY в файле .env")
        # timeout — чтобы бот не висел вечно, если сервер не отвечает.
        # max_retries — SDK сам повторит запрос при сетевом сбое.
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=settings.request_timeout,
            max_retries=2,
        )
        self._model = model
        # Некоторые модели/шлюзы принимают только устаревшее имя max_tokens.
        # Определяем это один раз при первой ошибке и дальше не спотыкаемся.
        self._legacy_token_param = False

    async def _create(self, messages: list[dict], max_tokens: int):
        """Один вызов API с учётом того, как эта модель называет лимит токенов."""
        limit_field = "max_tokens" if self._legacy_token_param else "max_completion_tokens"
        return await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            **{limit_field: max_tokens},
        )

    async def ask(
        self,
        system: str,
        history: list[dict],
        max_tokens: int,
        detailed: bool,
    ) -> str:
        # Системный промпт идёт первым сообщением, дальше — вся история.
        messages = [{"role": "system", "content": system}, *history]

        try:
            try:
                response = await self._create(messages, max_tokens)
            except BadRequestError as e:
                # Модель не знает max_completion_tokens — пробуем старое имя.
                text = str(e)
                if not self._legacy_token_param and "max_completion_tokens" in text:
                    logger.info("OpenAI: модель %s требует max_tokens, переключаюсь", self._model)
                    self._legacy_token_param = True
                    response = await self._create(messages, max_tokens)
                else:
                    raise
        except AuthenticationError:
            raise ProviderError(
                "OpenAI: неверный API-ключ (проверь OPENAI_API_KEY).", retryable=True
            )
        except PermissionDeniedError:
            raise ProviderError(
                f"OpenAI: нет доступа к модели {self._model}. "
                "Проверь OPENAI_MODEL в .env и права ключа.",
                retryable=True,
            )
        except RateLimitError:
            raise ProviderError(
                "OpenAI: лимит запросов или закончились средства на балансе.",
                retryable=True,
            )
        except APITimeoutError:
            raise ProviderError(
                "OpenAI: сервер долго не отвечает. Попробуй ещё раз.", retryable=True
            )
        except APIConnectionError:
            raise ProviderError(
                "OpenAI: нет связи с сервером. Проверь интернет/VPN.", retryable=True
            )
        except BadRequestError as e:
            raise ProviderError(f"OpenAI отклонил запрос: {_short(e)}")
        except APIStatusError as e:
            # 5xx — сломался сервер OpenAI, есть смысл переиграть на другой модели.
            raise ProviderError(
                f"OpenAI вернул ошибку {e.status_code}: {_short(e)}",
                retryable=e.status_code >= 500,
            )

        if not response.choices:
            raise ProviderError("GPT вернул пустой ответ. Попробуй переформулировать.")

        choice = response.choices[0]
        text = (choice.message.content or "").strip()

        if not text:
            # finish_reason="length" значит, что лимит токенов кончился раньше,
            # чем модель начала писать ответ — типично для «рассуждающих» моделей.
            if choice.finish_reason == "length":
                raise ProviderError(
                    "GPT не уложился в лимит длины ответа. "
                    "Попробуй задать вопрос короче или переключись в развёрнутый режим."
                )
            if choice.finish_reason == "content_filter":
                raise ProviderError("GPT отказался отвечать: сработал фильтр содержимого.")
            raise ProviderError("GPT вернул пустой ответ. Попробуй переформулировать.")
        return text


def _short(error: Exception, limit: int = 200) -> str:
    """Короткий текст ошибки: полное тело ответа API в чат тащить незачем."""
    message = getattr(error, "message", None) or str(error)
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[:limit] + "…"
