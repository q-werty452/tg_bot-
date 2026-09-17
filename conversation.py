"""
conversation.py — общая для Telegram и WhatsApp логика запроса к ИИ и
восстановления истории после перезапуска.

Вынесено из bot.py: это единственные два куска логики обработки сообщения,
которые не зависят ни от aiogram, ни от какого-либо другого транспорта —
whatsapp_bot.py использует их без изменений. Остальная оркестрация
(respond(), запись в панель, доставка ответа, обработка ошибок конкретного
транспорта) намеренно НЕ обобщается и живёт отдельно в каждом боте — см.
комментарий в whatsapp_bot.py о том, почему это осознанный выбор.
"""

import logging

from providers import (
    ProviderError,
    fallback_chain,
    get_provider,
    mark_unavailable,
)

logger = logging.getLogger(__name__)


async def ask_with_fallback(
    preferred: str, system: str, history: list[dict], max_tokens: int, detailed: bool
) -> tuple[str, str]:
    """
    Спросить модель, а если она подвела — незаметно переспросить у соседней.

    Зачем: у бесплатных тарифов есть лимит запросов. Упереться в него посреди
    показа или рабочего дня нельзя, поэтому при ошибке самого провайдера
    (лимит, сеть, битый ключ) бот молча берёт следующую доступную модель.
    Человек видит просто ответ, а в лог пишется, кто именно ответил.

    Ошибки другого рода (сработал фильтр, ответ не влез в лимит длины)
    не переигрываем: соседняя модель ответит так же (см. ProviderError.retryable).

    Возвращает пару: текст ответа и имя модели, которая ответила.
    """
    chain = fallback_chain(preferred)
    if not chain:
        raise ProviderError("Не настроена ни одна модель. Проверь ключи в .env")

    last_error: ProviderError | None = None

    for name in chain:
        try:
            provider = get_provider(name)
        except ProviderError as e:
            # Нет ключа или не установлена библиотека — просто идём дальше.
            last_error = e
            continue

        try:
            answer = await provider.ask(
                system=system, history=history, max_tokens=max_tokens, detailed=detailed
            )
        except ProviderError as e:
            if not e.retryable:
                raise  # виноват вопрос, а не провайдер — переспрашивать незачем
            last_error = e
            mark_unavailable(name)  # отставим на пару минут, чтобы не спотыкаться
            logger.warning("Провайдер %s подвёл (%s), пробую следующего", name, e)
            continue

        if name != preferred:
            logger.info("Ответ получен через запасную модель %s (вместо %s)", name, preferred)
        return answer, name

    # Все модели по очереди отказали.
    raise last_error or ProviderError("Ни одна модель не ответила. Попробуй позже.")


async def restore_history(session, chat_id: int, crm, history_limit: int,
                          channel: str = "telegram") -> None:
    """Один раз за жизнь сессии подтянуть контекст диалога из панели."""
    if session.restored or session.history or not crm.enabled:
        session.restored = True
        return
    session.restored = True
    context = await crm.context(chat_id, channel=channel)
    if not context:
        return
    if context.get("is_open"):
        session.ticket_id = context.get("ticket_id")
    for item in context.get("messages") or []:
        role = "user" if item.get("author") == "citizen" else "assistant"
        text = (item.get("text") or "").strip()
        if text:
            session.add(role, text, history_limit)
    if session.history:
        logger.info("Чат %s: восстановлено %s сообщений из панели",
                    chat_id, len(session.history))
