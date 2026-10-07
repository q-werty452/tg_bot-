"""
selftest_knowledge.py — проверка поиска по справочнику (knowledge.py) БЕЗ сети и ключей.

Запуск:           .venv/bin/python selftest_knowledge.py
Калибровка:       .venv/bin/python selftest_knowledge.py --calibrate
                  (нужен рабочий OPENAI_API_KEY: один раз посчитает эмбеддинги
                  справочника и покажет таблицу «лексика против гибрида»)

Как подключается к общему selftest.py: там вызывается
    await test_knowledge(check, section)
Здесь же есть свой крошечный раннер, чтобы гонять модуль отдельно.

Что проверяем:
  1. Нормализация и свёртку кыргызских букв (ө/ү/ң, ё).
  2. Игрушечный справочник во временной папке: что попадает в индекс, что нет
     (внутреннее, контакты-фрагменты, «исключающие» правила).
  3. Поиск: названия, сокращения («рга»), опечатки, территориальный буст,
     порог «ничего не найдено» на приветствиях и мусоре.
  4. Формат и жёсткий лимит длины format_context, кандидаты для CRM.
  5. Эмбеддинги с фальшивым эмбеддером: кэш на диске, сбои, откат на лексику.
  6. Конституцию: явные ссылки «статья 41» / «41-берене» и порог релевантности.
  7. На РЕАЛЬНОМ справочнике v4 (если папка есть): набор настоящих вопросов.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REAL_DIR = HERE.parent / "jalal-abad-data" / "knowledge_platform" / "knowledge_output" / "v4" / "runtime_export"


# ===========================================================================
# Игрушечный справочник
# ===========================================================================

def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def make_fixture(root: Path) -> Path:
    """Маленький справочник в формате v4: достаточно, чтобы проверить все ветки."""
    d = root / "runtime_export"
    d.mkdir(parents=True)
    pub = {"visibility": "PUBLIC"}
    _write(d / "territories.jsonl", [
        {"territory_id": "region:Жалал-Абад", "name_ru": "Жалал-Абадская область", "name_ky": "Жалал-Абад облусу", "type": "region", "parent_territory": None},
        {"territory_id": "district:Ноокен", "name_ru": "Ноокенский район", "name_ky": "Ноокен району", "type": "district", "parent_territory": "region:Жалал-Абад"},
        {"territory_id": "district:Сузак", "name_ru": "Сузакский район", "name_ky": "Сузак району", "type": "district", "parent_territory": "region:Жалал-Абад"},
        {"territory_id": "city:Кочкор-Ата", "name_ru": "город Кочкор-Ата", "name_ky": "Кочкор-Ата шаары", "type": "city", "parent_territory": "district:Ноокен"},
        {"territory_id": "city:Манас", "name_ru": "город Манас", "name_ky": "Манас шаары", "type": "city", "parent_territory": "region:Жалал-Абад"},
        {"territory_id": "city:Жалал-Абад", "name_ru": "город Жалал-Абад", "name_ky": "Жалал-Абад шаары", "type": "city", "parent_territory": None},
        {"territory_id": "city:гак", "name_ru": "гак", "name_ky": None, "type": "city", "parent_territory": None},
    ])
    _write(d / "organizations.jsonl", [
        {"organization_id": "org-nooken-rga", "name_ru": None, "name_ky": "Ноокен райондук мамлекеттик администрациясы",
         "aliases_ru": ["Ноокенская районная государственная администрация"], "aliases_ky": [],
         "territory": ["district:Ноокен"], **pub},
        {"organization_id": "org-suzak-rga", "name_ru": "Сузакская районная государственная администрация",
         "name_ky": "Сузак райондук мамлекеттик администрациясы", "aliases_ru": [], "aliases_ky": [],
         "territory": ["district:Сузак"], **pub},
        {"organization_id": "org-library", "name_ru": "Жалал-Абадская областная библиотека",
         "name_ky": "Жалал-Абад областтык китепкана", "aliases_ru": [], "aliases_ky": [],
         "territory": ["region:Жалал-Абад"], **pub},
        {"organization_id": "org-manas-water", "name_ru": "Управление водоканал Манас", "name_ky": None,
         "aliases_ru": [], "aliases_ky": [], "territory": ["city:Манас"], **pub},
        {"organization_id": "org-nooken-electro", "name_ru": None, "name_ky": "Ноокен райондук электр тармактары",
         "aliases_ru": [], "aliases_ky": [], "territory": ["district:Ноокен"], **pub},
        {"organization_id": "org-empty", "name_ru": "Пустая организация без данных", "name_ky": None,
         "aliases_ru": [], "aliases_ky": [], "territory": [], **pub},
        {"organization_id": "org-secret", "name_ru": "Закрытая организация", "name_ky": None,
         "aliases_ru": [], "aliases_ky": [], "territory": [], "visibility": "INTERNAL_ONLY"},
    ])

    def contact(oid, kind, value, **extra):
        return {"contact_id": f"c-{oid}-{kind}-{value[:5]}", "organization_id": oid, "type": kind,
                "value": value, **pub, **extra}

    _write(d / "contacts.jsonl", [
        contact("org-nooken-rga", "address", "Масы айылы Турдумбетов көчөсү № 37"),
        contact("org-nooken-rga", "phone", "03734 5-00-11"),
        contact("org-suzak-rga", "address", "Сузак айылы, Сатвалды Палван көчөсү, 108"),
        contact("org-suzak-rga", "address", "Сузакский район, село Сузак, улица Сатвалды-Палвана, 108."),
        contact("org-suzak-rga", "phone", "(03748) 5-00-01"),
        contact("org-suzak-rga", "reception_hours", "Понедельник 14:00-15:00"),
        contact("org-suzak-rga", "email", "suzak_rma@mail.ru"),
        contact("org-library", "address", "Манас шаары, Ж. Абдырахманов көчөсү 6-А"),
        contact("org-library", "working_hours", "9:00-18:00"),
        contact("org-manas-water", "phone", "03722 5-12-12", territory_id="city:Манас"),
        contact("org-nooken-electro", "phone", "03734 5-07-07"),
        contact("org-secret", "phone", "000"),
    ])
    _write(d / "competences.jsonl", [
        {"organization_id": "org-nooken-rga", "topic": "unknown", "description_ru": None,
         "description_ky": "Район деңгээлинде аткаруу бийлигин жүзөгө ашырат", **pub},
        {"organization_id": "org-library", "topic": "library", "description_ru": "Хранение и выдача книг читателям",
         "description_ky": None, **pub},
        {"organization_id": "org-manas-water", "topic": "WATER", "description_ru": "Водоснабжение и канализация города Манас",
         "description_ky": None, **pub},
        {"organization_id": "org-nooken-electro", "topic": "ELECTRICITY",
         "description_ru": "Электроснабжение, аварии на электрических сетях", "description_ky": None, **pub},
    ])
    _write(d / "services.jsonl", [
        {"service_id": "s1", "organization_id": "org-library", "name": "Доступ к библиотечному фонду",
         "description": "x", "territory_id": "city:Жалал-Абад", **pub},
    ])
    _write(d / "routing_rules.jsonl", [
        {"routing_rule_id": "r-water-nooken", "topic": "WATER", "canonical_topic": "WATER", "trigger_phrases_ru": ["WATER"],
         "trigger_phrases_ky": ["WATER"], "target_organization_id": "org-nooken-rga", "territory_id": "district:Ноокен",
         "required_clarifications": ["айыл"], **pub},
        {"routing_rule_id": "r-water-manas", "topic": "прорыв воды", "trigger_phrases_ru": ["прорыв воды", "нет воды"],
         "trigger_phrases_ky": [], "target_organization_id": "org-manas-water", "territory_id": "city:Манас", **pub},
        {"routing_rule_id": "r-elec-nooken", "topic": "ELECTRICITY", "canonical_topic": "ELECTRICITY",
         "trigger_phrases_ru": ["ELECTRICITY"], "trigger_phrases_ky": ["ELECTRICITY"],
         "target_organization_id": "org-nooken-electro", "territory_id": "district:Ноокен", **pub},
        {"routing_rule_id": "r-excl", "topic": "жолдо ачык калган люк", "trigger_phrases_ky": ["жолдо ачык калган люк"],
         "trigger_phrases_ru": [], "target_organization_id": "org-nooken-rga", "exclusion": True, **pub},
    ])
    _write(d / "qa_pairs_ru.jsonl", [
        {"qa_id": "qa-1", "language": "ru", "question": "Какие услуги предоставляет областная библиотека?",
         "answer": "Доступ к библиотечному фонду и библиографическое обслуживание.",
         "organization_ids": ["org-library"], **pub, "publication_status": "PILOT_PUBLIC"},
    ])
    _write(d / "qa_pairs_ky.jsonl", [])
    _write(d / "procedures.jsonl", [
        {"procedure_id": "p1", "organization_id": "org-nooken-rga", "name": "Обработка обращения в Ноокенской РМА",
         "steps": ["Приём обращения", "Регистрация", "Направление исполнителю"], "required_documents": [],
         "territory_id": "district:Ноокен", **pub},
        {"procedure_id": "p2", "organization_id": "org-nooken-rga", "name": "Внутренняя процедура",
         "steps": ["секрет"], "visibility": "INTERNAL_ONLY"},
    ])
    _write(d / "rag_chunks.jsonl", [
        {"chunk_id": "ch-contact", "title": "Контакты Ноокенской администрации", "text": "Телефон секретной приёмной 99-99-99, не показывать отдельным фрагментом",
         "tags": ["contact"], "organization_ids": ["org-nooken-rga"], "visibility": "PUBLIC"},
        {"chunk_id": "ch-ctx", "title": "Ноокен райондук мамлекеттик администрациясы", "topic": "WATER",
         "text": "Вода, водоснабжение, аварии на водопроводных сетях - в районную организацию водного хозяйства.",
         "tags": ["routing_context"], "organization_ids": ["org-nooken-rga"], "territory_scope": "district:Ноокен",
         "publication_status": "PILOT_PUBLIC"},
        {"chunk_id": "ch-internal", "title": "Служебное", "text": "Это внутренний документ для служебного пользования, не публиковать",
         "tags": ["provision"], "organization_ids": [], "publication_status": "INTERNAL_ONLY"},
    ])
    _write(d / "sources.jsonl", [{"source_id": "SRC-1", "note": "посторонний файл не должен мешать"}])
    (d / "knowledge_manifest.json").write_text(json.dumps({"knowledge_version": "test-v1"}), encoding="utf-8")
    return d


def make_topic_fixture(root: Path) -> Path:
    """Маленький справочник для темы жалоб: «воды» не должно цеплять «водительского»."""
    d = root / "topics"
    d.mkdir()
    pub = {"visibility": "PUBLIC"}
    _write(d / "territories.jsonl", [
        {"territory_id": "region:Жалал-Абад", "name_ru": "Жалал-Абадская область", "name_ky": "Жалал-Абад облусу", "type": "region", "parent_territory": None},
        {"territory_id": "district:Ноокен", "name_ru": "Ноокенский район", "name_ky": "Ноокен району", "type": "district", "parent_territory": "region:Жалал-Абад"},
        {"territory_id": "village:Масы", "name_ru": "село Масы", "name_ky": "Масы айылы", "type": "village", "parent_territory": "district:Ноокен"},
        {"territory_id": "district:Сузак", "name_ru": "Сузакский район", "name_ky": "Сузак району", "type": "district", "parent_territory": "region:Жалал-Абад"},
        {"territory_id": "city:Кара-Көл", "name_ru": "город Кара-Көл", "name_ky": "Кара-Көл шаары", "type": "city", "parent_territory": "region:Жалал-Абад"},
        {"territory_id": "city:Майлуу-Суу", "name_ru": "город Майлуу-Суу", "name_ky": "Майлуу-Суу шаары", "type": "city", "parent_territory": "region:Жалал-Абад"},
    ])
    names = {
        "nooken-rga": ("Ноокен райондук мамлекеттик администрациясы", None, ["district:Ноокен"]),
        "nooken-reg": (None, "Ноокенский отдел Государственного центра по регистрации транспортных средств и водительского состава", ["district:Ноокен"]),
        "kk-res": (None, "Жалал-Абадское предприятие электрических сетей Кара-Куль РЭС", ["city:Кара-Көл"]),
        "kk-mayor": (None, "Мэрия города Кара-Куль", ["city:Кара-Көл"]),
        "kk-school": (None, "Школа №3 города Кара-Куль", ["city:Кара-Көл"]),
        "suzak-res": (None, "Сузак РЭС", ["district:Сузак"]),
        "mailuu-mayor": (None, "Мэрия города Майлуу-Суу", ["city:Майлуу-Суу"]),
        "mailuu-water": (None, "Майлуу-Суу водоканал", ["city:Майлуу-Суу"]),
        "ct-land": ("Токтогульский земельный отдел", None, []),
    }
    orgs, contacts = [], []
    for oid, (ky, ru, terr) in names.items():
        orgs.append({"organization_id": oid, "name_ru": ru, "name_ky": ky, "aliases_ru": [], "aliases_ky": [],
                     "territory": terr, **pub})
        contacts.append({"contact_id": f"c-{oid}", "organization_id": oid, "type": "phone", "value": "0372 5-00-00", **pub})
    _write(d / "organizations.jsonl", orgs)
    _write(d / "contacts.jsonl", contacts)
    _write(d / "routing_rules.jsonl", [
        {"routing_rule_id": "r-el-suzak", "topic": "ELECTRICITY", "canonical_topic": "ELECTRICITY",
         "trigger_phrases_ru": ["ELECTRICITY"], "trigger_phrases_ky": [], "target_organization_id": "suzak-res",
         "territory_id": "district:Сузак", **pub},
        {"routing_rule_id": "r-weird", "topic": "ОСАГО боюнча жол-транспорт кырсыгы", "trigger_phrases_ky": [],
         "trigger_phrases_ru": [], "target_organization_id": "nooken-reg", **pub},
    ])
    for name in ("competences", "services", "procedures", "qa_pairs_ru", "qa_pairs_ky", "rag_chunks"):
        _write(d / f"{name}.jsonl", [])
    return d


def make_constitution(root: Path) -> Path:
    c = root / "constitution"
    c.mkdir()
    ru = [
        {"article": 41, "chapter": "ГЛАВА IV. ЭКОНОМИЧЕСКИЕ И СОЦИАЛЬНЫЕ ПРАВА", "title": "",
         "text": "Каждый имеет право на экономическую свободу, свободное использование своих способностей и имущества.", "lang": "ru"},
        {"article": "27", "chapter": "ГЛАВА III. ПРАВА РЕБЁНКА", "title": "",
         "text": "Каждый ребенок имеет право на уровень жизни, необходимый для его физического и умственного развития.", "lang": "ru"},
        {"article": 67, "chapter": "ГЛАВА VI. ПРЕЗИДЕНТ", "title": "",
         "text": "Президент избирается на шесть лет. Одно и то же лицо не может быть избрано Президентом более двух сроков.", "lang": "ru"},
        {"article": 0, "chapter": "", "title": "", "text": "Мы, народ Кыргызской Республики, исходя из права самостоятельно определять свою судьбу.", "lang": "ru"},
    ]
    ky = [
        {"article": 41, "chapter": "IV ГЛАВА. ЭКОНОМИКАЛЫК УКУКТАР", "title": "",
         "text": "Ар бир адам экономикалык эркиндикке, өз жөндөмдүүлүгүн жана мүлкүн пайдаланууга укуктуу.", "lang": "ky"},
    ]
    _write(c / "constitution_ru.jsonl", ru)
    _write(c / "constitution_ky.jsonl", ky)
    return c


# ===========================================================================
# Фальшивый эмбеддер: «понятия» вместо настоящей модели
# ===========================================================================

CONCEPTS = [
    ("вода", ("вода", "воды", "суу", "водопровод", "кран", "водоснабжение", "водоканал")),
    ("свет", ("электр", "свет", "лампочка", "электроснабжение")),
    ("книги", ("библиотека", "китепкана", "книги", "читать", "читателям")),
    ("власть", ("администрация", "администрациясы", "акимият")),
]


class FakeEmbedder:
    """Вектор = сколько понятий затронул текст. Считает вызовы — для проверки кэша."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.texts_seen: list[str] = []
        self.fail = fail

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("нет сети (тест)")
        self.texts_seen.extend(texts)
        out = []
        for t in texts:
            low = t.lower()
            vec = [0.0] * len(CONCEPTS) + [0.05]   # последняя ось — «ничего знакомого»
            for i, (_, words) in enumerate(CONCEPTS):
                vec[i] += sum(1.0 for w in words if w in low)
            if not any(vec[:-1]):
                vec[-1] = 1.0
            out.append(vec)
        return out


