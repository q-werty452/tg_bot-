"""
evals/live_ky.py — живая проверка понимания кыргызского говора, сленга и
смеси языков на НАСТОЯЩЕЙ модели (платно: ~2-3 цента за прогон).

Запуск:  .venv/bin/python evals/live_ky.py
Каждый вопрос — с ожиданиями: язык ответа, слова, которые должны быть,
и слова, которых быть не должно (выдумки, чужие органы). В конце — сводка.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conversation import ask_assistant, init_knowledge, knowledge_context, with_context  # noqa: E402
from prompts import build_system, reply_language  # noqa: E402
from remote_config import remote  # noqa: E402
from storage import Mode, Session  # noqa: E402

# (вопрос, язык ответа, должно быть хоть одно из, не должно быть)
CASES = [
    ("Губернаторго кирейин дегем", "ky", ["Текебаев", "бейшемби"], ["мэри"]),
    ("губернатор кабыл алабы", "ky", ["бейшемби", "14:00"], []),
    ("облус башчысына кантип жолугам", "ky", ["Текебаев"], []),
    ("полпредке жазылайын десем кайда барам", "ky", ["Абдырахманов", "5-00-01"], []),
    ("жер алайын деп эле", "ky", ["район", "кайсы", "Абдырайым"], ["9:00", "18:00"]),
    ("жер маселеси боюнча кимге барам", "ky", ["Абдырайым", "дүйшөмбү"], []),
    ("пособие алайын деп, кимге кирем", "ky", ["Увайдиллаев", "шаршемби"], []),
    ("арызым карала элек, канча күн күтөм", "ky", ["Базылбеков", "жума"], []),
    ("суу жок 3 кундон бери Масыда", "ky", ["аты", "фамил", "ФИО", "аты-жөн"], []),
    ("жарык жок биздин айылда, электр качан берет", "ky", ["айыл", "кайсы", "район"], []),
    ("акимге арыз жазайын дедим ал мени укпай жатат", "ky", ["Текебаев", "аты"], []),
    ("салам кандайсын", "ky", [], ["Здравствуйте"]),
    ("рахмат чоң", "ky", [], []),
    ("салам, свет жок ужэ два дня", "", ["свет", "жарык", "электр"], []),
    ("кантип справка алам жашаган жеримден", "ky", [], ["МФЦ", "Госуслуг"]),
    ("salamatsyzby, suu jok", "ky", [], []),
    ("ассалому алейкум акаке, мусор чыгарбай жатышат", "ky", ["аты", "кайсы", "ФИО"], []),
    ("привет", "ru", ["Здравствуйте", "Привет", "помочь"], []),
    ("как записатся на прием к губернатору", "ru", ["Текебаев", "четверг"], ["мэри"]),
    ("скока ждать ответ на жалобу", "ru", ["Базылбеков", "пятниц"], []),
    ("где земельный отдел", "ru", ["район", "где вы", "каком"], ["9:00 до 18:00"]),
]


async def main() -> int:
    remote.apply_cached()
    init_knowledge()
    system = build_system(Mode.CHAT, invent=False)
    failed = 0
    for question, lang, need, forbid in CASES:
        session = Session(provider="openai")
        session.add("user", question, 20)
        knowledge = await knowledge_context(session, session.mode)
        history = with_context(session.history, knowledge=knowledge,
                               citizen="номер телефона известен, не спрашивай")
        answer, _ = await ask_assistant("openai", system, history, 700, False)
        low = answer.lower()
        problems = []
        if lang and reply_language(answer) not in (lang, ""):
            problems.append(f"язык ответа {reply_language(answer)}, ждали {lang}")
        if need and not any(n.lower() in low for n in need):
            problems.append(f"нет ничего из {need}")
        bad = [f for f in forbid if f.lower() in low]
        if bad:
            problems.append(f"лишнее: {bad}")
        mark = "ok  " if not problems else "FAIL"
        failed += bool(problems)
        print(f"{mark} {question}\n     → {answer[:240]}")
        for p in problems:
            print(f"     !! {p}")
    print(f"\nИтого: {len(CASES) - failed}/{len(CASES)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
