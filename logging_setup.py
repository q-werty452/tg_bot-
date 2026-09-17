"""
logging_setup.py — общая настройка логирования для bot.py и whatsapp_bot.py.

Вынесено из bot.py в отдельный модуль, чтобы у каждого процесса был свой
файл лога: если оба процесса писали бы в один и тот же bot.log,
ротация и запись строк начали бы конфликтовать друг с другом.
"""

import logging
import logging.handlers
import sys

from config import settings


def setup_logging(log_file: str = "bot.log") -> None:
    """Логи одновременно в консоль и в файл (с ротацией)."""
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    # maxBytes/backupCount — чтобы файл логов не съел диск за полгода работы.
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(settings.log_level if settings.log_level in
                  ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO")
    root.handlers = [console, file_handler]

    # aiohttp на уровне DEBUG заваливает лог служебными запросами.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