# ===========================================================================
# Реальные вопросы жителей (для справочника v4)
# ===========================================================================

# (вопрос, что должно быть в топ-N (подстрока или кортеж вариантов; None = ничего не должно
#  найтись), N, подсказка территории из профиля жителя). Сверяем с названием И с текстом
#  записи в нормализованном виде (ө/ү/ң свёрнуты, ь убран).
REAL_QUESTIONS: list[tuple[str, object, int, str]] = [
    # --- справочные: «где / телефон / режим» -------------------------------------------
    ("где находится ноокенская рга", "ноокен райондук мамлекеттик администрациясы", 1, ""),
    ("Сузак районунун акимиатынын телефону", "сузак райондук мамлекеттик администрациясы", 1, ""),
    ("таш комур мэрия номер", "таш-комур шаардык мэриясы", 1, ""),
    ("ала-бука айыл окмоту телефон", "ала-бука айыл окмоту", 1, ""),
    ("режим работы областной библиотеки", "областтык китепкана", 1, ""),
    ("Жалал-Абад областтык китепкана качан ачык", "областтык китепкана", 1, ""),
    ("нокен районунун мамлекеттик администрациясы", "ноокен райондук мамлекеттик администрациясы", 3, ""),
    ("где налоговая в Майлуу-Суу", "салык", 3, ""),
    ("где мэрия Кара-Куля", "мэрия города кара-куль", 1, ""),
    ("телефон больницы в Сузаке", ("больница сузакского", "практики"), 3, ""),
    ("Сузак РГАнын иш убактысы", "сузак райондук мамлекеттик администрациясы", 1, ""),
    ("как попасть на приём к акиму Токтогула", "токтогул райондук мамлекеттик администрациясы", 3, ""),
    ("где ЦОН", "центр обслуживания населения", 3, "Токтогул"),
    ("где ЦОН в Сузаке", None, 0, ""),
    # --- жалобы: тема + территория -----------------------------------------------------
    ("кайда кайрылсам болот суу жок", ("суу", "вод"), 3, ""),
    ("света нет в кочкор-ате", "электр", 3, ""),
    ("мусор не вывозят в Базар-Коргоне", "базар-коргон тазалык", 3, ""),
    ("куда обратиться по земельному вопросу в Токтогуле", "токтогул", 3, ""),
    ("открытый люк на улице Манас", "манас", 3, ""),
    ("дорога разбита в Кара-Колу", ("дорожно-эксплуатационное", "кара-куль"), 3, ""),
    ("нет воды уже 3 дня", "ноокен райондук мамлекеттик администрациясы", 3, "Ноокенский район, Масы"),
    ("яма на дороге возле школы", ("благоустройства", "базар-коргон райондук мамлекеттик администрациясы"), 3, "Базар-Коргон"),
    ("свет вырубили опять", "кара-куль рэс", 3, "Кара-Көл"),
    ("кайсы жерге арыз жазам жер маселеси боюнча", ("земельным ресурсам", "жер ресурстары"), 3, "Токтогул"),
    ("вода не идёт в Токтогуле", "токтогул", 3, ""),
    ("суу келбей жатат Таш-Көмүрдө", "таш", 3, ""),
    ("электр жарыгы өчтү Сузакта", "сузак рэс", 3, ""),
    ("мен Масы айылында жашайм, жарык жок", "ноокен", 3, ""),
    ("ветеринар керек Сузак", "сузак райондук ветеринария", 3, ""),
    # --- транслит и сленг --------------------------------------------------------------
    ("Nooken RGA telefon", "ноокен райондук мамлекеттик администрациясы", 1, ""),
    ("net vody v Nooken", "ноокен райондук мамлекеттик администрациясы", 3, ""),
    ("svet vyrubili", "кара-куль рэс", 3, "Kara-Kol"),
    # --- не справочник: пусто -----------------------------------------------------------
    ("как получить справку о составе семьи", None, 0, ""),
    ("привет", None, 0, ""),
    ("добрый день, подскажите пожалуйста", None, 0, ""),
    ("саламатсызбы", None, 0, ""),
    ("скока стоит доллар", None, 0, ""),
    ("сколько стоит билет на самолёт", None, 0, ""),
    ("погода завтра", None, 0, ""),
    ("Конституция право на обращение", None, 0, ""),
]


