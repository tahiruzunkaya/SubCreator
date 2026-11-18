"""OCR metnini çeviriden önce temizler: sık OCR hataları ve konuşmacı adları."""
from __future__ import annotations

import re

_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "´": "'", "`": "'"})

# "every- thing" → "everything" (satır sonunda bölünmüş kelime birleşince arada boşluk kalır)
_HYPHEN_BREAK = re.compile(r"(?<=[a-z])- (?=[a-z])")
# Tek başına duran "|" veya "l" neredeyse her zaman "I" harfidir: "| know", "l'm"
_LONE_I = re.compile(r"(^|(?<=[\s\"(]))[|l](?=\s|'(?:m|ll|d|ve|s)\b|[,.!?]|$)")
_ONE_AS_I = re.compile(r"(^|(?<=[\s\"(]))1(?='(?:m|ll|d|ve)\b)")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")
_MULTI_SPACE = re.compile(r"\s{2,}")

# Konuşmacı: "JOHN:", "Ellie:", "Captain Price:" (en fazla 3 kelime, büyük harfle başlayan)
_SPEAKER = re.compile(r"^((?:[A-Z][A-Za-z0-9'.\-]*)(?:\s+[A-Z][A-Za-z0-9'.\-]*){0,2})\s*:\s+(\S.*)$")
# Konuşmacı gibi görünen ama çevrilmesi gereken yaygın kelimeler
_NOT_SPEAKERS = {"note", "warning", "tip", "hint", "objective", "new objective", "quest", "mission", "location"}


def fix_ocr(text: str) -> str:
    t = text.translate(_QUOTES)
    t = _HYPHEN_BREAK.sub("", t)
    t = _LONE_I.sub(r"\1I", t)
    t = _ONE_AS_I.sub(r"\1I", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = _MULTI_SPACE.sub(" ", t)
    return t.strip()


def split_speaker(text: str) -> tuple[str | None, str]:
    """Baştaki konuşmacı adını ayırır; ad çevrilmeden korunur."""
    m = _SPEAKER.match(text)
    if not m or m.group(1).lower() in _NOT_SPEAKERS:
        return None, text
    return m.group(1), m.group(2)


def join_speaker(speaker: str | None, translated: str) -> str:
    return f"{speaker}: {translated}" if speaker else translated


_DIALOG_DASH = re.compile(r"^[-–—]\s*")


def segment_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Altyazı satırlarını konuşmalara ayırır: [(önek, metin), ...].

    - "- Nereye?" / "- Burada!" gibi tireli satırlar veya "Ellie: ..." gibi konuşmacılı satırlar
      ayrı konuşma sayılır (ayrı çevrilir, ayrı satırda gösterilir).
    - Aksi halde birden fazla satıra bölünmüş tek cümle birleştirilir (çeviri kalitesi için).
    Önek ("- " veya "Ellie: ") çevrilmeden korunur.
    """
    segments: list[list[str]] = []  # [önek, metin]
    for raw in lines:
        line = fix_ocr(raw)
        if not line:
            continue
        dash = _DIALOG_DASH.match(line)
        if dash:
            segments.append(["- ", line[dash.end():]])
            continue
        speaker, body = split_speaker(line)
        if speaker:
            segments.append([f"{speaker}: ", body])
        elif segments:
            segments[-1][1] = fix_ocr(f"{segments[-1][1]} {line}")
        else:
            segments.append(["", line])
    return [(prefix, body) for prefix, body in segments if body]


def segments_text(segments: list[tuple[str, str]]) -> str:
    return "\n".join(prefix + body for prefix, body in segments)
