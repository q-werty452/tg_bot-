"""
knowledge.py — поиск по справочнику организаций (и по Конституции КР) для промпта бота.

Зачем: раньше модель отвечала «из головы» и выдумывала адреса и телефоны.
Администрация передала справочник (≈340 организаций, контакты, компетенции,
правила «куда обращаться»). Целиком он весит ≈47 тыс. токенов — в каждый запрос
его не положишь. Поэтому здесь ищем 3–6 записей, относящихся к вопросу, и
подставляем в промпт только их (≈400–700 токенов). Нашли мало или ничего —
возвращаем пусто, и бот честно скажет «данных нет», а не придумает.

Как это устроено:
  1. load() читает JSONL-файлы справочника (только публичное) и собирает
     «документы»: карточки организаций, правила маршрутизации, фрагменты,
     вопросы-ответы, порядки обращения. Данные недоверенные: только json.loads,
     никакого кода оттуда не исполняется.
  2. Лексический поиск (BM25) работает без сети: нормализация, свёртка
     кыргызских букв (жители пишут «Ноокен»/«нокен», «Кара-Кол» без ө/ү/ң), стемминг
     обрезкой, исправление опечаток по триграммам.
  3. Смысловой поиск (эмбеддинги) — необязательная добавка. Модуль не зависит
     от SDK: ему передают асинхронную функцию embed(). Любой сбой сети или
     ключа = тихий откат на лексику, исключений наружу не летит НИКОГДА.
  4. Результаты сливаются, поднимаются документы нужного района/города и
     опускаются документы чужой территории. Не прошло порог — пусто.

Единый объект на процесс: get_knowledge_base().
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable, Iterable

try:  # numpy нужен только для смыслового поиска; без него работает лексика
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Тип функции-эмбеддера: список текстов -> список векторов.
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
# Лексические пороги (MIN_SCORE_LEX, веса территорий, TERRITORY/UNKNOWN_WORD_WEIGHT)
# подобраны на реальном справочнике v4 (см. selftest_knowledge.py: набор настоящих
# вопросов). Гибридные (COS_*, WEIGHT_*, MIN_*_HYBRID) — предварительные, по
# типичным значениям косинуса text-embedding-3-small; перекалибровать командой
#   .venv/bin/python selftest_knowledge.py --calibrate
# сразу, как заработает ключ OpenAI. Устроено безопасно: смысл только ДОБАВЛЯЕТ
# находки к лексике, поэтому неточный порог даёт «меньше», а не «мусор».

EMBED_BATCH = 100            # сколько текстов за один запрос к API эмбеддингов
EMBED_QUERY_TIMEOUT = 6.0    # дольше жителя с вопросом не держим
EMBED_COOLDOWN = 60.0        # после сбоя эмбеддингов не пробуем столько секунд
EMBED_CACHE_QUERIES = 256    # сколько запросов помним (экономим сеть на повторах)

BM25_K1 = 1.2
BM25_B = 0.6
FUZZY_MIN_DICE = 0.5         # порог триграммного сходства для исправления опечаток
TERRITORY_WORD_WEIGHT = 0.5  # слова названия района: фильтр, а не тема вопроса
UNKNOWN_WORD_WEIGHT = 0.6    # вес слова, которого нет в справочнике (оно режет покрытие)

# Слияние лексики и смысла. Косинус text-embedding-3-small для «похожего»
# обычно 0.30–0.65, для постороннего 0.05–0.25 — шкалируем это в [0, 1].
# Шкала у каждой модели своя (у Gemini косинусы выше), поэтому пороги по моделям.
COS_PROFILES = {
    "openai": (0.22, 0.52),
    "gemini": (0.45, 0.75),
}
COS_LOW, COS_HIGH = COS_PROFILES["openai"]    # для обратной совместимости


def _cos_profile(model: str) -> tuple[float, float]:
    return COS_PROFILES["gemini" if "gemini" in (model or "").lower() else "openai"]

WEIGHT_LEX = 0.45
WEIGHT_SEM = 0.55

# Пороги «ничего релевантного».
MIN_SCORE_LEX = 0.45         # только слова (доля веса запроса, найденная в записи)
MIN_SCORE_HYBRID = 0.50      # смесь слов и смысла
MIN_LEX_IN_HYBRID = 0.10     # запись без общих слов проходит лишь при...
MIN_SEM_IN_HYBRID = 0.90     # ...очень высоком косинусе (≈0.5 и выше)
RELATIVE_CUTOFF = 0.55       # хвост слабее 55% от лучшего не показываем

# Территориальный буст.
BOOST_EXACT = 1.5
BOOST_RELATED = 1.15
PENALTY_FOREIGN = 0.40
BOOST_CONTACT_INTENT = 1.2   # «телефон/адрес/где» -> карточки организаций выше
TOPIC_BASE = {"route": 0.75, "chunk": 0.70, "org": 0.62}   # оценка записи, попавшей в тему жалобы
TOPIC_LEX_MIN = 0.70          # запись чужой темы при жалобе проходит только по остальным словам и строго
TOPIC_ADMIN_FALLBACK = 0.50  # местная администрация, когда профильной организации в районе нет
PRIOR_ADMIN = 1.06           # администрации/мэрии/айыл окмоту чуть выше прочих при равенстве

# Конституция: ссылаемся только при высокой релевантности или явном номере.
CONST_MIN_SCORE_LEX = 0.62
CONST_MIN_SCORE_LEX_CUE = 0.45   # человек прямо написал «по Конституции» — порог мягче
CONST_MIN_SCORE_HYBRID = 0.62
CONST_MIN_SEM = 0.55
# Вопрос о Конституции («какие права даёт Конституция...») — справочник
# подмешиваем только при очень сильном совпадении: учреждение, в положении
# которого мелькнуло слово «право», жителю тут не поможет.
CONST_DIRECTORY_MIN = 0.8

# ---------------------------------------------------------------------------
# Нормализация текста
# ---------------------------------------------------------------------------

# ё -> е; кыргызские ө/ү/ң -> о/у/н (жители часто пишут без них, а в данных они
# есть — сворачиваем с обеих сторон); мягкий/твёрдый знаки убираем (частая
# причина опечаток).
_FOLD = str.maketrans({"ё": "е", "ө": "о", "ү": "у", "ң": "н", "ъ": None, "ь": None})
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    """NFKC + нижний регистр + свёртка ё/ө/ү/ң. Одинакова для документов и запросов."""
    return unicodedata.normalize("NFKC", text or "").casefold().translate(_FOLD)


def tokenize(text: str) -> list[str]:
    """Слова текста после нормализации (без стемминга)."""
    return _WORD_RE.findall(normalize(text))


# Транслит: жители пишут латиницей («nooken», «net vody», «svet»). Читаем это как
# русскую/кыргызскую фонетику и ищем уже по кириллице. Порядок важен: сначала
# двубуквенные сочетания.
_LATIN_PAIRS = (("sch", "щ"), ("shch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"),
                ("ts", "ц"), ("yo", "е"), ("yu", "ю"), ("ya", "я"), ("ye", "е"), ("ng", "н"),
                ("oo", "оо"), ("uu", "уу"))
_LATIN_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и",
    "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
    "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс", "y": "ы", "z": "з",
}
# Латиница, которую оставляем как есть (аббревиатуры и слова из интернета).
_LATIN_KEEP = frozenset({"id", "whatsapp", "email", "wifi", "sms", "pdf", "http", "https", "www", "kg", "com", "ru"})
_LATIN_RE = re.compile(r"[a-z]+")


def _translit(word: str) -> str:
    """«nooken» -> «ноокен». Слова не из латиницы возвращаются без изменений."""
    if not _LATIN_RE.fullmatch(word) or word in _LATIN_KEEP or len(word) < 3:
        return word
    out, i = [], 0
    while i < len(word):
        for lat, cyr in _LATIN_PAIRS:
            if word.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(_LATIN_SINGLE.get(word[i], word[i]))
            i += 1
    return "".join(out)


def query_words(text: str) -> list[str]:
    """Слова вопроса: нормализация + транслит латиницы. Одиночные латинские буквы
    («v», «i») — шум, выбрасываем."""
    words = []
    for w in tokenize(text):
        if len(w) <= 2 and w.isascii() and w.isalpha() and w not in _LATIN_KEEP:
            continue
        words.append(_translit(w))
    return words


def stem(word: str) -> str:
    """Грубый стемминг: первые 5 символов. Для русских и кыргызских окончаний
    («Ноокенская», «Ноокенде», «Ноокен районунун» -> «нооке»/«район») этого хватает."""
    return word[:5]


def _norm_set(words: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize(w) for w in words)


# Служебные слова: приветствия, вопросительные слова, вежливость. Совпадения по
# ним не должны давать результатов («привет», «подскажите, пожалуйста»).
_STOP_WORDS = _norm_set("""
и в во на по с со к ко у о об от до для за из не но а или что это как где куда когда
какой какая какие каком какое кто чем чего мне мой моя мое мои наш ваш вам вас нас нам
мы я он она они есть ли бы же ну вот тут там очень можно нужно надо хочу хотел хотела
узнать подскажите подскажи скажите скажи помогите помоги пожалуйста спасибо привет
здравствуйте здравствуй добрый доброе день вечер утро ночь здрасте хай алло
находится находятся расположен расположена скока сколько стоит нет все еще при про под над
без через между так такой быть будет был была могу можете нужна нужен нужны сейчас
сегодня завтра вообще просто обратиться обратится куда работы работает работают режим
получить получение получения получу оформить оформление сделать подать найти написать

саламатсызбы саламатсыз салам ассалому алейкум кандай кандайча кантип кайда кайдан кайсы
эмне эмнеге ким качан канча бар жок болот болобу болсо керек керекпи мен биз сиз сен ал
алар бул ошол ушул учун менен жонундо боюнча деген дагы же жана да де го бир эмес болуп
болгон жатат жатабыз айтып айтыныз айтсаныз берсениз суранам сураныч рахмат кечиресиз
кайрылсам кайрылам мага бизге сизге кимге турат жайгашкан жайгашат

уже ещё опять снова давно недавно тоже также даже только почему зачем ничего никто
нету ваще щас плз пж пжл спс ок окей да его ее её их им них этот эта эти это того тот
здесь сюда свой своя свои своё который которая которые если чтобы потому тогда теперь
раньше пока было были будут дня дней неделю недели неделя месяц месяца года часа час
минут кто-то что-то кто-нибудь что-нибудь вчера позавчера постоянно всегда никогда
вырубили вырубило отключили выключили отключился отключено пропал пропала пропало сломался сломана
сломано разбита разбито разбитая порвало прорвало бузулган бузулду очту очуп кетти калды аткан
возле около рядом напротив близ жанында жанынан алдында артында
попасть попасти алам алсам барам барсам жазам жазсам кылам кылсам турам жашайм жашайбыз
уважаемые уважаемый извините прошу просим хотим хотели нам срочно