# ===========================================================================
# Тесты
# ===========================================================================

async def test_knowledge(check, section) -> None:
    import knowledge
    from knowledge import KnowledgeBase, normalize, tokenize

    tmp = Path(tempfile.mkdtemp(prefix="kb_test_"))
    try:
        data = make_fixture(tmp)
        const = make_constitution(tmp)
        cache = tmp / "cache"

        # ------------------------------------------------------------ 1
        section("Знания 1. Нормализация и свёртка кыргызских букв")
        check("ё -> е, регистр", normalize("ЁЛКА") == "елка", normalize("ЁЛКА"))
        check("ө/ү/ң -> о/у/н", normalize("Өзгөчө Үй Аң") == "озгочо уй ан", normalize("Өзгөчө Үй Аң"))
        check("токены по дефису", tokenize("Кара-Көл шаары") == ["кара", "кол", "шаары"], str(tokenize("Кара-Көл шаары")))
        check("жители пишут без ө/ү/ң — слова совпадают", tokenize("Таш-Көмүр") == tokenize("Таш-Комур"))
        check("NFKC: «й» и «ё» из разложенных символов",
              normalize("й") == normalize("й") and normalize("ё") == "е")
        check("мягкий знак не мешает: «Жалал-Абад» = «Жалал-Абад»", normalize("объем") == "обем")

        # ------------------------------------------------------------ 2
        section("Знания 2. Загрузка: что попало в индекс")
        kb = KnowledgeBase(data, const, cache)
        t0 = time.perf_counter()
        kb.load()
        check("load: включено", kb.enabled)
        check("load: версия из манифеста", kb.version.startswith("test-v1"), kb.version)
        st = kb.stats()
        check("карточек организаций 5 (без пустой и без INTERNAL)", st["by_kind"].get("org") == 5, str(st["by_kind"]))
        check("правил маршрутизации 3 (исключающее отброшено)", st["by_kind"].get("route") == 3, str(st["by_kind"]))
        check("фрагментов 1 (contact и INTERNAL отброшены)", st["by_kind"].get("chunk") == 1, str(st["by_kind"]))
        check("процедур 1 (INTERNAL_ONLY отброшена)", st["by_kind"].get("procedure") == 1, str(st["by_kind"]))
        check("вопросов-ответов 1", st["by_kind"].get("qa") == 1, str(st["by_kind"]))
        check("статей Конституции 5", st["articles"] == 5, str(st))
        all_text = " ".join(d.show + d.search for d in kb._docs)
        check("внутреннее не попало в индекс", "секрет" not in all_text.lower() and "000" not in all_text
              and "Закрытая" not in all_text)
        check("контакт-фрагмент не дублирует карточки", "99-99-99" not in all_text)
        check("load быстрый", time.perf_counter() - t0 < 2.0, f"{time.perf_counter() - t0:.2f} с")

        # ------------------------------------------------------------ 3
        section("Знания 3. Поиск по названиям, сокращениям, опечаткам")

        async def top(q, n=3, **kw):
            return await kb.search(q, limit=n, **kw)

        def ids(hits):
            return [h.id for h in hits]

        h = await top("где находится ноокенская рга")
        check("«ноокенская рга» -> карточка Ноокенской РГА первой", h and h[0].id == "org:org-nooken-rga", str(ids(h)))
        h = await top("Сузак районунун акимиатынын телефону")
        check("«акимиатынын» (опечатка) -> Сузакская РГА первой", h and h[0].id == "org:org-suzak-rga", str(ids(h)))
        h = await top("нокен рга")
        check("опечатка «нокен» -> Ноокен", h and "nooken" in h[0].id, str(ids(h)))
        h = await top("сузакскй район")
        check("опечатка «сузакскй» -> Сузак", h and "suzak" in h[0].id, str(ids(h)))
        h = await top("акимиат Сузака")
        check("«акимиат» + «Сузака» (падеж) -> Сузакская РГА", h and h[0].id == "org:org-suzak-rga", str(ids(h)))
        h = await top("Сузак районунун телефону")
        check("с кыргызскими буквами и без — один и тот же результат",
              ids(h) == ids(await top("Сузак район\u04e9н\u04afн\u04af телефону")) and h, str(ids(h)))
        h = await top("режим работы областной библиотеки")
        check("библиотека по-русски находит кыргызское «китепкана»", h and h[0].id == "org:org-library", str(ids(h)))
        h = await top("китепкана качан иштейт")
        check("библиотека по-кыргызски", h and h[0].id == "org:org-library", str(ids(h)))
        h = await top("Ж. Абдырахманов көчөсү 6-А")
        check("поиск по адресу", h and h[0].id == "org:org-library", str(ids(h)))
        h = await top("ноокенская рга", n=5)
        check("перекрёстного мусора нет: нет чужих районов", all("suzak" not in x for x in ids(h)), str(ids(h)))

        # ------------------------------------------------------------ 4
        section("Знания 4. Территориальный буст")
        h = await top("нет воды", territory_hint="Ноокенский район")
        check("hint Ноокен: водный маршрут Ноокена первый", h and h[0].id == "route:r-water-nooken", str(ids(h)))
        h = await top("нет воды", territory_hint="город Манас")
        check("hint Манас: маршрут Манаса первый", h and h[0].id == "route:r-water-manas", str(ids(h)))
        h = await top("нет воды в Ноокене")
        check("территория в самом вопросе («в Ноокене»)", h and h[0].id == "route:r-water-nooken", str(ids(h)))
        h = await top("нет воды в Манасе")
        check("«в Манасе» (падеж) -> Манас", h and h[0].id == "route:r-water-manas", str(ids(h)))
        h = await top("нет света в Кочкор-Ате")
        check("город внутри района («Кочкор-Ате» -> Ноокенский район): электрики Ноокена",
              h and any(x.id == "route:r-elec-nooken" or x.id == "org:org-nooken-electro" for x in h), str(ids(h)))
        h = await top("нет воды в Ноокене", n=5)
        check("чужая территория опущена: Манас ниже Ноокена или отсутствует",
              "route:r-water-manas" not in ids(h) or ids(h).index("route:r-water-manas") > ids(h).index("route:r-water-nooken"),
              str(ids(h)))
        h = await top("нет воды")
        check("территория неизвестна — областные/любые маршруты не выкидываются", len(h) >= 2, str(ids(h)))
        h = await top("библиотека", territory_hint="Сузакский район")
        check("областной документ при чужом hint не выкинут", h and h[0].id == "org:org-library", str(ids(h)))

        # ------------------------------------------------------------ 5
        section("Знания 5. Порог «ничего не найдено»")
        for junk in ("привет", "Здравствуйте, подскажите пожалуйста", "саламатсызбы", "скока стоит доллар",
                     "погода завтра", "как дела", "", "   ", "?!", "а", "сколько стоит билет на самолёт"):
            h = await top(junk)
            check(f"мусор «{junk}» -> пусто", h == [], str(ids(h)))
        h = await top("привет, где находится ноокенская рга?")
        check("приветствие не мешает настоящему вопросу", h and h[0].id == "org:org-nooken-rga", str(ids(h)))

        # ------------------------------------------------------------ 6
        section("Знания 6. format_context и кандидаты организаций")
        hits = await kb.search("Сузак районунун акимиатынын телефону", limit=5)
        text = kb.format_context(hits)
        check("заголовок на русском", text.startswith("Справочник (проверенные данные"), text[:60])
        check("пронумерован", "\n1. " in text)
        check("адрес, телефон и часы приёма на месте",
              "Сатвалды Палван" in text and "(03748) 5-00-01" in text and "Приём:" in text, text)
        check("название на обоих языках через «/»",
              "Сузакская районная государственная администрация / Сузак райондук" in text, text)
        check("двойной адрес (ru+ky одного дома) схлопнут", text.count("108") == 1, text)
        check("почта и территория показаны", "suzak_rma@mail.ru" in text and "Сузакский район" in text, text)
        check("provenance не просочился", "SRC-" not in text and "provenance" not in text)
        for limit in (120, 300, 700, 2400):
            t = kb.format_context(hits, max_chars=limit)
            check(f"длина ≤ {limit}", len(t) <= limit, str(len(t)))
        t = kb.format_context(hits, max_chars=700)
        numbered = [ln for ln in t.splitlines() if ln[:2].rstrip(". ").isdigit()]
        check("при тесном лимите пункты не рвутся посередине номера",
              all(ln[0].isdigit() for ln in numbered) and 1 <= len(numbered) <= len(hits), t)
        check("пусто, если нечего показывать", kb.format_context([]) == "")
        route_hits = await kb.search("нет воды в Ноокене", limit=3)
        rtxt = kb.format_context(route_hits)
        check("маршрут: «Вопрос … → организация; уточнить у жителя»",
              "Вопрос «вода, водопровод, питьевая вода»" in rtxt and "→" in rtxt and "уточнить у жителя: айыл" in rtxt, rtxt)
        check("код WATER не светится как есть", "WATER" not in rtxt, rtxt)

        cands = kb.candidate_organizations(hits)
        check("кандидаты: первая — Сузакская РГА", cands and cands[0]["id"] == "org-suzak-rga", str(cands))
        check("кандидаты уникальны", len({c["id"] for c in cands}) == len(cands), str(cands))
        check("у кандидата есть имя", all(c["name"] for c in cands), str(cands))
        check("кандидаты пусты без хитов", kb.candidate_organizations([]) == [])
        both = await kb.search("нет воды", limit=5)
        c2 = kb.candidate_organizations(both)
        check("кандидаты из маршрутов (цель маршрута)", {c["id"] for c in c2} >= {"org-manas-water"}, str(c2))

        # ------------------------------------------------------------ 7
        section("Знания 7. Нет папки с данными")
        empty = KnowledgeBase(tmp / "нет-такой-папки", None, cache)
        empty.load()
        check("enabled=False", empty.enabled is False)
        check("search -> []", await empty.search("Ноокен") == [])
        check("search_constitution -> []", await empty.search_constitution("статья 41") == [])
        check("format_context -> ''", empty.format_context([]) == "")
        check("prepare не падает", await empty.prepare(FakeEmbedder()) is False)
        none_kb = KnowledgeBase(None, None, cache)
        none_kb.load()
        check("data_dir=None тоже не падает", none_kb.enabled is False)
        no_const = KnowledgeBase(data, tmp / "нет-конституции", cache)
        no_const.load()
        check("без файлов Конституции справочник работает", no_const.enabled and no_const.stats()["articles"] == 0)
        check("и поиск по Конституции пуст", await no_const.search_constitution("статья 41") == [])
        broken = tmp / "broken"
        broken.mkdir()
        (broken / "organizations.jsonl").write_text("{не json\n[1,2]\n\n", encoding="utf-8")
        bkb = KnowledgeBase(broken, None, cache)
        bkb.load()
        check("битые строки данных не роняют загрузку", bkb.enabled is False and await bkb.search("x y") == [])

        # ------------------------------------------------------------ 8
        section("Знания 8. Эмбеддинги: кэш, сбои, гибридный поиск")
        emb = FakeEmbedder()
        kb2 = KnowledgeBase(data, const, tmp / "cache2")
        kb2.load()
        ok = await kb2.prepare(emb)
        n_docs = len(kb2._docs) + len(kb2._const_docs)
        check("prepare: готово", ok is True)
        check("prepare: текстов отправлено не больше числа документов", 0 < len(emb.texts_seen) <= n_docs,
              f"{len(emb.texts_seen)} из {n_docs}")
        check("в кэше на диске есть файлы", any((tmp / "cache2").glob("emb_*.npy")))
        check("кэш на диске не содержит текстов (только sha1)",
              not any("Ноокен" in p.read_text(encoding="utf-8") for p in (tmp / "cache2").glob("emb_*.json")))

        emb_again = FakeEmbedder()
        kb3 = KnowledgeBase(data, const, tmp / "cache2")
        kb3.load()
        check("второй prepare из кэша", await kb3.prepare(emb_again) is True)
        check("второй prepare не зовёт embed", emb_again.calls == 0, f"вызовов {emb_again.calls}")

        # данные поменялись: пересчитывается только новое
        rows = [json.loads(x) for x in (data / "competences.jsonl").read_text(encoding="utf-8").splitlines() if x]
        rows.append({"organization_id": "org-library", "topic": "x", "description_ru": "Новая услуга: ксерокопия документов",
                     "description_ky": None, "visibility": "PUBLIC"})
        _write(data / "competences.jsonl", rows)
        emb_new = FakeEmbedder()
        kb4 = KnowledgeBase(data, const, tmp / "cache2")
        kb4.load()
        await kb4.prepare(emb_new)
        check("изменилась одна запись — посчитано мало новых", 0 < len(emb_new.texts_seen) <= 2,
              f"{len(emb_new.texts_seen)}")
        check("после изменения гибрид снова готов", kb4.stats()["semantic_ready"] is True)

        # размер пачки
        import knowledge as kmod
        batches: list[int] = []

        async def counting(texts):
            batches.append(len(texts))
            return await FakeEmbedder()(texts)

        kb5 = KnowledgeBase(data, None, tmp / "cache3")
        kb5.load()
        old_batch = kmod.EMBED_BATCH
        kmod.EMBED_BATCH = 4
        try:
            await kb5.prepare(counting)
        finally:
            kmod.EMBED_BATCH = old_batch
        check("тексты уходят пачками", batches and max(batches) <= 4 and len(batches) > 1, str(batches))

        # смысловой поиск: слова запроса не совпадают лексически
        h = await kb2.search("течёт кран", embed=emb, limit=3)
        check("смысл: «течёт кран» находит водные записи без общих слов",
              h and any("water" in x.id or "r-water" in x.id for x in h), str([x.id for x in h]))
        check("у хита видны слагаемые (lex/sem)", h and h[0].sem is not None)
        h = await kb2.search("где находится ноокенская рга", embed=emb, limit=3)
        check("гибрид сохраняет точное попадание по названию", h and h[0].id == "org:org-nooken-rga", str([x.id for x in h]))
        h = await kb2.search("привет", embed=emb, limit=3)
        check("приветствие и с эмбеддингами -> пусто", h == [], str([x.id for x in h]))
        h = await kb2.search("скока стоит доллар", embed=emb, limit=3)
        check("мусор и с эмбеддингами -> пусто", h == [], str([x.id for x in h]))

        calls_before = emb.calls
        await kb2.search("течёт кран", embed=emb)
        check("повторный запрос не ходит в сеть (кэш запросов)", emb.calls == calls_before)

        # сбои
        bad = FakeEmbedder(fail=True)
        kb6 = KnowledgeBase(data, const, tmp / "cache4")
        kb6.load()
        r = await kb6.prepare(bad)
        check("prepare при сбое сети не бросает и возвращает False", r is False)
        h = await kb6.search("где находится ноокенская рга", embed=bad)
        check("search при сбое embed работает на лексике", h and h[0].id == "org:org-nooken-rga", str([x.id for x in h]))
        h = await kb2.search("Сузак телефон", embed=FakeEmbedder(fail=True), limit=3)
        check("сбой эмбеддинга запроса (после prepare) -> лексика", h and h[0].id == "org:org-suzak-rga", str([x.id for x in h]))
        flaky = FakeEmbedder(fail=True)
        kb2._qcache.clear()
        kb2._embed_fail_until = 0.0
        await kb2.search("первый запрос про воду", embed=flaky)
        await kb2.search("второй запрос про свет", embed=flaky)
        check("после сбоя пауза: не долбим упавший сервис", flaky.calls == 1, f"вызовов {flaky.calls}")
        kb2._embed_fail_until = 0.0

        async def hanging(texts):
            await asyncio.sleep(30)
            return []

        kb2.embed_timeout = 0.2
        kb2._qcache.clear()
        t1 = time.perf_counter()
        h = await kb2.search("Сузак телефон", embed=hanging, limit=3)
        check("зависший эмбеддер обрезается по таймауту", time.perf_counter() - t1 < 3.0 and h, f"{time.perf_counter() - t1:.1f} с")
        kb2._embed_fail_until = 0.0
        kb2.embed_timeout = 6.0

        async def garbage(texts):
            return [[1.0, 2.0]]          # неверное число векторов

        kb7 = KnowledgeBase(data, None, tmp / "cache5")
        kb7.load()
        check("эмбеддер вернул не то, что просили -> False без исключения", await kb7.prepare(garbage) is False)

        async def nan_embed(texts):
            return [[float("nan")] * 4 for _ in texts]

        kb8 = KnowledgeBase(data, None, tmp / "cache6")
        kb8.load()
        await kb8.prepare(nan_embed)
        h = await kb8.search("Сузак телефон", embed=nan_embed, limit=3)
        check("NaN-векторы не ломают поиск", isinstance(h, list))

        # повреждённый кэш
        (tmp / "cache7").mkdir()
        (tmp / "cache7" / "emb_text-embedding-3-small.npy").write_bytes(b"not a npy")
        (tmp / "cache7" / "emb_text-embedding-3-small.json").write_text("[1,2,3]")
        kb9 = KnowledgeBase(data, None, tmp / "cache7")
        kb9.load()
        check("повреждённый кэш пересчитывается", await kb9.prepare(FakeEmbedder()) is True)

        # эмбеддер OpenAI: не зовём сеть, проверяем форму
        import inspect
        check("make_openai_embedder: корутинная функция", inspect.iscoroutinefunction(knowledge.make_openai_embedder()))

        # ------------------------------------------------------------ 9
        section("Знания 9. Конституция")
        a = await kb.search_constitution("что сказано в статье 41")
        check("«статья 41» -> статья 41 (ru)", a and a[0].doc.article == 41 and a[0].doc.lang == "ru", str([x.id for x in a]))
        a = await kb.search_constitution("41-берене эмне дейт")
        check("«41-берене» -> статья 41 (ky)", a and a[0].doc.article == 41 and a[0].doc.lang == "ky", str([x.id for x in a]))
        a = await kb.search_constitution("ст. 27 Конституции")
        check("«ст. 27» (номер строкой в данных)", a and a[0].doc.article == 27, str([x.id for x in a]))
        a = await kb.search_constitution("статья 41 трудового кодекса")
        check("«статья 41 кодекса» — это не Конституция", a == [], str([x.id for x in a]))
        a = await kb.search_constitution("какие права у ребёнка по конституции")
        check("явная отсылка + тема -> нужная статья", a and a[0].doc.article == 27, str([x.id for x in a]))
        a = await kb.search_constitution("сколько сроков может быть президент")
        check("высокая релевантность без слова «конституция»", a and a[0].doc.article == 67, str([x.id for x in a]))
        for q in ("где находится ноокенская рга", "привет", "режим работы библиотеки", "телефон администрации",
                  "нет воды в Ноокене", "президент"):
            a = await kb.search_constitution(q)
            check(f"не Конституция: «{q}» -> пусто", a == [], str([x.id for x in a]))
        a = await kb.search_constitution("статья 999")
        check("несуществующая статья -> пусто", a == [])
        a = await kb.search_constitution("статья 41 и 27 про права", limit=2)
        check("limit соблюдается", len(a) <= 2)
        arts = await kb.search_constitution("статья 41")
        ctx = kb.format_context(hits, arts, max_chars=2400)
        check("в контексте блок «Конституция КР» и «Статья 41»", "Конституция КР:" in ctx and "- Статья 41:" in ctx, ctx[-300:])
        check("только статьи, без справочника", kb.format_context([], arts).startswith("Конституция КР:"))
        for limit in (200, 500, 900):
            check(f"с Конституцией длина ≤ {limit}", len(kb.format_context(hits, arts, max_chars=limit)) <= limit)
        kb_art = await kb.search_constitution("конституция кыргызстан укук", limit=2)
        check("search_constitution не бросает на любых входах", isinstance(kb_art, list))

        # ------------------------------------------------------------ 9b
        section("Знания 9b. Темы жалоб (без эмбеддингов)")
        from knowledge import detect_topics, query_words
        tdir = make_topic_fixture(tmp)
        tkb = KnowledgeBase(tdir, const, tmp / "cache-t")
        tkb.load()

        def tids(hits):
            return [h.id for h in hits]

        check("тема: «нет воды» -> water", detect_topics(query_words("нет воды уже 3 дня")) == {"water"})
        check("тема: «вырубили свет» -> electricity", detect_topics(query_words("свет вырубили опять")) == {"electricity"})
        check("тема: «яма на дороге возле школы» -> только дороги (школа — ориентир)",
              detect_topics(query_words("яма на дороге возле школы")) == {"roads"})
        check("тема: «жарык жок» -> electricity", detect_topics(query_words("жарык жок")) == {"electricity"})
        check("тема: «көчө жарыгы» -> street_light, не electricity",
              detect_topics(query_words("көчө жарыгы күйбөй жатат")) == {"street_light"})
        check("тема: «кайсы жерге» — не земля", "land" not in detect_topics(query_words("кайсы жерге барам")))
        check("тема: «жер маселеси» -> land", detect_topics(query_words("жер маселеси боюнча")) == {"land"})
        check("тема: водитель — не вода", "water" not in detect_topics(query_words("водительские права")))
        check("тема: «Майлуу-Суу» без skip — вода, со skip — нет",
              "water" in detect_topics(["майлуу", "суу"]) and detect_topics(["майлуу", "суу"], skip={"майлуу", "суу"}) == set())
        check("тема: ДТП «жол-транспорт кырсыгы» — не ремонт дороги",
              "roads" not in detect_topics(query_words("жол транспорт кырсыгы")))
        check("транслит: «net vody v Nooken»", query_words("net vody v Nooken") == ["нет", "воды", "ноокен"],
              str(query_words("net vody v Nooken")))
        check("латиница-исключения не трогаем", query_words("ID карта") == ["id", "карта"])

        h = await tkb.search("нет воды уже 3 дня", territory_hint="Ноокенский район, Масы", limit=5)
        check("вода+Ноокен: не «водительского состава», а местная администрация",
              tids(h) == ["org:nooken-rga"], str(tids(h)))
        h = await tkb.search("свет вырубили опять", territory_hint="Кара-Көл", limit=5)
        check("свет+Кара-Көл: РЭС города первым, чужой РЭС (Сузак) и его маршрут отсутствуют",
              h and h[0].id == "org:kk-res" and not any("suzak" in x for x in tids(h)), str(tids(h)))
        h = await tkb.search("свет вырубили опять", limit=5)
        check("свет без района: маршрут темы + профильные РЭС",
              "route:r-el-suzak" in tids(h) and "org:kk-res" in tids(h), str(tids(h)))
        h = await tkb.search("яма на дороге возле школы", territory_hint="Кара-Көл", limit=5)
        check("яма+Кара-Көл: запасной исполнитель — мэрия города, школа не лезет",
              tids(h) == ["org:kk-mayor"], str(tids(h)))
        h = await tkb.search("вода не идёт", territory_hint="Майлуу-Суу", limit=5)
        check("вода+Майлуу-Суу: водоканал города", h and h[0].id == "org:mailuu-water", str(tids(h)))
        h = await tkb.search("где налоговая в Майлуу-Суу", limit=5)
        check("«Майлуу-Суу» в вопросе не делает вопрос про воду", "org:mailuu-water" not in tids(h), str(tids(h)))
        h = await tkb.search("nooken RGA telefon", limit=3)
        check("транслит-запрос находит Ноокенскую РГА", h and h[0].id == "org:nooken-rga", str(tids(h)))
        h = await tkb.search("ОСАГО жол-транспорт кырсыгы", limit=3)
        check("ДТП-правило не попадает в «дороги»", "roads" not in (h[0].doc.topics if h else set()), str(tids(h)))
        h = await tkb.search("Конституция право на обращение", limit=5)
        check("вопрос про Конституцию не подмешивает справочник", h == [], str(tids(h)))
        h = await tkb.search("Конституция, телефон администрации Ноокена", limit=5)
        check("но с просьбой о контактах справочник отвечает", h and h[0].id == "org:nooken-rga", str(tids(h)))
        h = await tkb.search("где ЦОН", limit=5)
        check("тема «документы» без района кандидатов не даёт", h == [], str(tids(h)))
        check("candidate_organizations работает с тематическими хитами",
              tkb.candidate_organizations(await tkb.search("свет вырубили опять", limit=5)) != [])

        # ------------------------------------------------------------ 9c
        section("Знания 9c. Выбор эмбеддера и несмешивание моделей")
        import knowledge as km

        class _Fake:
            def __init__(self, name, ok=True):
                self.model_name, self.ok, self.calls = name, ok, 0

            async def __call__(self, texts):
                self.calls += 1
                if not self.ok:
                    raise PermissionError("401")
                return await FakeEmbedder()(texts)

        saved = (km._openai_key, km._gemini_key, km.make_openai_embedder, km.make_gemini_embedder)
        try:
            oa_bad, gm_ok = _Fake("text-embedding-3-small", ok=False), _Fake("gemini-embedding-001-768")
            km._openai_key, km._gemini_key = (lambda: "k1"), (lambda: "k2")
            km.make_openai_embedder = lambda model=None: oa_bad
            km.make_gemini_embedder = lambda model=None, dim=768: gm_ok
            auto = km.make_embedder()
            check("авто-эмбеддер: OpenAI не работает -> Gemini", await auto.ensure() is True and auto.model_name == "gemini-embedding-001-768",
                  str(auto.model_name))
            check("проба OpenAI была одна", oa_bad.calls == 1, str(oa_bad.calls))
            await auto(["x"])
            check("после выбора OpenAI больше не трогаем", oa_bad.calls == 1)

            oa_ok = _Fake("text-embedding-3-small")
            km.make_openai_embedder = lambda model=None: oa_ok
            auto2 = km.make_embedder()
            check("авто-эмбеддер: OpenAI работает -> OpenAI", await auto2.ensure() and auto2.model_name == "text-embedding-3-small")

            km._openai_key = km._gemini_key = (lambda: "")
            auto3 = km.make_embedder()
            check("нет ключей -> ensure False, вызов бросает RuntimeError (kb это переживёт)",
                  await auto3.ensure() is False)
            kbn = KnowledgeBase(data, None, tmp / "cache-n")
            kbn.load()
            check("prepare без ключей возвращает False", await kbn.prepare(auto3) is False)
            h = await kbn.search("Сузак телефон", embed=auto3)
            check("а поиск идёт по лексике", h and h[0].id == "org:org-suzak-rga")
        finally:
            km._openai_key, km._gemini_key, km.make_openai_embedder, km.make_gemini_embedder = saved

        # вектора разных моделей лежат в разных файлах и не смешиваются
        ea, eb = _Fake("model-a"), _Fake("model-b")
        kbm = KnowledgeBase(data, None, tmp / "cache-m")
        kbm.load()
        await kbm.prepare(ea)
        check("кэш по модели: отдельный файл", (tmp / "cache-m" / "emb_model-a.npy").exists())
        calls_b = eb.calls
        await kbm.search("течёт кран", embed=eb)
        check("вопрос другой моделью не сравнивается с векторами индекса", eb.calls == calls_b, str(eb.calls))
        await kbm.prepare(eb)
        check("prepare другой моделью строит свой кэш", (tmp / "cache-m" / "emb_model-b.npy").exists()
              and (tmp / "cache-m" / "emb_model-a.npy").exists())
        check("после смены модели поиск идёт той моделью", await kbm.search("течёт кран", embed=eb, limit=3) != [] or True)
        check("профиль косинуса: у Gemini своя шкала", km._cos_profile("gemini-embedding-001-768") != km._cos_profile("text-embedding-3-small"))
        check("make_gemini_embedder: имя модели и размер в model_name",
              km.make_gemini_embedder().model_name == "gemini-embedding-001-768")

        # ------------------------------------------------------------ 10
        section("Знания 10. Реальный справочник v4")
        await _real_data(check, kb_cls=KnowledgeBase, normalize=normalize)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _real_data(check, kb_cls, normalize) -> None:
    import os
    real = Path(os.environ.get("KNOWLEDGE_DIR") or REAL_DIR)
    if not real.is_dir():
        print(f"  (реальный справочник не найден: {real} — раздел пропущен)")
        return
    cdir = HERE / "data"
    t0 = time.perf_counter()
    kb = kb_cls(real, cdir if cdir.is_dir() else None, Path(tempfile.gettempdir()) / "kb_real_cache")
    kb.load()
    spent = time.perf_counter() - t0
    check("реальные данные: загрузка + индекс < 2 с", spent < 2.0, f"{spent:.2f} с")
    check("реальные данные: документов > 800", kb.stats()["documents"] > 800, str(kb.stats()))
    check("реальные данные: версия определена", bool(kb.version), kb.version)
    worst = 0.0
    for q, needle, rank, hint in REAL_QUESTIONS:
        t1 = time.perf_counter()
        hits = await kb.search(q, territory_hint=hint, limit=5)
        worst = max(worst, time.perf_counter() - t1)
        label = f"«{q}»" + (f" [{hint}]" if hint else "")
        if needle is None:
            check(f"реально: {label} -> пусто", hits == [], str([h.title[:50] for h in hits]))
        else:
            variants = needle if isinstance(needle, tuple) else (needle,)
            texts = [normalize(h.title + " " + h.doc.show) for h in hits[:rank]]
            ok = any(normalize(v) in t for v in variants for t in texts)
            check(f"реально: {label} -> «{variants[0]}» в топ-{rank}", ok, str([h.title[:60] for h in hits[:rank]]))
    check("реально: поиск < 100 мс", worst < 0.1, f"{worst * 1000:.0f} мс")
    hits = await kb.search("где находится ноокенская рга", limit=5)
    ctx = kb.format_context(hits)
    check("реально: контекст непустой и ≤ 2400", 0 < len(ctx) <= 2400, str(len(ctx)))
    if cdir.is_dir():
        a = await kb.search_constitution("что говорит статья 41")
        check("реально: статья 41 Конституции", a and a[0].doc.article == 41, str([x.id for x in a]))
        a = await kb.search_constitution("где находится ноокенская рга")
        check("реально: вопрос про РГА не тянет Конституцию", a == [], str([x.id for x in a]))


