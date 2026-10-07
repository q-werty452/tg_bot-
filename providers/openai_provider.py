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

import base64
import logging
import re

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
from utils import read_image

logger = logging.getLogger(__name__)


def _to_message(item: dict) -> dict:
    """
    Перевести одно сообщение нашей истории в формат OpenAI.

    Без фото — как раньше, content простой строкой. С фото — content
    становится списком блоков (текст + image_url с картинкой как data:
    URL): так GPT/OpenRouter-модели с поддержкой vision реально видят,
    что на фото, а не только читают текстовую пометку о нём.
    """
    images = item.get("images")
    if not images:
        return {"role": item["role"], "content": item["content"]}

    parts: list[dict] = []
    if item["content"]:
        parts.append({"type": "text", "text": item["content"]})
    for path in images:
        encoded = read_image(path)
        if encoded is None:
            continue
        mime, data = encoded
        b64 = base64.b64encode(data).decode("ascii")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    if not parts:
        parts = [{"type": "text", "text": item["content"] or ""}]
    return {"role": item["role"], "content": parts}


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
        # «Рассуждающие» модели (gpt-5*) тратят часть max_completion_tokens
        # на невидимые рассуждения ДО того, как начнут писать ответ. При
        # небольшом лимите (диалоговый режим) рассуждения могут съесть весь
        # лимит, и модель вернёт пустой ответ (finish_reason="length").
        # reasoning_effort="minimal" сводит эти траты почти к нулю — нам
        # для короткого чат-ответа глубокие рассуждения и не нужны.
        self._reasoning_model = model.startswith("gpt-5")

    async def _create(self, messages: list[dict], max_tokens: int):
        """Один вызов API с учётом того, как эта модель называет лимит токенов."""
        limit_field = "max_tokens" if self._legacy_token_param else "max_completion_tokens"
        extra = {"reasoning_effort": "minimal"} if self._reasoning_model else {}
        return await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            **{limit_field: max_tokens},
            **extra,
        )

    async def ask(
        self,
        system: str,
        history: list[dict],
        max_tokens: int,
        detailed: bool,
    ) -> str:
        # Системный промпт идёт первым сообщением, дальше — вся история.
        messages = [{"role": "system", "content": system}, *(_to_message(h) for h in history)]

        try:
            try:
                response = await self._create(messages, max_tokens)
            except BadRequestError as e:
                text = str(e)
                if not self._legacy_token_param and "max_completion_tokens" in text:
                    # Модель не знает max_completion_tokens — пробуем старое имя.
                    logger.info("OpenAI: модель %s требует max_tokens, переключаюсь", self._model)
                    self._legacy_token_param = True
                    response = await self._create(messages, max_tokens)
                elif self._reasoning_model and "reasoning_effort" in text:
                    # Модель не принимает reasoning_effort — не рассуждающая,
                    # несмотря на имя gpt-5*. Отключаем и повторяем без него.
                    logger.info("OpenAI: модель %s не знает reasoning_effort, отключаю", self._model)
                    self._reasoning_model = False
                    response = await self._create(messages, max_tokens)
                else:
                    raise
        except _API_ERRORS as e:
            raise _to_provider_error(e, self._model) from None

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

    async def search_web(self, system: str, history: list[dict], max_tokens: int) -> str:
        """
        Ответ с поиском в интернете (Responses API, инструмент web_search).

        Зовётся только когда модель сама сказала, что в справочнике ответа
        нет (см. prompts.parse_search_request) — поиск платный, на каждое
        сообщение его не тратим. Фото в поиск не передаём: искать по ним
        нечего, а токены они съедают.
        """
        messages = [{"role": h["role"], "content": h["content"]}
                    for h in history if h.get("content")]
        tool = {
            "type": "web_search",
            # Без этого поиск тянет ответы про Россию и Казахстан.
            "user_location": {"type": "approximate", "country": "KG"},
            # «low» — меньше страниц в контексте модели: дешевле,
            # а для адреса или графика работы этого хватает.
            "search_context_size": "low",
        }
        # Минимальная длина «рассуждений», которую принимает поиск
        # (с «minimal» модели gpt-5 поиск не включают).
        extra = {"reasoning": {"effort": "low"}} if self._reasoning_model else {}

        async def create(tool: dict, extra: dict):
            return await self._client.responses.create(
                model=self._model,
                instructions=system,
                input=messages,
                tools=[tool],
                max_output_tokens=max_tokens,
                **extra,
            )

        try:
            try:
                response = await create(tool, extra)
            except BadRequestError as e:
                # Не все модели принимают уточнения к поиску (страна, объём
                # контекста, длину рассуждений). Один раз пробуем голый поиск.
                logger.info("OpenAI: поиск с уточнениями отклонён (%s), пробую без них",
                            _short(e))
                response = await create({"type": "web_search"}, {})
        except _API_ERRORS as e:
            raise _to_provider_error(e, self._model) from None

        text = plain_links((getattr(response, "output_text", "") or "").strip())
        if not text:
            raise ProviderError("Поиск не дал ответа. Попробуй переформулировать.")
        return text


