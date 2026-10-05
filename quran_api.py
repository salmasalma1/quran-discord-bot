# -*- coding: utf-8 -*-
"""
External API layer:
- alquran.cloud  : ayahs, search, metadata
- aladhan.com    : prayer times
- mp3quran.net   : recitation files (URLs built from data/reciters.json)
"""

import difflib
import json
import os
import random
import re
from typing import Optional

import aiohttp

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ---------------------------------------------------------------- local data

with open(os.path.join(DATA_DIR, "surahs.json"), encoding="utf-8") as f:
    SURAH_LIST = json.load(f)

with open(os.path.join(DATA_DIR, "reciters.json"), encoding="utf-8") as f:
    RECITERS = json.load(f)

# The API stores the surah list as a string "1,2,3,..." — convert to ints
for _r in RECITERS:
    if isinstance(_r["surahs"], str):
        _r["surahs"] = [int(x) for x in _r["surahs"].split(",") if x.strip().isdigit()]

with open(os.path.join(DATA_DIR, "athkar.json"), encoding="utf-8") as f:
    ATHKAR = json.load(f)

# Strips Quranic diacritics/marks (vowel signs, maddah, pause marks, ...)
_DIACRITICS = re.compile(
    r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED\u08D3-\u08FF]"
)


def _normalize(text: str) -> str:
    """Normalize Arabic text: remove diacritics, unify alef/hamza/ta-marbuta."""
    t = _DIACRITICS.sub("", (text or "").strip())
    t = (t.replace("\u0671", "\u0627")  # alef wasla -> alef
           .replace("\u0623", "\u0627")  # alef hamza above
           .replace("\u0625", "\u0627")  # alef hamza below
           .replace("\u0622", "\u0627")  # alef madda
           .replace("\u0649", "\u064A")  # alef maksura -> ya
           .replace("\u0629", "\u0647"))  # ta marbuta -> ha
    return t.lower()


def _canon(text: str) -> str:
    """Simplified form for fuzzy matching: collapse letter repeats and
    unify e->i / o->u, so different transliterations still match
    (Yaseen/Yasin, Naas/Nas, Taha/Taa-Haa ...)."""
    t = _normalize(text).replace("-", "").replace(" ", "").replace("'", "")
    t = re.sub(r"(.)\1+", r"\1", t)
    return t.replace("e", "i").replace("o", "u")


SURAH_BY_NUMBER = {s["number"]: s for s in SURAH_LIST}

for _s in SURAH_LIST:
    _s["norm"] = _normalize(_s["name_ar"])
    _s["norm_en"] = _s["name_en"].lower().replace("-", "").replace(" ", "")
    _s["canon_ar"] = _canon(_s["name_ar"])
    _s["canon_en"] = _canon(_s["name_en"])

RECITER_BY_ID = {r["id"]: r for r in RECITERS}


def find_surah(query: str) -> Optional[dict]:
    """Find a surah by number or name (Arabic/English), tolerant to
    spelling differences and missing diacritics."""
    q = (query or "").strip()
    if not q:
        return None
    if q.isdigit():
        n = int(q)
        return SURAH_BY_NUMBER.get(n) if 1 <= n <= 114 else None
    q = re.sub(r"^\u0633\u0648\u0631\u0629\s+", "", q)  # strip Arabic word for "surah:" prefix
    nq = _normalize(q).replace("-", "").replace(" ", "").replace("'", "")
    if not nq:
        return None
    # 1) exact match
    for s in SURAH_LIST:
        if nq == s["norm"] or nq == s["norm_en"]:
            return s
    # 2) simplified match (ignores repeated letters and e-i variations)
    cq = _canon(q)
    for s in SURAH_LIST:
        if cq == s["canon_ar"] or cq == s["canon_en"]:
            return s
    if len(cq) >= 4:
        # 3) name starts with the typed word
        for s in SURAH_LIST:
            if s["canon_ar"].startswith(cq) or s["canon_en"].startswith(cq):
                return s
        # 4) partial containment
        for s in SURAH_LIST:
            if cq in s["canon_ar"] or cq in s["canon_en"]:
                return s
        # 5) fuzzy match for big transliteration differences
        best, best_score = None, 0.0
        for s in SURAH_LIST:
            score = max(
                difflib.SequenceMatcher(None, cq, s["canon_ar"]).ratio(),
                difflib.SequenceMatcher(None, cq, s["canon_en"]).ratio(),
            )
            if score > best_score:
                best, best_score = s, score
        if best is not None and best_score >= 0.8:
            return best
    return None