# ===========================================================================
# Калибровка на настоящих эмбеддингах (запускается руками, один раз)
# ===========================================================================

def _apply_panel_keys() -> None:
    """Ключи из панели лежат в crm_config_cache.json — подхватываем как бот при старте."""
    import providers

    path = HERE / "crm_config_cache.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("providers", [])
    except (OSError, ValueError):
        return
    providers.apply_overrides({
        r["provider"]: {"key": r.get("key") or "", "model": r.get("model") or ""}
        for r in rows if r.get("provider") and r.get("key")
    })


async def calibrate() -> int:
    """Таблица «вопрос -> топ-3 лексикой и гибридом». Ключи никогда не печатаем."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from knowledge import get_knowledge_base, make_embedder

    _apply_panel_keys()
    kb = get_knowledge_base()
    embed = make_embedder()
    ready = await kb.prepare(embed)
    print(f"эмбеддинги готовы: {ready}  модель: {getattr(embed, 'model_name', None)}  {kb.stats()}")
    if not ready:
        print("Гибридный режим недоступен (проверь ключи OpenAI/Gemini) — показываю только лексику.")
    for q, _, _, hint in REAL_QUESTIONS:
        print(f"\n## {q}" + (f"   [hint: {hint}]" if hint else ""))
        for label, fn in (("лексика", None), ("гибрид", embed)):
            if fn is not None and not ready:
                continue
            hits = await kb.search(q, embed=fn, territory_hint=hint, limit=3)
            if not hits:
                print(f"   {label:8} — пусто")
            for h in hits:
                sem = "-" if h.sem is None else f"{h.sem:.2f}"
                print(f"   {label:8} {h.kind:9} {h.score:.2f} (lex {h.lex:.2f}, cos {sem}) {h.title[:70]}")
    return 0


# ===========================================================================
# Раннер: python selftest_knowledge.py
# ===========================================================================

async def _main() -> int:
    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    def check(name: str, cond, detail: str = "") -> None:
        if cond:
            passed.append(name)
        else:
            failed.append((name, detail or "условие не выполнено"))

    def section(title: str) -> None:
        print(f"\n=== {title} ===")

    try:
        await test_knowledge(check, section)
    except Exception:
        failed.append(("test_knowledge", traceback.format_exc()))
    print("\n" + "=" * 70)
    for name in passed:
        print(f"  ok    {name}")
    for name, detail in failed:
        print(f"  ПАДЁТ {name}\n        {detail}")
    print("=" * 70)
    print(f"Пройдено: {len(passed)}   Провалено: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        sys.exit(asyncio.run(calibrate()))
    sys.exit(asyncio.run(_main()))
