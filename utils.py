"""
utils.py — мелкие вспомогательные функции.

Пока здесь одна задача: Telegram не принимает сообщения длиннее
4096 символов. Развёрнутый ответ модели легко может быть длиннее,
поэтому его нужно аккуратно разрезать на части.
"""

TELEGRAM_LIMIT = 4000  # берём с запасом от реальных 4096


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """
    Режет длинный текст на куски не длиннее `limit`.

    Стараемся резать по границам абзацев, потом по строкам, и только
    в крайнем случае — прямо посреди текста. Так ответ остаётся читаемым.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        # Абзац сам по себе длиннее лимита — режем его по строкам.
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            for line in paragraph.split("\n"):
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                if len(current) + len(line) + 1 > limit:
                    chunks.append(current)
                    current = line
                else:
                    current = f"{current}\n{line}" if current else line
            continue

        if len(current) + len(paragraph) + 2 > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph

    if current:
        chunks.append(current)

    return [c.strip() for c in chunks if c.strip()]