def audio_url(reciter_id: str, surah_number: int) -> Optional[str]:
    """Build the mp3 URL for a surah on an mp3quran.net server."""
    r = RECITER_BY_ID.get(reciter_id)
    if not r or surah_number not in r["surahs"]:
        return None
    return f'{r["server"]}{surah_number:03d}.mp3'


def default_reciter() -> dict:
    return RECITERS[0]


# ---------------------------------------------------------------- HTTP session

_session: Optional[aiohttp.ClientSession] = None


async def http_get_json(url: str, params: dict | None = None) -> dict:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Mozilla/5.0 (Discord Quran Bot)"},
        )
    async with _session.get(url, params=params) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


# ---------------------------------------------------------------- ayahs

async def get_ayah(surah_number: int, ayah_number: Optional[int] = None) -> Optional[dict]:
    """Return an ayah (Arabic + English translation). If no ayah number is
    given, pick a random one from the surah."""
    s = SURAH_BY_NUMBER.get(surah_number)
    if not s:
        return None
    if ayah_number is None:
        ayah_number = random.randint(1, s["ayahs"])
    global_number = _surah_ayah_to_global(surah_number, ayah_number)
    if global_number is None:
        return None
    data = await http_get_json(
        f"https://api.alquran.cloud/v1/ayah/{global_number}/editions/quran-uthmani,en.sahih"
    )
    if data.get("code") != 200:
        return None
    ar, en = data["data"][0], data["data"][1]
    return {
        "surah": ar["surah"]["number"],
        "surah_name": s["name_ar"],
        "surah_name_en": ar["surah"]["englishName"],
        "ayah": ar["numberInSurah"],
        "arabic": ar["text"],
        "translation": en["text"],
    }


async def random_ayah() -> dict:
    s = random.choice(SURAH_LIST)
    result = await get_ayah(s["number"])
    if result is None:  # rare fallback
        result = await get_ayah(1, 1)
    return result


def _surah_ayah_to_global(surah_number: int, ayah_number: int) -> Optional[int]:
    """Convert (surah, ayah-in-surah) to the global ayah number 1-6236."""
    if surah_number not in SURAH_BY_NUMBER:
        return None
    s = SURAH_BY_NUMBER[surah_number]
    if not 1 <= ayah_number <= s["ayahs"]:
        return None
    return sum(x["ayahs"] for x in SURAH_LIST if x["number"] < surah_number) + ayah_number


async def search_quran(query: str, limit: int = 10) -> dict:
    """Search the Quran text (Arabic). Returns {"count": n, "results": [...]}"""
    data = await http_get_json(
        f"https://api.alquran.cloud/v1/search/{query}/all/ar"
    )
    if data.get("code") != 200:
        return {"count": 0, "results": []}
    matches = data["data"]["matches"][:limit]
    return {
        "count": data["data"]["count"],
        "results": [
            {
                "surah": m["surah"]["number"],
                "surah_name": m["surah"]["name"],
                "ayah": m["numberInSurah"],
                "text": m["text"],
            }
            for m in matches
        ],
    }


# ---------------------------------------------------------------- prayer times

PRAYER_NAMES = {
    "Fajr": "Fajr",
    "Sunrise": "Sunrise",
    "Dhuhr": "Dhuhr",
    "Asr": "Asr",
    "Sunset": "Sunset",
    "Maghrib": "Maghrib",
    "Isha": "Isha",
    "Imsak": "Imsak",
    "Midnight": "Midnight",
}


async def prayer_times(city: str, country: str, method: int = 5) -> Optional[dict]:
    """Prayer times from aladhan.com. method=5 = Egyptian General Authority."""
    try:
        data = await http_get_json(
            "https://api.aladhan.com/v1/timingsByCity",
            params={"city": city, "country": country, "method": method},
        )
    except aiohttp.ClientResponseError:
        return None
    if data.get("code") != 200:
        return None
    d = data["data"]
    timings = {k: v.split(" ")[0] for k, v in d["timings"].items() if k in PRAYER_NAMES}
    hijri = d["date"]["hijri"]
    month_label = hijri["month"].get("ar") or hijri["month"]["en"]
    return {
        "timings": timings,
        "hijri": f'{hijri["day"]} {month_label} {hijri["year"]} AH',
        "gregorian": d["date"]["readable"],
    }
