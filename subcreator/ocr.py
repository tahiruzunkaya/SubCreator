from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import cv2
import numpy as np

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_LETTER = re.compile(r"[A-Za-z]")


def clean_text(text: str) -> str:
    text = _WS.sub(" ", text).strip(" |_~")
    return text if len(_LETTER.findall(text)) >= 2 else ""


@dataclass
class OcrLine:
    """Okunan bir satır ve bölge içindeki konumu (piksel, yakalanan karenin koordinatları)."""

    text: str
    left: float
    top: float
    right: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class OcrResult:
    lines: list[OcrLine] = field(default_factory=list)
    width: int = 0  # yakalanan karenin boyutu
    height: int = 0

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)

    def __bool__(self) -> bool:
        return bool(self.lines)

    def block(self) -> tuple[float, float, float, float] | None:
        """Tüm satırları kapsayan dikdörtgen (sol, üst, sağ, alt)."""
        if not self.lines:
            return None
        return (
            min(l.left for l in self.lines),
            min(l.top for l in self.lines),
            max(l.right for l in self.lines),
            max(l.bottom for l in self.lines),
        )

    def line_height(self) -> float:
        heights = sorted(l.height for l in self.lines)
        return heights[len(heights) // 2] if heights else 0.0


class SubtitleOCR:
    """RapidOCR (PaddleOCR modelleri, ONNX) ile altyazı bölgesini satır satır okur."""

    def __init__(self, min_confidence: float = 0.6) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self.min_confidence = min_confidence
        # Varsayılan ayarlar geniş/kısa altyazı şeridini kısa kenarı 736 px olacak şekilde
        # büyütüyor (≈800 ms). Uzun kenarı 960 px ile sınırlamak ≈50–110 ms'ye indiriyor.
        # Çok çekirdekli CPU'larda thread sayısını sınırlamak da aşırı yüklenmeyi önlüyor.
        self._engine = RapidOCR(
            det_limit_type="max",
            det_limit_side_len=960,
            intra_op_num_threads=min(8, os.cpu_count() or 4),
            inter_op_num_threads=1,
        )

    def read_lines(self, bgr: np.ndarray) -> OcrResult:
        h, w = bgr.shape[:2]
        scale = 1.0
        if h < 50:
            # Çok küçük bölgelerde büyütmek tanımayı iyileştirir.
            scale = 2.0
            bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        result, _ = self._engine(bgr, use_cls=False)
        out = OcrResult(width=w, height=h)
        if not result:
            return out

        items = []
        for box, text, score in result:
            if float(score) < self.min_confidence or not text.strip():
                continue
            xs = [p[0] / scale for p in box]
            ys = [p[1] / scale for p in box]
            items.append((min(ys), max(ys), min(xs), max(xs), text.strip()))
        for line in _group_lines(items):
            cleaned = clean_text(line.text)
            if cleaned:
                line.text = cleaned
                out.lines.append(line)
        return out

    def read(self, bgr: np.ndarray) -> str:
        """Tüm satırları tek metin olarak döndürür."""
        return self.read_lines(bgr).text


def _group_lines(items: list[tuple[float, float, float, float, str]]) -> list[OcrLine]:
    """Kutuları satırlara ayırır (yukarıdan aşağı), her satırı soldan sağa birleştirir."""
    items.sort(key=lambda t: (t[0] + t[1]) / 2)
    groups: list[dict] = []
    for top, bottom, left, right, text in items:
        cy, hh = (top + bottom) / 2, bottom - top
        if groups and abs(cy - groups[-1]["cy"]) < 0.5 * max(hh, groups[-1]["h"]):
            groups[-1]["parts"].append((left, right, top, bottom, text))
        else:
            groups.append({"cy": cy, "h": hh, "parts": [(left, right, top, bottom, text)]})
    lines = []
    for g in groups:
        parts = sorted(g["parts"])
        lines.append(OcrLine(
            text=" ".join(p[4] for p in parts),
            left=min(p[0] for p in parts),
            right=max(p[1] for p in parts),
            top=min(p[2] for p in parts),
            bottom=max(p[3] for p in parts),
        ))
    return lines
