"""
utils.py — мелкие вспомогательные функции.

Две задачи:
1. Telegram не принимает сообщения длиннее 4096 символов. Развёрнутый
   ответ модели легко может быть длиннее, поэтому его нужно аккуратно
   разрезать на части.
2. Прочитать с диска фото, присланное жителем, и подготовить его для
   показа модели (все три активных провайдера хотят разные форматы,
   но сырые байты + mime-тип им нужны одинаковые — отсюда read_image()).
"""

import logging
import mimetypes
from pathlib import Path

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000  # берём с запасом от реальных 4096

# Изображения крупнее этого модели не показываем (только в карточку заявки).
# Смысл ограничения — не раздувать запрос: телефонное фото такого размера
# уже давно избыточно для того, чтобы модель разобрала, что на нём.
VISION_IMAGE_LIMIT = 8 * 1024 * 1024


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


def read_image(path: str) -> tuple[str, bytes] | None:
    """
    Прочитать изображение с диска для отправки модели.

    Возвращает (mime, байты) или None, если файла нет, он не похож на
    изображение по расширению или слишком велик — в этих случаях фото
    остаётся видно только в карточке заявки, а модель получает как раньше
    текстовую пометку о том, что жителем приложено фото.
    """
    file = Path(path)
    mime = mimetypes.guess_type(file.name)[0] or ""
    if not mime.startswith("image/"):
        return None
    try:
        data = file.read_bytes()
    except OSError:
        logger.warning("Не удалось прочитать изображение %s для модели", path)
        return None
    if len(data) > VISION_IMAGE_LIMIT:
        logger.info("Изображение %s больше %d МБ — модели не показываем",
                    path, VISION_IMAGE_LIMIT // (1024 * 1024))
        return None
    return mime, data