эле эч эчким эчнерсе бери кун кундон апта ай жыл азыр бугун эртен кечээ мурун
биздин сиздин менин алардын бизде сизде анан андан ошондо мындай ошондой абдан
ото дайыма кайра дагыле ушундай сурайм сурайбыз жардам бериниз урматтуу
""".split())

# Слова-намерения «нужны контакты/адрес/часы»: сами по себе ничего не ищем, но
# поднимаем карточки организаций (в них лежат адреса и телефоны).
_CONTACT_WORDS = _norm_set("""
телефон телефону телефонду телефона номер номери номеру тел адрес адреса адресу дарек
дареги контакт контакты байланыш график часы время убактысы убакыты убакыт иштейт иштейби расписание прием приема приему приёма приём email почта сайт whatsapp вотсап
где находится кайда расположен
""".split())
_CONTACT_STEMS = frozenset(stem(w) for w in _CONTACT_WORDS) - {"где", "кайда"}

# Ключи слов, указывающих на Конституцию (сами по себе в поиске не участвуют).
_CONST_CUE_STEMS = frozenset(stem(normalize(w)) for w in (
    "конституция конституции конституцию конституциясы конституциянын статья статье статьи берене".split()))

# Общие слова, которые нужны только как намерение, а искать по ним бессмысленно.
_INTENT_ONLY = _CONTACT_WORDS


# Небольшой словарь синонимов/сокращений: запрос на одном языке находит
# документы на другом, а «рга» — «районную государственную администрацию».
# Формат: (режим, слова-триггеры в запросе, слова-добавки). Правится руками.
#   "alt"    — добавки это то же понятие на другом языке: слово запроса считается
#              найденным, если в документе есть ЛЮБОЕ из них («вода» ~ «суу»);
#   "expand" — сокращение раскрывается в несколько слов, каждое ищется отдельно
#              (сама аббревиатура в названиях не встречается).
_SYNONYMS: list[tuple[str, str, str]] = [
    ("expand", "рга рма акимият акимиат аким акимов", "районная государственная администрация райондук мамлекеттик администрация"),
    ("alt", "айыл окмоту сельсовет аилокмоту", "айыл окмоту"),
    ("alt", "вода воды воду водой водопровод суу суусу", "вода водопровод суу водоснабжение"),
    ("alt", "свет света электричество электр жарык жарыгы", "электр электричество жарык"),
    ("alt", "мусор мусора таштанды", "мусор таштанды"),
    ("alt", "дорога дороги дорогу жол жолдор", "дорога жол дорожные"),
    ("alt", "люк люка", "люк колодец"),
    ("alt", "канализация канализацию агынды", "канализация агынды"),
    ("alt", "школа школы школу мектеп", "школа мектеп"),
    ("alt", "садик детсад бакча", "садик бакча"),
    ("alt", "больница больницу оорукана дарыгер врач поликлиника", "больница оорукана дарыгер"),
    ("alt", "полиция милиция", "полиция милиция внутренних иштер"),
    ("alt", "налоговая налог салык", "налоговая налог салык"),
    ("alt", "земля земли землю участок жер тилке", "земля земельный жер тилкеси участок"),
    ("alt", "пенсия пенсию пенсионный пенсиялар", "пенсия пенсиялар"),
    ("alt", "библиотека библиотеки китепкана", "библиотека китепкана"),
    ("alt", "справка справку маалымкат", "справка маалымкат"),
    ("alt", "скорая", "скорая тез жардам"),
]

# Человеческие названия тем для кодов вроде ROAD/WATER в правилах маршрутизации
# (в данных триггерами записаны сами английские коды, жители так не пишут).
_TOPIC_LABELS: dict[str, tuple[str, str]] = {
    "ROAD": ("дорога, ремонт дорог, асфальт", "жол, жолду оңдоо"),
    "WATER": ("вода, водопровод, питьевая вода", "суу, ичүүчү суу, суу түтүгү"),
    "WASTE": ("мусор, вывоз мусора", "таштанды, таштанды чыгаруу"),
    "STREET_LIGHT": ("уличное освещение, фонари", "көчө жарыгы"),
    "ELECTRICITY": ("электричество, свет", "электр энергиясы, жарык"),
    "MANHOLE": ("люк, колодец", "люк"),
    "LAND": ("земельный участок, земля", "жер тилкеси, жер"),
    "SEWER": ("канализация", "канализация, агынды суу"),
    "WATER_SERVICE": ("водоснабжение", "суу менен камсыздоо"),
    "EDUCATION": ("образование, школа", "билим берүү, мектеп"),
    "SOCIAL_SUPPORT": ("социальная поддержка, пособия", "социалдык колдоо"),
}
_JUNK_TOPICS = {"OTHER", "UNKNOWN", "ESCALATION", "ROUTING", "COMPETENCE"}


# ---------------------------------------------------------------------------
# Темы жалоб: «нет воды», «яма на дороге», «свет вырубили» — без эмбеддингов
# ---------------------------------------------------------------------------
# Жители пишут о проблеме своими словами (по-русски, по-кыргызски, сленгом), а в
# справочнике это записано названиями организаций и кодами маршрутов (WATER,
# ROAD…). Мост между ними — небольшой словарь тем. Тема распознана → кандидаты:
# маршруты этой темы, организации «профиля» (водоканал, РЭС, ДЭУ, Тазалык…) и,
# для коммунальных тем, местная администрация как запасной исполнитель.
#
# Слова запроса: «пре*» — по началу слова, без звёздочки — точное слово. Точные
# формы нужны там, где префикс опасен: «воды» не должно совпасть с «водительских»,
# «свет» — со «светофором». Всё нормализуется так же, как текст (ө→о, ь убран).

@dataclass(frozen=True)
class _Topic:
    name: str
    label: str
    words: tuple[str, ...]
    org_re: str = ""                  # профиль организаций по названию (регулярка)
    codes: tuple[str, ...] = ()       # canonical_topic в маршрутах и фрагментах
    phrases: tuple[str, ...] = ()     # регулярки по всему тексту запроса
    weak: tuple[str, ...] = ()        # слова, дающие тему лишь если нет конкурента
    communal: bool = False            # при пробеле берём местную администрацию
    needs_place: bool = False         # без названного района кандидатов не даём (ЦОН есть не везде)


_TOPICS: tuple[_Topic, ...] = (
    _Topic("water", "вода / водоснабжение",
           "вода воды воду водой воде водо* кран* водопровод* суу суу* ичүүчү",
           r"водоканал|сууканал|водо(снабж|провод|хозяйств)|таза суу|ичуучу суу|суу менен камсыз|суу чарба|суу ресурс|коммунал",
           codes=("WATER", "WATER_SERVICE"), communal=True),
    _Topic("irrigation", "полив / ирригация",
           "полив* сугат* арык* арыкт* ирригац* оросительн* мелиорац*",
           r"суу чарба|ирригац|сугат|мелиорац|водного хозяйства", communal=True),
    _Topic("electricity", "электричество",
           "электр* электричеств* свет света свету светом свете трансформатор* столб* столбы рэс энерг*",
           r"электр|\bрэс\b|энерг|эл\.? ?тармак",
           codes=("ELECTRICITY",), weak=("жарык*", "жарыг*"), communal=True),
    _Topic("street_light", "уличное освещение",
           "фонар* освещен*",
           r"освещен|кочо жары[кг]", codes=("STREET_LIGHT",),
           phrases=(r"кочо\s+жары[кг]", r"улич\w*\s+(свет|освещ|фонар)"), communal=True),
    _Topic("roads", "дороги",
           "дорог* дорож* яма ямы яму ямой яме ямк* выбоин* асфальт* жол жолдо* жолду* жолдун жолго жолдор* "
           "чуңкур* көпүрө* мост мосту моста мосты тротуар* щебен* гравий* обочин*",
           r"дорожн|автожол|автомобильн\w* жол|жол ?(курулуш|эксплуат|чарба|кызмат)|жолдор|благоустр|\bдэу\b",
           codes=("ROAD",), communal=True),
    _Topic("waste", "мусор / санитария",
           "мусор* свалк* помойк* отход* вывоз* вывез* таштанд* акыр чикир тазалык санитар* уборк*",
           r"тазалык|мусор|таштанд|санитарн(?!о.эпидемиол)|коммунал|благоустр|озеленен|отход",
           codes=("WASTE",), communal=True),
    _Topic("sewer", "люки / канализация",
           "люк* канализац* сток* колодц* агынды* коллектор*",
           r"канализац|водоканал|сууканал|агынды|коммунал|благоустр",
           codes=("MANHOLE", "SEWER"), communal=True),
    _Topic("gas_heating", "газ / отопление",
           "газ газа газу газом газов* отоплен* батаре* котельн* тепл* жылуулук* жылыт*",
           r"газ\b|\bгаз|газов|теплоснаб|жылуулук|отоплен|котельн|теплосет", communal=True),
    _Topic("land", "земля / кадастр",
           "земл* земельн* участ* кадастр* тилке* геодез* межев* межа межи",
           r"земельн|\bжер (ресурс|агентт|комитет|тилке)|жер ресурс|кадастр|геодез",
           codes=("LAND",), communal=True),
    _Topic("construction", "строительство / архитектура",
           "стройк* строительств* застройк* самовольн* архитектур* градостро* курулуш*",
           r"архитектур|градостро|шаар куруу|курулуш|строительств|строител",
           codes=("ILLEGAL_CONSTRUCTION",), communal=True),
    _Topic("social", "соцпомощь / пенсии",
           "пособи* пенси* жөлөкпул* жөлөк* малоимущ* малообеспеч* соцфонд* соцзащит* социал* инвалид* "
           "многодетн* опекун* субсиди* мүгөк* соцпомощ*",
           r"социал|эмгек|труда|пенси|мигра|соцфонд", codes=("SOCIAL_SUPPORT",)),
    _Topic("health", "медицина",
           "больниц* поликлиник* врач* доктор* дарыгер* оорукан* оору* фап цсм цовп скорая скорой скорую "
           "медицин* лечен* прививк* эмдөө* аптек* госпитал* стационар*",
           r"больниц|поликлиник|медицин|ден.?соолук|оорукана|дарыгер|врачебн|цовп|цсм|\bфап\b|здравоохран|госпитал|диспансер|центр семейной",
           codes=("HEALTH",)),
    _Topic("education", "образование",
           "школ* мектеп* детсад* садик* бакча* университет* колледж* лицей* учител* мугалим* гимнази*",
           r"школ|мектеп|образован|билим бер|детск\w* сад|бала бакча|университет|колледж|лицей|гимнази",
           codes=("EDUCATION",)),
    _Topic("public_order", "правопорядок",
           "полици* милици* драк* краж* украл* воровств* вор увд овд иштер хулиган* стрельб* тонооч* уурд*",
           r"внутренних дел|ички иштер|милици|полици|\bовд\b|\bувд\b|жол кыймыл",
           codes=("PUBLIC_ORDER",)),
    _Topic("agro", "сельское хозяйство / ветеринария",
           "скот* корова коров* овц* лошад* пастбищ* ветеринар* мал малы малдын малга малдар* жайыт* жайлоо* "
           "тоют* сено сена урожай* агроном* ферм*",
           r"ветеринар|мал чарба|жайыт|тоют|сельск\w* хозяйств|айыл чарба"),
    _Topic("documents", "документы",
           "паспорт* справк* маалымкат* цон загс свидетельств* нотариус* доверенност* id прописк* регистраци*",
           r"центр обслуживания населения|\bцон\b|тундук|паспорт|загс|нотариус|жарандык абал",
           needs_place=True),
)

_TOPIC_BY_NAME = {t.name: t for t in _TOPICS}
_CODE_TOPICS: dict[str, tuple[str, ...]] = {}
for _t in _TOPICS:
    for _c in _t.codes:
        _CODE_TOPICS[_c] = _CODE_TOPICS.get(_c, ()) + (_t.name,)


def _compile_words(patterns: Iterable[str]) -> tuple[frozenset[str], tuple[str, ...]]:
    exact, prefixes = set(), []
    for raw in patterns:
        w = normalize(raw)
        if w.endswith("*"):
            prefixes.append(w[:-1])
        else:
            exact.add(w)
    return frozenset(exact), tuple(prefixes)


_TOPIC_WORDS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}
_TOPIC_WEAK: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}
_TOPIC_PHRASES: dict[str, list[re.Pattern]] = {}
_TOPIC_ORG_RE: dict[str, re.Pattern] = {}
for _t in _TOPICS:
    _TOPIC_WORDS[_t.name] = _compile_words(_t.words.split())
    _TOPIC_WEAK[_t.name] = _compile_words(_t.weak)
    _TOPIC_PHRASES[_t.name] = [re.compile(normalize(p)) for p in _t.phrases]
    if _t.org_re:
        _TOPIC_ORG_RE[_t.name] = re.compile(normalize(_t.org_re))

# «Кайсы жерге» = «куда», а не «земля»: такие соседи отменяют слово «жер».
_JER_WORDS = frozenset(normalize(w) for w in ("жер", "жерди", "жердин", "жерлер", "жерлерди"))
_JER_BAD_PREV = frozenset(normalize(w) for w in ("кайсы", "кандай", "ушул", "ошол", "бул", "бир", "кайда", "ар", "башка"))
# «Яма возле школы»: школа здесь лишь ориентир, а не тема жалобы.
_CONTEXT_PREPS = frozenset(normalize(w) for w in (
    "возле", "около", "рядом", "напротив", "близ", "жанында", "жанынан", "алдында", "артында"))


def _match_words(word: str, spec: tuple[frozenset[str], tuple[str, ...]]) -> bool:
    exact, prefixes = spec
    return word in exact or any(word.startswith(p) for p in prefixes)


# «Жол-транспорт кырсыгы», «жол кыймылы» — это ДТП и ГАИ, а не ремонт дороги.
_JOL_NOT_ROAD_NEXT = frozenset(normalize(w) for w in (
    "транспорт", "кыймыл", "кыймылы", "кыймылынын", "кырсык", "кырсыгы", "белги", "эрежеси"))


def _word_topics(word: str, prev: str = "", nxt: str = "") -> set[str]:
    """Темы, на которые указывает одно слово запроса."""
    out = {name for name, spec in _TOPIC_WORDS.items() if _match_words(word, spec)}
    if "roads" in out and word.startswith("жол") and nxt in _JOL_NOT_ROAD_NEXT:
        out.discard("roads")
    if word in _JER_WORDS and prev not in _JER_BAD_PREV:
        out.add("land")
    return out


def detect_topics(words: list[str], skip: Iterable[str] = ()) -> set[str]:
    """Темы жалобы в запросе. skip — слова названия района («Майлуу-Суу» — не вода)."""
    skip = set(skip)
    primary: set[str] = set()
    secondary: set[str] = set()
    weak: set[str] = set()
    for i, w in enumerate(words):
        if w in skip:
            continue
        prev = words[i - 1] if i else ""
        found = _word_topics(w, prev, words[i + 1] if i + 1 < len(words) else "")
        if found:
            (secondary if prev in _CONTEXT_PREPS else primary).update(found)
        for name, spec in _TOPIC_WEAK.items():
            if _match_words(w, spec):
                weak.add(name)
    text = " ".join(w for w in words if w not in skip)
    for name, pats in _TOPIC_PHRASES.items():
        if any(p.search(text) for p in pats):
            primary.add(name)
    return primary or secondary or weak


def _is_topic_word(word: str, prev: str = "", nxt: str = "") -> bool:
    return bool(_word_topics(word, prev, nxt))


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Doc:
    """Одна запись для поиска."""
    id: str
    kind: str                      # org | route | chunk | qa | procedure | constitution
    title: str                     # короткое имя (для логов и таблиц)
    search: str                    # текст для лексического поиска (без служебного мусора)
    embed_text: str                # текст для эмбеддинга (короткий)
    show: str                      # компактный текст для модели
    organization_ids: tuple[str, ...] = ()
    territory_ids: frozenset[str] = frozenset()
    group: str = ""                # ключ группы для ограничения однотипных результатов
    prior: float = 1.0             # небольшая предпочтительность (администрации — «по умолчанию»)
    article: int = 0               # номер статьи (только Конституция)
    lang: str = ""
    code: str = ""                 # canonical_topic (ROAD, WATER…) у маршрутов и фрагментов
    aux: str = ""                  # текст для определения темы (названия организации, тема правила)
    topics: frozenset[str] = frozenset()   # темы жалоб, к которым относится запись
    admin: bool = False            # местная администрация (РГА, мэрия, айыл окмоту)


@dataclass
class Hit:
    """Найденный документ с оценками (для отладки и калибровки видны слагаемые)."""
    doc: Doc
    score: float
    lex: float = 0.0
    sem: float | None = None

    @property
    def id(self) -> str:
        return self.doc.id

    @property
    def kind(self) -> str:
        return self.doc.kind

    @property
    def title(self) -> str:
        return self.doc.title


@dataclass
class _Term:
    """Один смысловой термин запроса: несколько допустимых ключей индекса."""
    word: str
    alts: list[tuple[str, float]]   # (ключ индекса, вес совпадения)
    weight: float = 1.0
    ref_idf: float = 0.0
    place: bool = False          # слово из названия района/города: фильтр, не тема
    topic: bool = False          # слово указывает на тему жалобы («воды», «яма»)


@dataclass
class _Query:
    terms: list[_Term] = field(default_factory=list)
    words: list[str] = field(default_factory=list)  # слова запроса (для территорий)
    keys: list[str] = field(default_factory=list)   # ключи ВСЕХ слов (для территорий)
    contact_intent: bool = False
    const_cue: bool = False        # в вопросе есть «конституция/статья» (в поиске не участвует)
    topics: set[str] = field(default_factory=set)       # темы жалобы (см. detect_topics)
    place_words: set[str] = field(default_factory=set)  # слова, ушедшие в название территории


# ---------------------------------------------------------------------------
# Индекс BM25 с опечатками
# ---------------------------------------------------------------------------

def _trigrams(word: str) -> set[str]:
    padded = f"_{word}_"
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def _terr_key_match(query_key: str, terr_key: str) -> bool:
    """Слово запроса — название территории (с падежным окончанием).
    Короткое название («Бука») узнаём по началу слова («Букинский»), длинное —
    по тем же 5 буквам. «Ноокен» и «Ноот» намеренно не путаем."""
    if query_key == terr_key:
        return True
    return len(terr_key) < 5 and len(query_key) > len(terr_key) and query_key.startswith(terr_key)


def _joined_match(query_joined: str, terr_joined: str) -> bool:
    """Слитное сравнение: «кочкорате» ~ «кочкората», «базаркоргоне» ~ «базаркоргон»."""
    if len(terr_joined) < 5:
        return False
    extra = len(query_joined) - len(terr_joined)
    if query_joined.startswith(terr_joined):
        return extra <= 6
    if abs(extra) <= 2 and query_joined[:3] == terr_joined[:3] and len(terr_joined) >= 7:
        return difflib.SequenceMatcher(None, query_joined, terr_joined).ratio() >= 0.85
    return False


class _Index:
    """BM25 по ключам (стемам) + поиск ближайшего слова при опечатке."""

    def __init__(self) -> None:
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.doc_len: list[int] = []
        self.avg_len = 1.0
        self.idf: dict[str, float] = {}
        self.words: dict[str, int] = defaultdict(int)       # слово -> частота (для опечаток)
        self._prefix: dict[int, dict[str, list[str]]] = {}
        self._tri: dict[str, list[str]] | None = None

    # -- построение --------------------------------------------------------
    def add(self, weighted_texts: Iterable[tuple[str, int]]) -> None:
        """Добавить документ: набор (текст, вес). Вес 3 = слово считается трижды
        (названия важнее остального текста)."""
        counts: dict[str, int] = defaultdict(int)
        total = 0
        for text, weight in weighted_texts:
            for word in tokenize(text):
                counts[stem(word)] += weight
                total += weight
                self.words[word] += 1
        doc_idx = len(self.doc_len)
        self.doc_len.append(total)
        for key, tf in counts.items():
            self.postings[key].append((doc_idx, tf))

    def finish(self) -> None:
        n = max(len(self.doc_len), 1)
        self.avg_len = (sum(self.doc_len) / n) or 1.0
        self.idf = {
            key: math.log(1.0 + (n - len(p) + 0.5) / (len(p) + 0.5))
            for key, p in self.postings.items()
        }
        self.idf_unknown = math.log(1.0 + (n + 0.5) / 0.5)
        for size in (3, 4):
            table: dict[str, list[str]] = defaultdict(list)
            for key in self.postings:
                if len(key) >= size:
                    table[key[:size]].append(key)
            self._prefix[size] = table

    # -- разбор слов запроса ------------------------------------------------
    def _fuzzy(self, word: str) -> tuple[str, float] | None:
        """Ближайшее известное слово по триграммам (Dice). None, если не похоже."""
        if len(word) < 4:
            return None
        if self._tri is None:
            tri: dict[str, list[str]] = defaultdict(list)
            for known in self.words:
                if len(known) >= 4:
                    for t in _trigrams(known):
                        tri[t].append(known)
            self._tri = tri
        mine = _trigrams(word)
        shared: dict[str, int] = defaultdict(int)
        for t in mine:
            for cand in self._tri.get(t, ()):
                shared[cand] += 1
        best: tuple[float, int, str] | None = None
        for cand, hit in shared.items():
            if abs(len(cand) - len(word)) > 3 or min(len(cand), len(word)) / max(len(cand), len(word)) < 0.6:
                continue
            dice = 2.0 * hit / (len(mine) + len(_trigrams(cand)))
            # опечатка в первой букве редка: с другой первой буквой нужно очень близкое слово
            if dice < FUZZY_MIN_DICE or (cand[0] != word[0] and dice < 0.75):
                continue
            rank = (dice, self.words[cand], cand)
            if best is None or rank > best:
                best = rank  # type: ignore[assignment]
        return (best[2], best[0]) if best else None

    def resolve(self, word: str, expand: bool = True) -> tuple[list[tuple[str, float]], str]:
        """Слово запроса -> допустимые ключи индекса и «исправленный» ключ слова.

        expand=False — без «мягких» окончаний у коротких слов: для слов-тем
        («воды») иначе находились бы «водительских»; нужное расширение даёт
        словарь тем, а не весь словарь справочника."""
        key = stem(word)
        alts: list[tuple[str, float]] = []
        if key in self.postings:
            alts.append((key, 1.0))
        if expand and len(word) <= 5:
            need = max(3, len(word) - 1)
            for other in self._prefix.get(need, {}).get(word[:need], ()):
                if other != key:
                    alts.append((other, 0.7))
        if alts or not expand:
            return alts, key
        found = self._fuzzy(word)
        if found:
            fixed = stem(found[0])
            return [(fixed, 0.85)], fixed
        return [], key

    # -- оценка -------------------------------------------------------------
    def score(self, terms: list[_Term]) -> tuple[dict[int, float], dict[int, int]]:
        """Доля «веса запроса» (0..1), которую набрал документ.

        Каждый термин даёт не больше своего веса (повторы слова в тексте не
        накручивают счёт), поэтому документ, совпавший по одному слову из трёх,
        не получит 1.0. Обычный BM25 (с учётом частоты и длины) идёт лишь
        небольшой добавкой, чтобы упорядочить документы с равным покрытием."""
        total_ref = sum(t.weight * t.ref_idf for t in terms)
        if total_ref <= 0:
            return {}, {}
        covered: dict[int, float] = defaultdict(float)
        raw: dict[int, float] = defaultdict(float)
        matched: dict[int, int] = defaultdict(int)     # сколько «тематических» слов нашлось
        for term in terms:
            best: dict[int, float] = {}
            best_raw: dict[int, float] = {}
            for key, w in term.alts:
                idf = self.idf.get(key)
                if idf is None:
                    continue
                for doc_idx, tf in self.postings[key]:
                    norm = 1.0 - BM25_B + BM25_B * self.doc_len[doc_idx] / self.avg_len
                    tfn = tf * (BM25_K1 + 1.0) / (tf + BM25_K1 * norm)
                    cap = w * idf * min(1.0, tfn)
                    if cap > best.get(doc_idx, 0.0):
                        best[doc_idx] = cap
                    full = w * idf * tfn
                    if full > best_raw.get(doc_idx, 0.0):
                        best_raw[doc_idx] = full
            for doc_idx, part in best.items():
                covered[doc_idx] += term.weight * part
                if not term.place:
                    matched[doc_idx] += 1
            for doc_idx, part in best_raw.items():
                raw[doc_idx] += term.weight * part
        scores = {
            d: min(1.0, 0.92 * (c / total_ref) + 0.08 * min(1.0, raw[d] / total_ref / 1.5))
            for d, c in covered.items()
        }
        return scores, dict(matched)

    def make_query(self, text: str, syn_groups, topics: bool = True) -> _Query:
        """Разбор запроса: стоп-слова, намерения, опечатки, синонимы."""
        q = _Query()
        words = query_words(text)
        q.words = words
        for word in words:
            if word in _CONTACT_WORDS or stem(word) in _CONTACT_STEMS:
                q.contact_intent = True
        seen_expand: set[str] = set()
        for idx, word in enumerate(words):
            # Числа (номера домов, телефоны, «3 дня») по смыслу вопроса ничего
            # не ищут, зато совпадают с адресами случайных учреждений: «статья 41»
            # находила дом № 41. Номер статьи Конституции ловится отдельно.
            if any(ch.isdigit() for ch in word):
                q.keys.append(stem(word))     # место в списке нужно для поиска территорий
                continue
            if word in _STOP_WORDS:
                q.keys.append(stem(word))
                continue
            if word in _INTENT_ONLY or stem(word) in _CONTACT_STEMS:
                q.keys.append(stem(word))
                continue
            if stem(word) in _CONST_CUE_STEMS:
                q.keys.append(stem(word))
                q.const_cue = True
                continue
            is_topic = topics and _is_topic_word(
                word, words[idx - 1] if idx else "", words[idx + 1] if idx + 1 < len(words) else "")
            alts, key = self.resolve(word, expand=not is_topic)
            q.keys.append(key)
            groups = [g for g in syn_groups if word in g[1] or stem(word) in g[1]
                      or (g[0] == "expand" and len(word) <= 10 and word.startswith(_ABBR_PREFIXES))]
            expand = [g for g in groups if g[0] == "expand"]
            if expand:
                for _, _, extra in expand:
                    for extra_word in extra:
                        ekey = stem(extra_word)
                        if ekey in seen_expand or ekey not in self.idf:
                            continue
                        seen_expand.add(ekey)
                        q.terms.append(_Term(extra_word, [(ekey, 1.0)], weight=0.6,
                                             ref_idf=self.idf[ekey]))
                continue
            for _, _, extra in groups:
                have = {k for k, _ in alts}
                for extra_word in extra:
                    ekey = stem(extra_word)
                    if ekey in self.idf and ekey not in have:
                        alts.append((ekey, 0.85))
                        have.add(ekey)
            if alts:
                strong = [self.idf[k] for k, w in alts if w >= 0.85]
                ref = max(strong) if strong else max(self.idf[k] for k, _ in alts)
                q.terms.append(_Term(word=word, alts=alts, ref_idf=ref, topic=is_topic))
            else:
                # слово справочнику неизвестно: идёт в знаменатель с пониженным весом,
                # чтобы «билет на самолёт» не находил случайный «билет» где-то в тексте
                q.terms.append(_Term(word=word, alts=[], weight=UNKNOWN_WORD_WEIGHT,
                                     ref_idf=self.idf_unknown, topic=is_topic))
        return q


def _build_synonyms() -> list[tuple[str, frozenset[str], list[str]]]:
    groups = []
    for mode, triggers, extra in _SYNONYMS:
        trig = set()
        for w in tokenize(triggers):
            trig.add(w)
            trig.add(stem(w))
        words = [w for w in tokenize(extra) if w not in _STOP_WORDS]
        groups.append((mode, frozenset(trig), words))
    return groups


_SYN_GROUPS = _build_synonyms()
_ABBR_PREFIXES = ("рга", "рма", "аким")      # «РГАнын», «РМАда», «акиму»: слово с окончанием


# ---------------------------------------------------------------------------
# Утилиты форматирования
# ---------------------------------------------------------------------------

def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rstrip()
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.-") + "…"


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = re.sub(r"\s+", " ", (item or "")).strip()
        key = normalize(item)
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _is_public(rec: dict) -> bool:
    """Публично ли: явный visibility, а если поля нет — по статусу публикации."""
    vis = rec.get("visibility")
    if vis:
        return str(vis).upper() == "PUBLIC"
    status = str(rec.get("publication_status") or "").upper()
    return not any(bad in status for bad in ("INTERNAL", "PRIVATE", "RESTRICTED", "DRAFT"))


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return []


def _read_jsonl(path: Path) -> list[dict]:
    """Читает JSONL, пропуская битые строки: данные не доверяем, ломаться не должны."""
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as error:
        logger.warning("Справочник: не удалось прочитать %s (%s)", path.name, error)
    return rows


def _topic_labels(topic: str) -> tuple[str, str] | None:
    code = (topic or "").strip()
    if re.fullmatch(r"[A-Z][A-Z_]+", code):
        if code in _TOPIC_LABELS:
            return _TOPIC_LABELS[code]
        if code in _JUNK_TOPICS:
            return None
        words = code.replace("_", " ").lower()
        return (words, words)
    return None


# ---------------------------------------------------------------------------
# База знаний
# ---------------------------------------------------------------------------

# Местная администрация: РГА, мэрия, айыл окмоту — первые адресаты бытовых жалоб.
_ADMIN_RE = re.compile(normalize(
    r"райондук мамлекеттик администрац|районная государственная администрац|айыл окмот|айыл аймагынын|"
    r"мэри[яи]|мэрия города|шаардык мэри|городская администрац"))
_NOT_ADMIN_RE = re.compile(normalize(r"департамент|управлени|отдел|бөлүм|башкарма|при мэри|предприяти|мп «"))


class KnowledgeBase:
    """Поиск по справочнику. Создаётся один раз на процесс (get_knowledge_base)."""

    def __init__(self, data_dir: Path | None, constitution_dir: Path | None,
                 cache_dir: Path, model: str = "text-embedding-3-small") -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self.constitution_dir = Path(constitution_dir) if constitution_dir else None
        self.cache_dir = Path(cache_dir)
        self.model = model
        self.enabled = False
        self.version = ""
        self.embed_timeout = EMBED_QUERY_TIMEOUT

        self._docs: list[Doc] = []
        self._index = _Index()
        self._const_docs: list[Doc] = []
        self._const_index = _Index()
        self._const_by_article: dict[tuple[str, int], Doc] = {}

        self._orgs: dict[str, dict] = {}              # id -> {"name": ..., "ru": ..., "ky": ...}
        self._cards: dict[str, Doc] = {}              # id организации -> её карточка
        self._by_topic: dict[str, list[int]] = defaultdict(list)   # тема -> номера документов
        self._admin_idx: list[int] = []               # номера документов-администраций
        self._territories: dict[str, dict] = {}       # id -> запись территории
        self._terr_match: list[tuple[str, list[str], str]] = []   # (id, ключи имени, имя слитно)
        self._parent: dict[str, str] = {}

        self._vecs = None                              # матрица документов (n, dim) или None
        self._cos = _cos_profile(model)                # (низ, верх) шкалы косинуса модели
        self._vec_model = ""                           # какой моделью посчитаны векторы
        self._const_vecs = None
        self._prepare_lock: asyncio.Lock | None = None
        self._embed_fail_until = 0.0
        self._qcache: OrderedDict[str, list[float]] = OrderedDict()
        self.load_seconds = 0.0

    # ------------------------------------------------------------------ загрузка
    def load(self) -> None:
        """Прочитать справочник и построить индексы. Синхронно, быстро (< 2 с)."""
        started = time.perf_counter()
        self.enabled = False
        self._docs, self._const_docs = [], []
        self._index, self._const_index = _Index(), _Index()
        self._const_by_article = {}
        self._cards = {}
        self._by_topic = defaultdict(list)
        self._admin_idx = []
        self._vecs = self._const_vecs = None

        if self.data_dir is None or not self.data_dir.is_dir():
            logger.warning("Справочник: папка данных не найдена (%s) — поиск по нему выключен",
                           self.data_dir)
            self._load_constitution()      # Конституция живёт отдельно от справочника
            self._finish_constitution()
            self.load_seconds = time.perf_counter() - started
            return
        try:
            self._load_directory()
        except Exception:  # данные недоверенные: любой сюрприз не должен ронять бота
            logger.exception("Справочник: ошибка разбора данных — поиск по нему выключен")
            self._docs = []
            self._index = _Index()
        self._load_constitution()
        self._finish_constitution()
        self.enabled = bool(self._docs)
        if not self.enabled:
            logger.warning("Справочник: документов не набралось — поиск выключен")
        self.version = self._compute_version()
        self.load_seconds = time.perf_counter() - started
        logger.info("Справочник загружен: %d документов, %d статей, %.2f с",
                    len(self._docs), len(self._const_docs), self.load_seconds)

    def _compute_version(self) -> str:
        parts = []
        manifest = self.data_dir / "knowledge_manifest.json" if self.data_dir else None
        if manifest and manifest.is_file():
            try:
                parts.append(str(json.loads(manifest.read_text(encoding="utf-8")).get("knowledge_version", "")))
            except (OSError, ValueError):
                pass
        if not parts or not parts[0]:
            sig = hashlib.sha1("|".join(d.id for d in self._docs).encode()).hexdigest()[:12]
            parts = [sig]
        if self._const_docs:
            cs = hashlib.sha1("|".join(d.id + d.embed_text[:40] for d in self._const_docs).encode()).hexdigest()[:6]
            parts.append("c" + cs)
        return "+".join(parts)

    # -- территории ----------------------------------------------------------
    def _load_territories(self, rows: list[dict]) -> None:
        for row in rows:
            tid = row.get("territory_id")
            if not tid or tid == "city:гак":
                continue
            self._territories[tid] = row
            parent = row.get("parent_territory")
            if parent:
                self._parent[tid] = parent
            kind, _, core = tid.partition(":")
            if kind in ("region", "service_zone") or not core:
                continue
            keys = [stem(w) for w in tokenize(core)]
            if not keys or (len(keys) == 1 and len(keys[0]) < 4):
                continue
            self._terr_match.append((tid, keys, "".join(tokenize(core))))

    def _terr_name(self, tid: str, lang: str = "ru") -> str:
        row = self._territories.get(tid)
        if not row:
            return tid.partition(":")[2]
        order = ("name_ru", "name_ky") if lang == "ru" else ("name_ky", "name_ru")
        for field_name in order:
            if row.get(field_name):
                return str(row[field_name])
        return tid.partition(":")[2]

    def _ancestors(self, tid: str) -> set[str]:
        out: set[str] = set()
        cur = self._parent.get(tid)
        while cur and cur not in out and not cur.startswith("region:"):
            out.add(cur)
            cur = self._parent.get(cur)
        return out

    def _find_territories(self, words: list[str], keys: list[str]) -> dict[str, tuple[int, int]]:
        """Какие территории названы в тексте.

        Два способа, чтобы переживать падежи и опечатки: по ключам слов
        («Ноокенский» -> «нооке») и по склеенным словам («Кочкор-Ате» ->
        «кочкорате» ~ «кочкората»)."""
        found: dict[str, tuple[int, int]] = {}
        if not keys:
            return found
        n_words = len(keys)
        for tid, tkeys, tjoined in self._terr_match:
            n = len(tkeys)
            hit_at = -1
            for i in range(n_words - n + 1):
                if all(_terr_key_match(keys[i + j], tkeys[j]) for j in range(n)):
                    hit_at, span = i, n
                    break
            if hit_at < 0 and words:
                for size in range(max(1, n - 1), n + 1):
                    for i in range(len(words) - size + 1):
                        if _joined_match("".join(words[i:i + size]), tjoined):
                            hit_at, span = i, size
                            break
                    if hit_at >= 0:
                        break
            if hit_at < 0:
                continue
            after = keys[hit_at + span] if hit_at + span < n_words else ""
            if after.startswith(("облас", "облус")):
                continue      # «Жалал-Абадская область» — вся область, а не город
            found[tid] = (hit_at, hit_at + span)
        # «Базар-Коргон» не должен заодно означать село «Коргон»: название, целиком
        # лежащее внутри более длинного найденного, отбрасываем.
        return {
            tid: (a, b) for tid, (a, b) in found.items()
            if not any((a2 <= a and b <= b2) and (b2 - a2) > (b - a) for a2, b2 in found.values())
        }

    def _infer_territories(self, text: str) -> set[str]:
        keys = [stem(w) for w in tokenize(text)]
        # в названиях ищем строго (без «мягкого» сопоставления коротких слов)
        found: set[str] = set()
        for tid, tkeys, _ in self._terr_match:
            n = len(tkeys)
            for i in range(len(keys) - n + 1):
                if keys[i:i + n] == tkeys:
                    after = keys[i + n] if i + n < len(keys) else ""
                    if not after.startswith(("облас", "облус")):    # «Жалал-Абад облусу» — область
                        found.add(tid)
                    break
        return found

    # -- сбор документов ------------------------------------------------------
    def _load_directory(self) -> None:
        d = self.data_dir
        assert d is not None
        self._load_territories(_read_jsonl(d / "territories.jsonl"))
        orgs = [o for o in _read_jsonl(d / "organizations.jsonl") if _is_public(o) and o.get("organization_id")]
        contacts = [c for c in _read_jsonl(d / "contacts.jsonl") if _is_public(c)]
        comps = [c for c in _read_jsonl(d / "competences.jsonl") if _is_public(c)]
        services = [s for s in _read_jsonl(d / "services.jsonl") if _is_public(s)]
        procs = [p for p in _read_jsonl(d / "procedures.jsonl") if _is_public(p)]
        routes = [r for r in _read_jsonl(d / "routing_rules.jsonl") if _is_public(r) and not r.get("exclusion")]
        qas = [q for name in ("qa_pairs_ru.jsonl", "qa_pairs_ky.jsonl")
               for q in _read_jsonl(d / name) if _is_public(q)]
        chunks = [c for c in _read_jsonl(d / "rag_chunks.jsonl") if _is_public(c)]

        by_org_contacts: dict[str, list[dict]] = defaultdict(list)
        for c in contacts:
            by_org_contacts[c.get("organization_id", "")].append(c)
        by_org_comps: dict[str, list[dict]] = defaultdict(list)
        for c in comps:
            by_org_comps[c.get("organization_id", "")].append(c)
        by_org_services: dict[str, list[dict]] = defaultdict(list)
        for s in services:
            by_org_services[s.get("organization_id", "")].append(s)

        org_terr: dict[str, set[str]] = {}
        # сначала имена и территории всех организаций (нужны маршрутам и фрагментам)
        for o in orgs:
            oid = o["organization_id"]
            names_ru = _uniq([o.get("name_ru") or "", o.get("short_name_ru") or "", *_as_list(o.get("aliases_ru"))])
            names_ky = _uniq([o.get("name_ky") or "", o.get("short_name_ky") or "", *_as_list(o.get("aliases_ky"))])
            ru = (o.get("name_ru") or (names_ru[0] if names_ru else "")).strip()
            ky = (o.get("name_ky") or (names_ky[0] if names_ky else "")).strip()
            if ky and normalize(ky) == normalize(ru):
                ky = ""
            display = " / ".join(x for x in (_clip(ru, 140), _clip(ky, 140)) if x)
            terr = {t for t in _as_list(o.get("territory")) if t != "city:гак" and not t.startswith("region:")}
            for c in by_org_contacts.get(oid, []):
                t = c.get("territory_id")
                if t and t != "city:гак" and not str(t).startswith("region:"):
                    terr.add(t)
            if not terr:
                terr = self._infer_territories(" ".join(names_ru + names_ky))
            org_terr[oid] = terr
            self._orgs[oid] = {"name": ru or ky, "display": display, "ru": ru, "ky": ky,
                               "names_ru": names_ru, "names_ky": names_ky}

        for o in orgs:
            doc = self._org_doc(o, by_org_contacts.get(o["organization_id"], []),
                                by_org_comps.get(o["organization_id"], []),
                                by_org_services.get(o["organization_id"], []),
                                org_terr[o["organization_id"]])
            if doc:
                self._add(doc)

        seen_routes: set[tuple] = set()
        for r in routes:
            doc = self._route_doc(r, org_terr, by_org_contacts, seen_routes)
            if doc:
                self._add(doc)
        for c in chunks:
            tags = set(_as_list(c.get("tags")))
            if "contact" in tags:
                continue          # контакты уже в карточках организаций
            doc = self._chunk_doc(c, tags, org_terr)
            if doc:
                self._add(doc)
        seen_qa: set[str] = set()
        for q in qas:
            doc = self._qa_doc(q, org_terr, seen_qa)
            if doc:
                self._add(doc)
        for p in procs:
            doc = self._proc_doc(p, org_terr)
            if doc:
                self._add(doc)
        self._index.finish()

    def _add(self, doc: Doc, const: bool = False) -> None:
        if doc.kind == "org" and doc.organization_ids:
            self._cards[doc.organization_ids[0]] = doc
        if const:
            self._const_docs.append(doc)
            self._const_index.add([(doc.title, 2), (doc.search, 1)])
        else:
            topics = self._doc_topics(doc)
            if topics:
                doc = replace(doc, topics=topics)
            self._docs.append(doc)
            self._index.add(self._weighted(doc))
            for t in topics:
                self._by_topic[t].append(len(self._docs) - 1)
            if doc.admin:
                self._admin_idx.append(len(self._docs) - 1)

    @staticmethod
    def _doc_topics(doc: Doc) -> frozenset[str]:
        """К каким темам жалоб относится запись: по коду маршрута или по названию."""
        topics: set[str] = set(_CODE_TOPICS.get(doc.code, ())) if doc.code else set()
        if doc.kind == "org" and doc.aux:
            text = normalize(doc.aux)
            topics |= {name for name, rx in _TOPIC_ORG_RE.items() if rx.search(text)}
        elif doc.kind == "route" and doc.aux:
            topics |= detect_topics(tokenize(doc.aux))
        return frozenset(topics)

    @staticmethod
    def _weighted(doc: Doc) -> list[tuple[str, int]]:
        # В search первая строка — названия (вес 3), остальное — обычный текст.
        head, _, tail = doc.search.partition("\n")
        return [(head, 3), (tail, 1)]

    def _terr_text(self, tids: Iterable[str]) -> str:
        """«Сузак району / Сузакский район» — для поиска (оба языка)."""
        parts = []
        for tid in sorted(tids):
            parts.append(self._terr_name(tid, "ky"))
            parts.append(self._terr_name(tid, "ru"))
        return " ".join(_uniq(parts))

    def _terr_show(self, tids: Iterable[str], limit: int = 2) -> str:
        names = _uniq(self._terr_name(t, "ru") for t in sorted(tids))
        return ", ".join(names[:limit])

    # -- карточка организации --------------------------------------------------
    def _org_doc(self, o: dict, contacts: list[dict], comps: list[dict],
                 services: list[dict], terr: set[str]) -> Doc | None:
        oid = o["organization_id"]
        info = self._orgs[oid]
        if not (contacts or comps or services):
            return None
        addresses, phones, faxes, hours, reception, emails, sites = [], [], [], [], [], [], []
        legal = []
        for c in contacts:
            value = str(c.get("value") or "").strip().rstrip(" .;,")
            kind = c.get("type")
            if not value:
                continue
            if kind == "address":
                (legal if c.get("purpose") == "legal_address" else addresses).append(value)
            elif kind == "phone":
                phones.append(value)
            elif kind == "fax":
                faxes.append(value)
            elif kind == "working_hours":
                hours.append(value)
            elif kind == "reception_hours":
                reception.append(value)
            elif kind == "email":
                emails.append(value)
            elif kind == "website":
                sites.append(value)
            if c.get("working_hours") and kind not in ("working_hours", "reception_hours"):
                hours.append(str(c["working_hours"]))
        addresses = self._dedupe_addresses(_uniq(addresses or legal))
        phones, faxes = _uniq(phones), _uniq(faxes)
        hours, reception = _uniq(hours), _uniq(reception)
        emails, sites = _uniq(emails), _uniq(sites)

        comp_texts, comp_examples = [], []
        for c in comps:
            text = c.get("description_ru") or c.get("description_ky") or ""
            if text:
                comp_texts.append(text)
            comp_examples.extend(_as_list(c.get("include_examples")))
        comp_texts = _uniq(comp_texts)
        service_names = _uniq(s.get("name") or "" for s in services)

        # --- для поиска: названия (вес 3), потом всё остальное -------------
        names_all = " ; ".join(info["names_ru"] + info["names_ky"])
        rest = [
            self._terr_text(terr),
            " ".join(addresses),
            " ".join(_clip(t, 500) for t in comp_texts[:6]),
            " ".join(_clip(t, 200) for t in _uniq(comp_examples)[:5]),
            " ".join(service_names[:8]),
        ]
        search = names_all + "\n" + " ; ".join(x for x in rest if x)

        # --- для показа модели ----------------------------------------------
        bits = [info["display"] or oid]
        head = bits[0]
        place = self._terr_show(terr)
        if place:
            head += f" — {place}"
        fields = []
        if addresses:
            fields.append("Адрес: " + "; ".join(_clip(a, 120) for a in addresses[:2]))
        if phones:
            fields.append("Тел.: " + ", ".join(phones[:4]))
        if faxes:
            fields.append("Факс: " + ", ".join(faxes[:1]))
        if hours:
            fields.append("Часы: " + "; ".join(hours[:2]))
        if reception:
            fields.append("Приём: " + "; ".join(reception[:2]))
        if emails:
            fields.append("Email: " + ", ".join(emails[:2]))
        if sites:
            fields.append("Сайт: " + sites[0])
        if comp_texts:
            fields.append("Занимается: " + _clip("; ".join(_clip(t, 130) for t in comp_texts[:2]), 200))
        if service_names:
            fields.append("Услуги: " + _clip("; ".join(service_names[:3]), 140))
        show = head + (". " + ". ".join(fields) if fields else "")

        embed_text = _clip(f"{names_all}. {self._terr_text(terr)}. "
                           + " ".join(_clip(t, 300) for t in comp_texts[:3]) + " "
                           + " ".join(service_names[:4]), 900)
        admin = any(m in normalize(names_all) for m in ("администрац", "айыл окмот", "мэри"))
        return Doc(id=f"org:{oid}", kind="org", title=info["name"] or oid, search=search,
                   embed_text=embed_text, show=show, organization_ids=(oid,),
                   territory_ids=frozenset(terr), group=f"org:{oid}",
                   prior=PRIOR_ADMIN if admin else 1.0, aux=names_all,
                   admin=bool(_ADMIN_RE.search(normalize(names_all))) and not _NOT_ADMIN_RE.search(normalize(names_all)))

    @staticmethod
    def _dedupe_addresses(addresses: list[str]) -> list[str]:
        """Один и тот же адрес по-русски и по-кыргызски (совпадают все числа) — оставляем первый."""
        out: list[str] = []
        seen: set[tuple] = set()
        for a in addresses:
            digits = tuple(re.findall(r"\d+", a))
            if digits and digits in seen:
                continue
            seen.add(digits)
            out.append(a)
        return out

    # -- правило маршрутизации ---------------------------------------------------
    def _route_doc(self, r: dict, org_terr: dict[str, set[str]],
                   by_org_contacts: dict[str, list[dict]], seen: set) -> Doc | None:
        target = r.get("target_organization_id") or ""
        info = self._orgs.get(target)
        name = info["name"] if info else ""
        if not name:
            labels = _as_list(r.get("responsible_candidate_labels"))
            name = labels[0] if labels else ""
        if not name:
            return None
        topic = str(r.get("topic") or r.get("canonical_topic") or "").strip()
        canon = str(r.get("canonical_topic") or "").strip()
        label = _topic_labels(canon) or _topic_labels(topic)
        triggers_ru = [t for t in _as_list(r.get("trigger_phrases_ru")) if not _topic_labels(t)]
        triggers_ky = [t for t in _as_list(r.get("trigger_phrases_ky")) if not _topic_labels(t)]
        if label:
            topic_ru, topic_ky = label
            shown_topic = topic_ru
        else:
            if not topic or topic.upper() in _JUNK_TOPICS or topic.lower() == "unknown":
                return None
            topic_ru = topic_ky = topic
            shown_topic = topic
        terr = set()
        for t in [r.get("territory_id"), *_as_list(r.get("territory")), *_as_list(r.get("territory_scope"))]:
            if t and t != "city:гак" and not str(t).startswith("region:"):
                terr.add(str(t))
        clar = _uniq(_as_list(r.get("required_clarifications")))
        sig = (normalize(shown_topic), tuple(sorted(terr)), target, tuple(normalize(c) for c in clar))
        if sig in seen:
            return None
        seen.add(sig)
        if not terr and target in org_terr:
            terr = set(org_terr[target])

        dep = r.get("target_department_name_ky") or ""
        dep_ru = dep if dep and "эмес" not in dep and ";" not in dep else ""
        search = (" ".join(_uniq([topic_ru, topic_ky, topic, *triggers_ru, *triggers_ky])) + " " + name
                  + "\n" + self._terr_text(terr) + " " + dep_ru)
        phone = ""
        for c in by_org_contacts.get(target, []):
            if c.get("type") == "phone" and c.get("value"):
                phone = str(c["value"]).strip()
                break
        place = self._terr_show(terr, 1)
        show = f"Вопрос «{_clip(shown_topic, 80)}»" + (f" ({place})" if place else "")
        show += f" → {_clip(name, 120)}"
        if dep_ru:
            show += f" ({_clip(dep_ru, 60)})"
        if phone:
            show += f", тел. {phone}"
        if clar:
            show += "; уточнить у жителя: " + ", ".join(clar[:3])
        embed_text = _clip(f"{topic_ru}. {topic_ky}. {' '.join(triggers_ru + triggers_ky)} "
                           f"{self._terr_text(terr)}. {name}", 500)
        return Doc(id=f"route:{r.get('routing_rule_id') or len(self._docs)}", kind="route",
                   title=f"{shown_topic} → {name}", search=search, embed_text=embed_text,
                   show=show, organization_ids=(target,) if target else (),
                   territory_ids=frozenset(terr), group=f"route:{normalize(shown_topic)}",
                   code=canon.upper(),
                   aux=" ".join([topic_ru, topic_ky, *triggers_ru, *triggers_ky]))

    # -- фрагмент ----------------------------------------------------------------
    def _chunk_doc(self, c: dict, tags: set[str], org_terr: dict[str, set[str]]) -> Doc | None:
        text = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        if len(text) < 20:
            return None
        title = str(c.get("title") or "").strip()
        if "/" in title or re.search(r"\.(docx?|pdf|xlsx?)$", title, re.I):
            title = ""       # это имя файла, а не название
        orgs = tuple(o for o in _as_list(c.get("organization_ids")) if o in self._orgs)
        terr = {t for t in _as_list(c.get("territory_scope")) if not t.startswith("region:")}
        if not terr:
            for o in orgs:
                terr |= org_terr.get(o, set())
        if not terr:
            terr = self._infer_territories(title or text[:160])
        label = _topic_labels(str(c.get("topic") or ""))
        extra = (label[0] + " " + label[1]) if label else ""
        search = title + " " + extra + "\n" + text
        show = (f"{_clip(title, 100)}: " if title else "") + _clip(text, 320)
        if label and title:
            show = f"{_clip(title, 100)} [{label[0]}]: " + _clip(text, 300)
        group = "chunk:" + ("derived" if tags & {"competence", "service", "routing", "procedure"} else str(c.get("chunk_id")))
        return Doc(id=f"chunk:{c.get('chunk_id')}", kind="chunk", title=title or text[:50],
                   search=search, embed_text=_clip(f"{title}. {extra} {text}", 900), show=show,
                   organization_ids=orgs, territory_ids=frozenset(terr),
                   group=group + ("|" + "|".join(sorted(tags)) if tags else ""),
                   code=str(c.get("topic") or "").upper() if "routing_context" in tags else "")

    # -- вопрос-ответ ---------------------------------------------------------------
    def _qa_doc(self, q: dict, org_terr: dict[str, set[str]], seen: set[str]) -> Doc | None:
        question = str(q.get("question") or "").strip()
        answer = str(q.get("answer") or "").strip()
        if not question or not answer:
            return None
        key = normalize(question + answer)
        if key in seen:
            return None
        seen.add(key)
        orgs = tuple(o for o in _as_list(q.get("organization_ids")) if o in self._orgs)
        terr: set[str] = set()
        for o in orgs:
            terr |= org_terr.get(o, set())
        names = " ".join(self._orgs[o]["name"] for o in orgs)
        return Doc(id=f"qa:{q.get('qa_id') or len(self._docs)}", kind="qa", title=question[:80],
                   search=question + "\n" + names + " " + answer,
                   embed_text=_clip(question + " " + answer, 700),
                   show=f"Вопрос: {_clip(question, 160)} Ответ: {_clip(answer, 300)}",
                   organization_ids=orgs, territory_ids=frozenset(terr), group=f"qa:{normalize(question)[:40]}")

    # -- порядок обращения ------------------------------------------------------------
    def _proc_doc(self, p: dict, org_terr: dict[str, set[str]]) -> Doc | None:
        name = str(p.get("name") or "").strip()
        steps = [s for s in _as_list(p.get("steps")) if s]
        if not name or not steps:
            return None
        oid = p.get("organization_id") or ""
        orgs = (oid,) if oid in self._orgs else ()
        terr = {t for t in [p.get("territory_id")] if t and not str(t).startswith("region:")}
        if not terr:
            terr = set(org_terr.get(oid, set()))
        org_name = self._orgs[oid]["name"] if orgs else ""
        docs_needed = _uniq(_as_list(p.get("required_documents")))
        deadline = str(p.get("deadline") or "").strip()
        search = f"{name}\n{org_name} " + " ".join(steps) + " " + " ".join(docs_needed)
        show = f"Порядок «{_clip(name, 80)}»" + (f" ({_clip(org_name, 80)})" if org_name else "") + ": "
        show += " ".join(f"{i}) {_clip(s, 110)}" for i, s in enumerate(steps[:3], 1))
        if docs_needed:
            show += ". Документы: " + _clip("; ".join(docs_needed), 120)
        if deadline:
            show += ". Срок: " + _clip(deadline, 60)
        return Doc(id=f"proc:{p.get('procedure_id') or len(self._docs)}", kind="procedure", title=name,
                   search=search, embed_text=_clip(f"{name}. {org_name}. " + " ".join(steps), 800),
                   show=show, organization_ids=orgs, territory_ids=frozenset(terr),
                   group=f"proc:{normalize(name)}")

    # -- Конституция -------------------------------------------------------------------
    def _load_constitution(self) -> None:
        if not self.constitution_dir or not self.constitution_dir.is_dir():
            return
        for path in sorted(self.constitution_dir.glob("constitution_*.jsonl")):
            for row in _read_jsonl(path):
                try:
                    article = int(row.get("article"))
                except (TypeError, ValueError):
                    continue
                text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
                if not text:
                    continue
                lang = str(row.get("lang") or path.stem.rpartition("_")[2] or "ru")
                title = str(row.get("title") or "").strip()
                chapter = str(row.get("chapter") or "").strip()
                section = str(row.get("section") or "").strip()
                label = "Статья" if lang == "ru" else "Берене"
                if article == 0:
                    label = "Преамбула" if lang == "ru" else "Кириш сөз"
                doc = Doc(id=f"const:{lang}:{article}", kind="constitution",
                          title=(f"{label} {article}" if article else label) + (f". {title}" if title else ""),
                          search=f"{title} {chapter} {section}\n{text}",
                          embed_text=_clip(f"{label} {article}. {title}. {text}", 1200),
                          show=_clip(text, 600), article=article, lang=lang,
                          group=f"const:{lang}:{article}")
                self._const_by_article[(lang, article)] = doc
                self._add(doc, const=True)

    def _finish_constitution(self) -> None:
        if self._const_docs:
            self._const_index.finish()

    # ------------------------------------------------------------------ эмбеддинги
    def _cache_paths(self, model: str | None = None) -> tuple[Path, Path]:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", model or self.model)
        return self.cache_dir / f"emb_{safe}.npy", self.cache_dir / f"emb_{safe}.json"

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _load_cache(self, model: str | None = None) -> dict[str, "np.ndarray"]:
        npy, meta = self._cache_paths(model)
        try:
            if not (npy.is_file() and meta.is_file()):
                return {}
            hashes = json.loads(meta.read_text(encoding="utf-8"))
            mat = np.load(npy)
            if mat.ndim != 2 or mat.shape[0] != len(hashes):
                return {}
            return {h: mat[i] for i, h in enumerate(hashes)}
        except Exception as error:
            logger.warning("Кэш эмбеддингов повреждён, пересчитаем (%s)", error)
            return {}

    def _save_cache(self, store: dict[str, "np.ndarray"], model: str | None = None) -> None:
        npy, meta = self._cache_paths(model)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            hashes = list(store)
            mat = np.stack([store[h] for h in hashes]).astype("float32") if hashes else np.zeros((0, 1), "float32")
            tmp_npy, tmp_meta = npy.with_suffix(".npy.tmp"), meta.with_suffix(".json.tmp")
            with tmp_npy.open("wb") as fh:
                np.save(fh, mat)
            tmp_meta.write_text(json.dumps(hashes), encoding="utf-8")
            tmp_npy.replace(npy)
            tmp_meta.replace(meta)
        except Exception as error:
            logger.warning("Не удалось сохранить кэш эмбеддингов (%s)", error)

    async def prepare(self, embed: EmbedFn | None) -> bool:
        """Досчитать эмбеддинги документов (только новые — остальное из кэша на диске).

        Можно вызывать фоном при старте. Ошибка сети/ключа — warning и возврат False:
        поиск продолжит работать на лексике. Возвращает True, если смысловой поиск готов.
        """
        if embed is None or np is None or not (self._docs or self._const_docs):
            return False
        if self._prepare_lock is None:
            self._prepare_lock = asyncio.Lock()
        async with self._prepare_lock:
            try:
                return await self._prepare(embed)
            except Exception as error:
                logger.warning("Эмбеддинги справочника недоступны, работаем на лексике (%s)",
                               type(error).__name__)
                return False

    @staticmethod
    async def _model_of(embed: EmbedFn | None, default: str) -> str | None:
        """Какой моделью считает эмбеддер. Авто-эмбеддер сначала выбирает модель."""
        if embed is None:
            return None
        ensure = getattr(embed, "ensure", None)
        if ensure is not None and not await ensure():
            return None
        return getattr(embed, "model_name", None) or default

    @staticmethod
    async def _call_embed(embed: EmbedFn, texts: list[str], task: str):
        if getattr(embed, "task_aware", False):
            return await embed(texts, task=task)     # type: ignore[call-arg]
        return await embed(texts)

    async def _prepare(self, embed: EmbedFn) -> bool:
        model = await self._model_of(embed, self.model)
        if model is None:
            logger.warning("Эмбеддинги справочника: нет рабочей модели, остаёмся на лексике")
            return False
        if model != self._vec_model:
            self._vecs = self._const_vecs = None     # вектора другой модели смешивать нельзя
            self._qcache.clear()
        store = self._load_cache(model)
        all_docs = self._docs + self._const_docs
        hashes = [self._sha(d.embed_text) for d in all_docs]
        todo: list[tuple[str, str]] = []
        seen: set[str] = set()
        for h, d in zip(hashes, all_docs):
            if h not in store and h not in seen:
                seen.add(h)
                todo.append((h, d.embed_text))
        failed = False
        size = max(1, int(getattr(embed, "batch_size", EMBED_BATCH)))
        for start in range(0, len(todo), size):
            batch = todo[start:start + size]
            try:
                vectors = await self._call_embed(embed, [t for _, t in batch], "document")
                arr = np.asarray(vectors, dtype="float32")
                if arr.ndim != 2 or arr.shape[0] != len(batch):
                    raise ValueError("эмбеддер вернул неверную форму")
            except Exception as error:
                # текст ошибки не пишем: в нём бывает фрагмент ключа
                logger.warning("Эмбеддинги: пачка %d не посчитана (%s, статус %s)",
                               start // size + 1, type(error).__name__,
                               getattr(error, "status_code", None) or getattr(error, "code", "-"))
                failed = True
                break
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            arr = arr / np.where(norms == 0, 1.0, norms)
            for (h, _), vec in zip(batch, arr):
                store[h] = vec
            self._save_cache({h: store[h] for h in store}, model)
        if todo:
            logger.info("Эмбеддинги: посчитано %d новых из %d документов", 0 if failed else len(todo), len(all_docs))
        keep = set(hashes)
        if todo and not failed:
            store = {h: v for h, v in store.items() if h in keep}   # чистим устаревшее
            self._save_cache(store, model)
        if any(h not in store for h in hashes):
            return False
        dims = {store[h].shape[0] for h in hashes}
        if len(dims) != 1:
            return False
        n_main = len(self._docs)
        if self._docs:
            self._vecs = np.stack([store[h] for h in hashes[:n_main]])
        if self._const_docs:
            self._const_vecs = np.stack([store[h] for h in hashes[n_main:]])
        self._vec_model = model
        self._cos = _cos_profile(model)
        return True

    async def _embed_query(self, query: str, embed: EmbedFn | None):
        """Вектор запроса или None (нет эмбеддера, сбой, недавний сбой)."""
        if embed is None or np is None or (self._vecs is None and self._const_vecs is None):
            return None
        key = normalize(query)
        if key in self._qcache:
            self._qcache.move_to_end(key)
            return self._qcache[key]
        if time.monotonic() < self._embed_fail_until:
            return None
        try:
            # Вектор вопроса обязан быть той же модели, что и вектора справочника.
            if await self._model_of(embed, self.model) != self._vec_model:
                raise ValueError("модель эмбеддера не совпадает с моделью индекса")
            vectors = await asyncio.wait_for(self._call_embed(embed, [query[:1000]], "query"),
                                             timeout=self.embed_timeout)
            vec = np.asarray(vectors[0], dtype="float32")
            norm = float(np.linalg.norm(vec))
            if vec.ndim != 1 or norm == 0:
                raise ValueError("пустой вектор запроса")
            vec = vec / norm
        except Exception as error:
            self._embed_fail_until = time.monotonic() + EMBED_COOLDOWN
            logger.warning("Эмбеддинг запроса не получен, ищем лексикой (%s)", type(error).__name__)
            return None
        self._qcache[key] = vec
        while len(self._qcache) > EMBED_CACHE_QUERIES:
            self._qcache.popitem(last=False)
        return vec

    # ------------------------------------------------------------------ поиск
    def _territory_factor(self, doc: Doc, wanted: set[str], wanted_up: set[str]) -> float:
        if not wanted or not doc.territory_ids:
            return 1.0
        d = doc.territory_ids
        if d & wanted:
            return BOOST_EXACT
        if d & wanted_up:               # документ уровнем выше (район для села)
            return BOOST_RELATED
        for t in d:
            if self._ancestors(t) & wanted:   # документ внутри запрошенного района
                return BOOST_RELATED
        return PENALTY_FOREIGN

    def _wanted_territories(self, q: _Query, hint: str) -> tuple[set[str], set[str]]:
        spans = self._find_territories(q.words, q.keys)
        if spans:
            # Слова названия, которых нет в справочнике («Кочкор-Ате» -> «ате»), не
            # должны снижать оценку: территорию учтём бустом, а не словом.
            named = {w for a, b in spans.values() for w in q.words[a:b]}
            q.place_words = named
            kept = []
            for t in q.terms:
                if t.word in named:
                    if not t.alts:
                        continue
                    t.weight = TERRITORY_WORD_WEIGHT
                    t.place = True
                kept.append(t)
            q.terms = kept
        wanted = set(spans)
        if not wanted and hint:
            hint_words = query_words(hint)
            hint_keys = [self._index.resolve(w)[1] if self._index.postings else stem(w)
                         for w in hint_words]
            wanted = set(self._find_territories(hint_words, hint_keys))
        up: set[str] = set()
        for t in wanted:
            up |= self._ancestors(t)
        return wanted, up

    async def search(self, query: str, embed: EmbedFn | None = None,
                     territory_hint: str = "", limit: int = 5) -> list[Hit]:
        """Найти записи справочника под вопрос. Пусто — ничего релевантного.

        Никогда не бросает исключений: любой сбой = пустой список/лексика."""
        try:
            return await self._search(query, embed, territory_hint, limit)
        except Exception:
            logger.exception("Поиск по справочнику упал — отвечаем без справочника")
            return []

    async def _search(self, query: str, embed: EmbedFn | None, territory_hint: str, limit: int) -> list[Hit]:
        if not self.enabled or not query or len(query.strip()) < 2 or limit <= 0:
            return []
        query = query[:600]              # длинные простыни не нужны и тормозят разбор
        q = self._index.make_query(query, _SYN_GROUPS)
        if q.const_cue and not q.contact_intent:
            return []                    # вопрос про Конституцию — справочник не подмешиваем
        wanted, wanted_up = self._wanted_territories(q, territory_hint)
        q.topics = detect_topics(q.words, skip=q.place_words)
        lex, matched = self._index.score(q.terms) if q.terms else ({}, {})
        if any(not t.place for t in q.terms):
            # одно лишь название района («Майлуу-Суу») ничего не говорит о вопросе:
            # запись обязана содержать хотя бы одно слово по существу
            lex = {d: v for d, v in lex.items() if matched.get(d, 0) >= 1}
        lex_rest: dict[int, float] = {}
        if q.topics:
            # Слова-темы («воды», «яма») работают через принадлежность записи теме. Для
            # записей чужих тем считаем только остальные содержательные слова — иначе
            # «паспорт земельного участка» отвечал бы на «как получить паспорт».
            rest = [t for t in q.terms if not t.topic and not t.place]
            lex_rest = self._index.score(rest)[0] if rest else {}
        qvec = await self._embed_query(query, embed) if self._vecs is not None else None
        sems = None
        if qvec is not None and self._vecs is not None and self._vecs.shape[1] == qvec.shape[0]:
            sems = self._vecs @ qvec
        if not lex and sems is None and not q.topics:
            return []
        # Чисто «смысловой» поиск по запросу из одних стоп-слов («привет») даёт мусор:
        # без единого содержательного слова эмбеддинг не используем.
        if not q.terms:
            sems = None
            if not lex and not q.topics:
                return []

        fallback = bool(wanted) and any(_TOPIC_BY_NAME[t].communal for t in q.topics)
        cands = set(lex)
        for t in q.topics:
            cands.update(self._by_topic.get(t, ()))
        if fallback:
            cands.update(self._admin_idx)
        if sems is not None:
            top = np.argpartition(-sems, min(40, len(sems) - 1))[:40]
            cands |= {int(i) for i in top}

        # темы вроде «документы» без названного района бессмысленны: ЦОН есть не везде
        place_only = all(_TOPIC_BY_NAME[t].needs_place for t in q.topics) if q.topics else False
        lo, hi = self._cos
        scored: list[Hit] = []
        for i in cands:
            doc = self._docs[i]
            factor = self._territory_factor(doc, wanted, wanted_up)
            on_topic = bool(doc.topics & q.topics) and (bool(wanted) or not place_only)
            l_score = lex.get(i, 0.0) if (on_topic or not q.topics) else lex_rest.get(i, 0.0)
            # Базовая оценка по теме: маршрут темы надёжнее профильной организации, а
            # местная администрация — запасной исполнитель коммунальных жалоб.
            tbase = 0.0
            if on_topic:
                tbase = TOPIC_BASE.get(doc.kind, 0.55)
            elif fallback and doc.admin and doc.territory_ids and doc.territory_ids <= (wanted | wanted_up):
                tbase = TOPIC_ADMIN_FALLBACK
            if tbase and wanted and factor < 1.0:
                tbase = 0.0              # тема та, а район чужой
            s_raw = None
            lex_min = CONST_DIRECTORY_MIN if q.const_cue else (
                MIN_SCORE_LEX if (on_topic or not q.topics) else TOPIC_LEX_MIN)
            lex_ok = l_score >= lex_min
            sem_ok = False
            score = l_score if lex_ok else 0.0
            if sems is not None:
                s_raw = float(sems[i])
                s_scaled = min(1.0, max(0.0, (s_raw - lo) / (hi - lo)))
                mixed = WEIGHT_LEX * l_score + WEIGHT_SEM * s_scaled
                # Смысл только ДОБАВЛЯЕТ находки: что проходило по словам, проходит и
                # дальше (эмбеддинги кыргызского слабее русских), а запись без общих
                # слов проходит лишь при очень высоком косинусе.
                sem_ok = mixed >= MIN_SCORE_HYBRID and (
                    l_score >= MIN_LEX_IN_HYBRID or s_scaled >= MIN_SEM_IN_HYBRID)
                if sem_ok:
                    score = max(score, mixed)
            if tbase:
                score = max(score, tbase) + (0.05 * l_score if lex_ok else 0.0)
            if not (lex_ok or sem_ok or tbase):
                continue
            threshold = min(MIN_SCORE_LEX if lex_ok else 9.0, MIN_SCORE_HYBRID if sem_ok else 9.0,
                            0.0 if tbase else 9.0)
            # Порог проверяем до бустов, но после штрафа: чужая территория не должна
            # проходить на слабом совпадении. Буст только поднимает порядок.
            if score * min(factor, 1.0) < threshold:
                continue
            if q.contact_intent:
                # человек просит телефон/адрес: ему нужна карточка, а не порядок обращения
                if doc.kind == "org":
                    factor *= BOOST_CONTACT_INTENT
                elif doc.kind in ("procedure", "chunk", "qa"):
                    factor *= 0.85
            final = score * factor * doc.prior
            scored.append(Hit(doc=doc, score=final, lex=l_score, sem=s_raw))
        return self._select(scored, limit, bool(wanted))

    def _promote_cards(self, scored: list[Hit]) -> list[Hit]:
        """Фрагмент «компетенция организации» сам по себе не говорит, куда идти:
        вместо него берём карточку этой организации (с адресом и телефонами)."""
        best: dict[str, Hit] = {h.doc.id: h for h in scored}
        out: list[Hit] = []
        for h in scored:
            doc = h.doc
            card = None
            if doc.kind == "chunk" and doc.group.startswith("chunk:derived") and doc.organization_ids:
                card = self._cards.get(doc.organization_ids[0])
            if card is None:
                out.append(h)
                continue
            replacement = Hit(doc=card, score=h.score * 0.98, lex=h.lex, sem=h.sem)
            other = best.get(card.id)
            if other is None:
                best[card.id] = replacement
                out.append(replacement)
            elif replacement.score > other.score:
                other.score, other.lex, other.sem = replacement.score, replacement.lex, replacement.sem
        return out

    def _select(self, scored: list[Hit], limit: int, has_territory: bool) -> list[Hit]:
        """Отсев хвоста, дублей и однотипных записей."""
        scored = self._promote_cards(scored)
        scored.sort(key=lambda h: (-h.score, h.doc.id))
        if not scored:
            return []
        top = scored[0].score
        # Что уже представлено карточкой организации или маршрутом: фрагменты и
        # вопросы-ответы про те же организации только повторили бы то же самое.
        cards: set[str] = set()
        covered: set[str] = set()
        for h in scored:
            if h.doc.kind == "org":
                cards.update(h.doc.organization_ids)
            if h.doc.kind in ("org", "route"):
                covered.update(h.doc.organization_ids)
        route_cap = 3 if has_territory else 2
        per_group: dict[str, int] = defaultdict(int)
        chunk_orgs: set[tuple] = set()
        routes = 0
        out: list[Hit] = []
        for h in scored:
            if h.score < top * RELATIVE_CUTOFF:
                break
            doc = h.doc
            orgs = doc.organization_ids
            if doc.kind == "chunk":
                if doc.group.startswith("chunk:derived") and orgs and all(o in covered for o in orgs):
                    continue
                if orgs:
                    if orgs in chunk_orgs:
                        continue        # один фрагмент на организацию достаточно
                    chunk_orgs.add(orgs)
            elif doc.kind == "qa" and orgs and all(o in cards for o in orgs):
                continue
            elif doc.kind == "route":
                # без названного района десятки одинаковых «вода → …» бесполезны
                per_group[doc.group] += 1
                routes += 1
                if routes > route_cap or per_group[doc.group] > route_cap:
                    continue
            out.append(h)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ Конституция
    _ARTICLE_RE = (
        re.compile(r"(?<![а-я])(?:стат(?:я|и|е|ю|ей)|ст)\s*\.?\s*(?:№|n)?\s*(\d{1,3})"),
        re.compile(r"(\d{1,3})\s*[- ]?\s*(?:берен\w*|стат(?:я|и|е|ю|ей))"),
        re.compile(r"берен\w*\s*(?:№)?\s*(\d{1,3})"),
    )
    _KY_MARKERS = _norm_set("берене беренеси беренеге конституциясы конституциянын укук укугу".split())

    def _guess_lang(self, query: str) -> str:
        raw = unicodedata.normalize("NFKC", query).casefold()
        if any(ch in raw for ch in "өүң"):
            return "ky"
        if any(w in self._KY_MARKERS for w in tokenize(query)):
            return "ky"
        return "ru"

    def _explicit_article(self, query: str) -> int | None:
        text = normalize(query)
        if any(w in text for w in ("кодекс", "кодексин")):
            return None          # «статья 41 Трудового кодекса» — не Конституция
        for rx in self._ARTICLE_RE:
            m = rx.search(text)
            if m:
                return int(m.group(1))
        return None

    async def search_constitution(self, query: str, embed: EmbedFn | None = None,
                                  limit: int = 2) -> list[Hit]:
        """Статьи Конституции — только при явной ссылке («статья 41», «41-берене»)
        или очень высокой релевантности. Иначе пусто. Исключений наружу нет."""
        try:
            return await self._search_constitution(query, embed, limit)
        except Exception:
            logger.exception("Поиск по Конституции упал")
            return []

    async def _search_constitution(self, query: str, embed: EmbedFn | None, limit: int) -> list[Hit]:
        if not self._const_docs or not query or limit <= 0:
            return []
        langs = {d.lang for d in self._const_docs}
        lang = self._guess_lang(query)
        if lang not in langs:
            lang = sorted(langs)[0]
        number = self._explicit_article(query)
        if number is not None:
            doc = self._const_by_article.get((lang, number))
            if doc is None:
                other = [d for (lg, n), d in self._const_by_article.items() if n == number]
                doc = other[0] if other else None
            return [Hit(doc=doc, score=1.0, lex=1.0)] if doc else []

        q = self._const_index.make_query(query, _SYN_GROUPS, topics=False)
        # Слова вроде «конституция/статья» есть везде и ничего не различают; но их
        # наличие говорит, что человек спрашивает именно про Конституцию.
        cue = q.const_cue
        lex, matched = self._const_index.score(q.terms) if q.terms else ({}, {})
        need = 1 if cue else 2           # без явной отсылки одного совпавшего слова мало
        lex = {d: v for d, v in lex.items() if matched.get(d, 0) >= need}
        qvec = await self._embed_query(query, embed) if self._const_vecs is not None else None
        sems = None
        if qvec is not None and self._const_vecs is not None and self._const_vecs.shape[1] == qvec.shape[0]:
            sems = self._const_vecs @ qvec
        if not q.terms:
            sems = None
        min_lex = CONST_MIN_SCORE_LEX_CUE if cue else CONST_MIN_SCORE_LEX
        best: dict[int, Hit] = {}            # номер статьи -> лучшая версия (ru или ky)
        for i, doc in enumerate(self._const_docs):
            l_score = lex.get(i, 0.0)
            s_raw = None
            score = l_score if l_score >= min_lex else 0.0
            if sems is not None:
                s_raw = float(sems[i])
                scaled = min(1.0, max(0.0, (s_raw - COS_LOW) / (COS_HIGH - COS_LOW)))
                mixed = WEIGHT_LEX * l_score + WEIGHT_SEM * scaled
                if mixed >= CONST_MIN_SCORE_HYBRID and s_raw >= CONST_MIN_SEM:
                    score = max(score, mixed)
            if score <= 0:
                continue
            if doc.lang == lang:
                score *= 1.02                # при равенстве — на языке вопроса
            hit = Hit(doc=doc, score=score, lex=l_score, sem=s_raw)
            if doc.article not in best or hit.score > best[doc.article].score:
                best[doc.article] = hit
        return sorted(best.values(), key=lambda h: -h.score)[:limit]

    # ------------------------------------------------------------------ вывод
    def format_context(self, hits: list[Hit], arts: list[Hit] | None = None,
                       max_chars: int = 2400) -> str:
        """Компактный блок для промпта. Пусто — если нечего показать.
        Длина строго ≤ max_chars, режем по целым пунктам."""
        arts = arts or []
        if not hits and not arts:
            return ""
        art_block = ""
        if arts:
            lines = []
            for a in arts:
                d = a.doc
                label = "Статья" if d.lang != "ky" else "Берене"
                head = f"{label} {d.article}" if d.article else ("Преамбула" if d.lang != "ky" else "Кириш сөз")
                lines.append(f"- {head}: {_clip(d.show, 600)}")
            art_block = "Конституция КР:\n" + "\n".join(lines)
            if len(art_block) > max_chars * 0.45:
                art_block = _clip(art_block, int(max_chars * 0.45))
        header = "Справочник (проверенные данные; используй только то, что относится к вопросу):"
        budget = max_chars - len(art_block) - (1 if art_block else 0)
        parts = [header] if hits else []
        used = len(header) if hits else 0
        for n, h in enumerate(hits, 1):
            line = f"{n}. {h.doc.show}"
            extra = len(line) + 1
            if used + extra > budget:
                if n == 1:       # хотя бы первая запись должна влезть — подрежем
                    room = budget - used - 1
                    if room > 60:
                        parts.append(_clip(line, room))
                        used = budget
                break
            parts.append(line)
            used += extra
        if hits and len(parts) == 1:
            parts = []           # одна «шапка» без записей бессмысленна
        if art_block:
            parts.append(art_block)
        text = "\n".join(parts)
        return text[:max_chars]

    def candidate_organizations(self, hits: list[Hit], limit: int = 5) -> list[dict]:
        """Организации-кандидаты в исполнители обращения (для CRM): уникальные,
        по убыванию релевантности найденных записей."""
        out: list[dict] = []
        seen: set[str] = set()
        for h in sorted(hits, key=lambda x: -x.score):
            for oid in h.doc.organization_ids:
                if oid in seen or oid not in self._orgs:
                    continue
                seen.add(oid)
                out.append({"id": oid, "name": self._orgs[oid]["name"]})
                if len(out) >= limit:
                    return out
        return out

    def stats(self) -> dict:
        kinds: dict[str, int] = defaultdict(int)
        for d in self._docs:
            kinds[d.kind] += 1
        return {
            "enabled": self.enabled,
            "version": self.version,
            "documents": len(self._docs),
            "by_kind": dict(kinds),
            "articles": len(self._const_docs),
            "vocabulary": len(self._index.postings),
            "semantic_ready": self._vecs is not None,
            "load_seconds": round(self.load_seconds, 3),
        }


# ---------------------------------------------------------------------------
# Эмбеддер OpenAI и единый объект на процесс
# ---------------------------------------------------------------------------

def _openai_key() -> str:
    from config import settings
    from providers import override_for
    return override_for("openai").get("key") or settings.openai_api_key or ""


def _gemini_key() -> str:
    from config import settings
    from providers import override_for
    return override_for("gemini").get("key") or settings.google_api_key or ""


def make_openai_embedder(model: str | None = None) -> EmbedFn:
    """Асинхронная функция embed(texts) на openai.AsyncOpenAI.

    Ключ берём при каждом вызове: он может прийти из панели позже, чем запустился
    бот. Клиент кэшируем по ключу, чтобы не пересоздавать соединение."""
    cache: dict[str, object] = {}

    async def embed(texts: list[str]) -> list[list[float]]:
        from openai import AsyncOpenAI
        from config import settings

        key = _openai_key()
        if not key:
            raise RuntimeError("нет ключа OpenAI для эмбеддингов")
        client = cache.get(key)
        if client is None:
            cache.clear()
            client = AsyncOpenAI(api_key=key, timeout=20.0, max_retries=1)
            cache[key] = client
        response = await client.embeddings.create(  # type: ignore[attr-defined]
            model=model or settings.embedding_model, input=texts)
        data = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in data]

    try:
        from config import settings
        embed.model_name = model or settings.embedding_model    # type: ignore[attr-defined]
    except Exception:  # config недоступен (например, в тестах) — модель по умолчанию
        embed.model_name = model or "text-embedding-3-small"    # type: ignore[attr-defined]
    return embed


GEMINI_EMBED_MODELS = ("gemini-embedding-001", "text-embedding-004")
GEMINI_EMBED_DIM = 768       # родной размер 3072; 768 почти не теряет в качестве, а кэш в 4 раза меньше
GEMINI_BATCH = 40            # бесплатный тариф считает каждый текст пачки отдельным запросом
GEMINI_TEXTS_PER_MINUTE = 80 # лимит ≈100 запросов в минуту; держим запас


def make_gemini_embedder(model: str | None = None, dim: int = GEMINI_EMBED_DIM) -> EmbedFn:
    """Эмбеддер на Gemini (google-genai). Запасной, если ключ OpenAI не работает.

    Знает «задачу»: для документов и вопросов Gemini считает вектора по-разному
    (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY), это заметно улучшает поиск. Ключ берём
    при каждом вызове (может прийти из панели позже); клиент кэшируем по ключу.
    model_name входит в имя файла кэша, поэтому вектора Gemini и OpenAI не смешиваются."""
    cache: dict[str, object] = {}
    chosen = model or GEMINI_EMBED_MODELS[0]
    sent: list[tuple[float, int]] = []      # (когда, сколько текстов) — скользящее окно минуты

    async def throttle(n: int) -> None:
        """Не превышаем минутный лимит бесплатного тарифа: лучше подождать, чем получить 429."""
        while True:
            now = time.monotonic()
            sent[:] = [(t, c) for t, c in sent if now - t < 60.0]
            if sum(c for _, c in sent) + n <= GEMINI_TEXTS_PER_MINUTE or not sent:
                sent.append((now, n))
                return
            await asyncio.sleep(max(1.0, 60.0 - (now - sent[0][0])))

    async def embed(texts: list[str], task: str = "document") -> list[list[float]]:
        from google import genai
        from google.genai import types

        key = _gemini_key()
        if not key:
            raise RuntimeError("нет ключа Gemini для эмбеддингов")
        client = cache.get(key)
        if client is None:
            cache.clear()
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=30000))
            cache[key] = client
        config = types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY" if task == "query" else "RETRIEVAL_DOCUMENT",
            output_dimensionality=dim)
        last: Exception | None = None
        attempts = 1 if task == "query" else 6
        for attempt in range(attempts):  # лимиты бесплатного тарифа: повторы с паузой
            try:
                if task != "query":
                    await throttle(len(texts))
                response = await asyncio.wait_for(
                    client.aio.models.embed_content(model=chosen, contents=texts, config=config),  # type: ignore[attr-defined]
                    timeout=15.0 if task == "query" else 40.0)
                vectors = [list(e.values) for e in response.embeddings]
                if len(vectors) != len(texts):
                    raise ValueError("Gemini вернул не столько векторов, сколько текстов")
                return vectors
            except Exception as error:
                last = error
                code = getattr(error, "code", None) or getattr(error, "status_code", None)
                if attempt == attempts - 1 or code not in (429, 500, 503, None):
                    break
                await asyncio.sleep(20.0 if code == 429 else 3.0 * (attempt + 1))
        assert last is not None
        raise last

    embed.model_name = f"{chosen}-{dim}"     # type: ignore[attr-defined]
    embed.task_aware = True                  # type: ignore[attr-defined]
    embed.batch_size = GEMINI_BATCH          # type: ignore[attr-defined]
    return embed


class _AutoEmbedder:
    """Выбирает эмбеддер при первом использовании: OpenAI, если его ключ рабочий
    (одна дешёвая проба на запуск), иначе Gemini. Выбор не меняется до перезапуска,
    чтобы вектора справочника и вектора вопросов всегда были одной модели."""

    task_aware = True
    RETRY_AFTER = 300.0

    def __init__(self) -> None:
        self._impl: EmbedFn | None = None
        self._lock: asyncio.Lock | None = None
        self._next_probe = 0.0

    @property
    def model_name(self) -> str | None:
        return getattr(self._impl, "model_name", None) if self._impl else None

    @property
    def batch_size(self) -> int:
        return getattr(self._impl, "batch_size", EMBED_BATCH) if self._impl else EMBED_BATCH

    async def ensure(self) -> bool:
        if self._impl is not None:
            return True
        if time.monotonic() < self._next_probe:
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._impl is not None:
                return True
            candidates: list[EmbedFn] = []
            try:
                if _openai_key():
                    candidates.append(make_openai_embedder())
                if _gemini_key():
                    for name in GEMINI_EMBED_MODELS:
                        candidates.append(make_gemini_embedder(name))
            except Exception as error:
                logger.warning("Эмбеддеры: не удалось подготовить (%s)", type(error).__name__)
            for candidate in candidates:
                try:
                    probe = candidate(["проба"], task="query") if getattr(candidate, "task_aware", False) \
                        else candidate(["проба"])
                    vectors = await asyncio.wait_for(probe, timeout=15.0)
                    if vectors and len(vectors[0]) > 0:
                        self._impl = candidate
                        logger.info("Эмбеддинги справочника: модель %s", self.model_name)
                        return True
                except Exception as error:
                    logger.warning("Эмбеддер %s не работает (%s, статус %s)",
                                   getattr(candidate, "model_name", "?"), type(error).__name__,
                                   getattr(error, "status_code", None) or getattr(error, "code", "-"))
            self._next_probe = time.monotonic() + self.RETRY_AFTER
            return False

    async def __call__(self, texts: list[str], task: str = "document") -> list[list[float]]:
        if not await self.ensure():
            raise RuntimeError("нет рабочего эмбеддера (ни OpenAI, ни Gemini)")
        assert self._impl is not None
        if getattr(self._impl, "task_aware", False):
            return await self._impl(texts, task=task)    # type: ignore[call-arg]
        return await self._impl(texts)


def make_embedder() -> EmbedFn:
    """Эмбеддер для бота: OpenAI, если ключ работает, иначе Gemini.

    Возвращает вызываемый объект сразу (без сети); проба ключей происходит при первом
    использовании — в kb.prepare(). Нет ни одного рабочего ключа — вызовы бросают
    RuntimeError, а KnowledgeBase молча остаётся на лексическом поиске."""
    return _AutoEmbedder()  # type: ignore[return-value]


_INSTANCE: KnowledgeBase | None = None


def get_knowledge_base() -> KnowledgeBase:
    """Единый объект на процесс. Первое обращение читает справочник с диска."""
    global _INSTANCE
    if _INSTANCE is None:
        from config import BASE_DIR, settings
        kb = KnowledgeBase(
            data_dir=settings.knowledge_dir,
            constitution_dir=BASE_DIR / "data",
            cache_dir=settings.knowledge_cache_dir,
            model=settings.embedding_model,
        )
        kb.load()
        _INSTANCE = kb
    return _INSTANCE