# Ссылки в ответе с поиском приходят Markdown-разметкой: «([сайт](адрес))».
# Мессенджер покажет её как есть, со скобками, поэтому переводим в обычный
# текст: «сайт (адрес)» или просто «адрес», если подпись и есть адрес.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_UTM_RE = re.compile(r"([?&])utm_source=openai(&?)")


def _drop_utm(url: str) -> str:
    """Убрать метку utm_source=openai, не ломая остальные параметры адреса."""
    def repl(m: re.Match) -> str:
        return m.group(1) if m.group(2) else ""
    return _UTM_RE.sub(repl, url)


def plain_links(text: str) -> str:
    """Markdown-ссылки и выделение -> обычный текст для мессенджера."""
    def link(m: re.Match) -> str:
        label, url = m.group(1).strip(), _drop_utm(m.group(2))
        bare = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
        if label.lower().removeprefix("www.") in bare.lower():
            return url
        return f"{label} ({url})"

    text = _MD_LINK_RE.sub(link, text)
    text = re.sub(r"\((https?://[^\s)]+)\)", lambda m: f"({_drop_utm(m.group(1))})", text)
    # Жирный/курсив Markdown и заголовки «## ...» — тоже просто текстом.
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text

# Ошибки SDK, которые переводим в понятную человеку ProviderError.
_API_ERRORS = (AuthenticationError, PermissionDeniedError, RateLimitError,
               APITimeoutError, APIConnectionError, BadRequestError, APIStatusError)


def _to_provider_error(e: Exception, model: str) -> ProviderError:
    """Ошибка SDK -> ProviderError с текстом для чата и признаком «переиграть»."""
    if isinstance(e, AuthenticationError):
        return ProviderError(
            "OpenAI: неверный API-ключ (проверь OPENAI_API_KEY).", retryable=True)
    if isinstance(e, PermissionDeniedError):
        return ProviderError(
            f"OpenAI: нет доступа к модели {model}. "
            "Проверь OPENAI_MODEL в .env и права ключа.",
            retryable=True,
        )
    if isinstance(e, RateLimitError):
        return ProviderError(
            "OpenAI: лимит запросов или закончились средства на балансе.",
            retryable=True,
        )
    if isinstance(e, APITimeoutError):
        return ProviderError(
            "OpenAI: сервер долго не отвечает. Попробуй ещё раз.", retryable=True)
    if isinstance(e, APIConnectionError):
        return ProviderError(
            "OpenAI: нет связи с сервером. Проверь интернет/VPN.", retryable=True)
    if isinstance(e, BadRequestError):
        return ProviderError(f"OpenAI отклонил запрос: {_short(e)}")
    # 5xx — сломался сервер OpenAI, есть смысл переиграть на другой модели.
    return ProviderError(
        f"OpenAI вернул ошибку {e.status_code}: {_short(e)}",
        retryable=e.status_code >= 500,
    )


def _short(error: Exception, limit: int = 200) -> str:
    """Короткий текст ошибки: полное тело ответа API в чат тащить незачем."""
    message = getattr(error, "message", None) or str(error)
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[:limit] + "…"
