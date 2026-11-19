"""Persian (colloquial) UI-review lexicon.

Rule-based on purpose: it is free, deterministic and testable. An optional
LLM pass (light tier) can refine low-confidence utterances later, but the
engine never depends on it.
"""

from __future__ import annotations

import re
import unicodedata

from ..model import Category, Severity

_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "ؤ": "و"})
_DIACRITICS = re.compile(r"[ً-ٰٟـ]")  # harakat, superscript alef, tatweel


def normalize(text: str) -> str:
    """Canonical form: Persian letters, no diacritics, ZWNJ → space, single spaces."""
    text = unicodedata.normalize("NFC", text).translate(_ARABIC_TO_PERSIAN)
    text = _DIACRITICS.sub("", text).replace("‌", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


# Words that point at something on screen. Their timestamp anchors the pointer lookup.
DEICTIC = {"این", "اینجا", "اون", "آن", "اونجا", "آنجا", "همین", "همینجا", "اینو", "اونو", "اینه", "اینا", "اینها", "this", "here", "that"}

# Category cues. Each cue is a normalized substring (colloquial and formal forms).
CUES: dict[Category, tuple[str, ...]] = {
    Category.PERFORMANCE: ("لگ", "کند", "کنده", "گیر میکنه", "گیر می کنه", "هنگ", "دیر", "سنگین", "طول میکشه",
                           "طول می کشه", "لود", "slow", "lag"),
    Category.ANIMATION: ("انیمیشن", "نرم نیست", "نرم نیس", "تند", "یهویی", "یهو", "پرش", "میپره", "می پره", "تکون",
                         "ترنزیشن", "حرکت", "smooth", "jank"),
    Category.INTERACTION: ("کار نمیکنه", "کار نمی کنه", "کار نمیکند", "کلیک", "نمیره", "نمی ره", "باز نمیشه",
                           "باز نمی شه", "خرابه", "خراب", "جواب نمیده", "جواب نمی ده", "هیچی نمیشه", "اتفاقی نمی افته",
                           "broken"),
    Category.LAYOUT: ("جانمایی", "قاطی", "تو هم", "درهم", "به هم ریخته", "نامرتب", "فاصله", "چسبیده", "زیادی", "اضافه", "اضافیه", "شلوغ", "کج", "تراز", "وسط نیست",
                      "بیرون زده", "جا نمیشه", "جا نمی شه", "پدینگ", "مارجین", "ردیف", "align"),
    Category.VISUAL: ("زشت", "گنده", "گونده", "خوشگل", "لوگو", "قشنگ تر", "شیک", "حرفه ای نیست", "قشنگ نیست", "رنگ", "فونت", "کمرنگ", "پررنگ", "تیره", "روشن", "سایه", "آیکون", "گرد",
                      "ضخیم", "نازک", "کوچیک", "کوچک", "بزرگ", "خوانا", "دیده نمیشه", "دیده نمی شه", "color", "font"),
    Category.CONTENT: ("متن", "توضیح", "منظور", "متوجه نمیشه", "متوجه نمی شه", "یعنی چی", "نوشتی", "غلط", "املا", "تایپو", "ترجمه", "نوشته", "عنوان", "typo"),
    Category.RTL: ("راست چین", "چپ چین", "برعکس", "جهت", "rtl", "ltr", "اعداد انگلیسی"),
    Category.ACCESSIBILITY: ("کنتراست", "دسترس", "صفحه خوان", "contrast"),
}

POSITIVE = ("خوبه", "عالیه", "قشنگه", "درسته", "اوکیه", "اوکی", "مشکلی نداره", "خوب شده")
INTENSIFIERS = ("خیلی", "اصلا", "افتضاح", "واقعا", "شدیدا", "فاجعه", "داغون")
CRITICAL = ("کلا کار نمیکنه", "کلا کار نمی کنه", "کرش", "سفید شد", "صفحه سفید", "ارور داد", "خطا داد")

# Voice commands (normalized). The value is the command id.
COMMANDS = {
    "ثبت کن": "record",
    "یادداشت کن": "record",
    "اولویت بالا": "priority_high",
    "مهمه": "priority_high",
    "مقایسه کن": "compare",
    "اینو نادیده بگیر": "ignore",
    "بیخیال": "ignore",
}

BASE_SEVERITY = {
    Category.INTERACTION: Severity.SEV2,
    Category.PERFORMANCE: Severity.SEV2,
    Category.RUNTIME: Severity.SEV1,
}


_CUE_CACHE: dict[str, re.Pattern[str]] = {}


def has_cue(norm: str, cue: str) -> bool:
    """Cue matches at a word start, so suffixed colloquial forms match ("لگه", "رنگش")
    but a cue hidden inside another word does not ("کند" in "نمیکند", "جا" in "اینجا")."""
    pattern = _CUE_CACHE.get(cue)
    if pattern is None:
        pattern = _CUE_CACHE[cue] = re.compile(r"(?<!\S)" + re.escape(normalize(cue)))
    return bool(pattern.search(norm))


def classify(text: str) -> tuple[Category | None, Severity, set[str], bool]:
    """Return (category, severity, commands, positive) for one utterance.

    ``category`` is None when the utterance carries no recognizable issue.
    """
    norm = normalize(text)
    commands = {cmd for phrase, cmd in COMMANDS.items() if has_cue(norm, phrase)}
    positive = any(has_cue(norm, p) for p in POSITIVE) and not any(has_cue(norm, n) for n in ("نیست", "نمی", "نه"))

    scores: dict[Category, int] = {}
    for cat, cues in CUES.items():
        # Multi-word cues are more specific ("چپ چین" beats the generic "متن").
        hits = sum(len(normalize(cue).split()) for cue in cues if has_cue(norm, cue))
        if hits:
            scores[cat] = hits
    category = max(scores, key=lambda c: (scores[c], -list(CUES).index(c))) if scores else None

    severity = BASE_SEVERITY.get(category, Severity.SEV3) if category else Severity.INFO
    if any(has_cue(norm, i) for i in INTENSIFIERS):
        severity = severity.bump()
    if any(has_cue(norm, c) for c in CRITICAL):
        severity = Severity.SEV1
    if "priority_high" in commands:
        severity = min(severity, Severity.SEV1, key=lambda s: s.rank)
    if positive and category is None:
        severity = Severity.INFO
    return category, severity, commands, positive


def is_deictic(word: str) -> bool:
    return normalize(word).strip("،,.!?؟") in DEICTIC
