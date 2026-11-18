"""PS5 olmadan test için: hareketli arka plan üstünde değişen İngilizce altyazılar gösteren pencere.

Çalıştır:  .venv\\Scripts\\python.exe scripts\\demo_window.py
Sonra:     .venv\\Scripts\\python.exe -m subcreator --process python --title "SubCreator Test" --select --no-save
"""
from __future__ import annotations

import math
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

LINES = [
    "Where did you hide the key? We need to leave before sunrise.",
    "",
    "I told you, the bridge is not safe anymore.",
    "Stay close to me and don't make a sound.",
    "",
    "We found the map, but the north gate is still locked.",
    # Çok satırlı örnekler: bölünmüş tek cümle, tireli diyalog, konuşmacılı 3 satır
    "If we don't reach the lighthouse before the storm hits,\nwe'll lose the signal for good.",
    "- Are you sure this is the right way?\n- Trust me, I've been here before.",
    "Ellie: What was that noise?\nJoel: Stay down and keep quiet.\nTess: They're coming this way.",
]


class DemoWindow(QWidget):
    def __init__(self, seconds_per_line: float) -> None:
        super().__init__()
        self.setWindowTitle("SubCreator Test Ekranı")
        self.resize(1280, 720)
        self.seconds_per_line = seconds_per_line
        self.start = time.monotonic()
        timer = QTimer(self)
        timer.timeout.connect(self.update)
        timer.start(33)

    def paintEvent(self, _e) -> None:
        t = time.monotonic() - self.start
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, self.width(), self.height())
        shift = (math.sin(t * 0.7) + 1) / 2
        g.setColorAt(0, QColor(30, int(60 + 80 * shift), 90))
        g.setColorAt(1, QColor(int(90 + 60 * shift), 50, 40))
        p.fillRect(self.rect(), g)
        # Hareketli "sahne" nesneleri
        for i in range(6):
            x = (self.width() * (i / 6) + t * 60 * (i + 1)) % self.width()
            p.setBrush(QColor(200, 180, 120, 90))
            p.setPen(Qt.NoPen)
            p.drawEllipse(int(x), int(self.height() * 0.55 + 40 * math.sin(t + i)), 120, 120)

        text = LINES[int(t / self.seconds_per_line) % len(LINES)]
        if text:
            font = QFont("Arial")
            font.setPixelSize(int(self.height() * 0.045))
            font.setBold(True)
            path = QPainterPath()
            metrics = QFontMetrics(font)
            rows = text.split("\n")
            line_h = metrics.height() * 1.05
            for i, row in enumerate(rows):
                w = metrics.horizontalAdvance(row)
                x = (self.width() - w) / 2
                # Son satır her zaman aynı yükseklikte; çok satırlılar yukarı doğru uzar.
                y = self.height() * 0.93 - (len(rows) - 1 - i) * line_h
                path.addText(x, y, font, row)
            p.setPen(QPen(QColor(0, 0, 0), 4))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255))
            p.drawPath(path)
        p.end()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3.0, help="Her altyazının ekranda kalma süresi")
    parser.add_argument("--top", action="store_true", help="Pencereyi her zaman üstte tut")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    w = DemoWindow(args.seconds)
    if args.top:
        w.setWindowFlag(Qt.WindowStaysOnTopHint)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
