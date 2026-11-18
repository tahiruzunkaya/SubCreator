from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QWidget

from . import winutil
from .config import Config, above_region, overlay_fracs, rects_overlap

log = logging.getLogger(__name__)


def fit_font(family: str, max_px: int, rect: QRect, text: str, flags) -> QFont:
    """Metni dikdörtgene sığdıran en büyük yazı boyutunu bulur (max_px'i geçmeden)."""
    font = QFont(family)
    font.setWeight(QFont.DemiBold)
    size = max(8, min(max_px, int(rect.height() * 0.45)))
    while size > 8:
        font.setPixelSize(size)
        if QFontMetrics(font).boundingRect(rect, flags, text).height() <= rect.height():
            break
        size -= 1
    font.setPixelSize(size)
    return font


def paint_subtitle_box(p: QPainter, rect: QRect, text: str, cfg: Config, max_px: int) -> None:
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, int(255 * cfg.bg_opacity)))
    radius = max(2, min(8, rect.height() // 6))
    p.drawRoundedRect(rect, radius, radius)
    pad_x = max(2, min(12, rect.width() // 40))
    inner = rect.adjusted(pad_x, 2, -pad_x, -2)
    flags = Qt.AlignCenter | Qt.TextWordWrap
    p.setFont(fit_font(cfg.font_family, max_px, inner, text, flags))
    p.setPen(QColor(255, 255, 255))
    p.drawText(inner, flags, text)


def paint_fitted_box(p: QPainter, area: QRect, text: str, cfg: Config, layout, max_px: int | None = None) -> None:
    """Çeviriyi orijinal altyazının yerine, onun satır yüksekliğine yakın yazıyla çizer.

    Kutu orijinal metnin tamamını örter, alttan hizalanır (altyazılar alta yaslıdır) ve çeviri daha
    uzunsa yukarı doğru büyür. Bölgeye sığmazsa yazı küçültülür.
    """
    W, H = area.width(), area.height()
    bx, by, bw, bh = layout.block
    block = QRectF(area.x() + bx * W, area.y() + by * H, bw * W, bh * H)
    line_px = max(8.0, layout.line_height * H)
    pad_x, pad_y = max(6.0, line_px * 0.5), max(3.0, line_px * 0.2)
    flags = Qt.AlignHCenter | Qt.TextWordWrap
    font = QFont(cfg.font_family)
    font.setWeight(QFont.DemiBold)
    # OCR kutusu yazıdan biraz yüksek; 0.9 ile orijinal yazı boyutuna yakın bir boyut elde edilir.
    size = int(min(max_px or cfg.max_font_px, max(10.0, line_px * 0.9)))
    avail_w = max(20, int(W - 2 * pad_x))
    while True:
        font.setPixelSize(size)
        br = QFontMetrics(font).boundingRect(QRect(0, 0, avail_w, 100_000), flags, text)
        if br.height() + 2 * pad_y <= H or size <= 10:
            break
        size -= 1

    box_w = min(W, max(br.width(), block.width()) + 2 * pad_x)
    box_h = min(H, max(br.height(), block.height()) + 2 * pad_y)
    left = min(max(area.x(), block.center().x() - box_w / 2), area.x() + W - box_w)
    bottom = min(area.y() + H, block.bottom() + pad_y)
    top = max(area.y(), bottom - box_h)
    box = QRectF(left, top, box_w, box_h)

    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, int(255 * cfg.bg_opacity)))
    radius = max(2.0, min(8.0, box_h / 6))
    p.drawRoundedRect(box, radius, radius)
    p.setFont(font)
    p.setPen(QColor(255, 255, 255))
    p.drawText(box, Qt.AlignCenter | Qt.TextWordWrap, text)


class SubtitleOverlay(QWidget):
    """Tıklamaları geçiren, ekran yakalamadan hariç tutulan şeffaf altyazı penceresi."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.cfg = cfg
        self.setWindowTitle("SubCreator Altyazı")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._text = ""
        self._layout = None
        self._client: winutil.Rect | None = None
        self._target_visible = False
        self._suspended = False
        self._preview_hidden = False
        # SUBCREATOR_NO_EXCLUDE=1: overlay'in ekran görüntüsünü alabilmek için (yalnızca hata ayıklama).
        self._exclude = not os.environ.get("SUBCREATOR_NO_EXCLUDE")
        self.excluded = self._exclude and winutil.exclude_from_capture(int(self.winId()))
        # Windows hariç tutmayı desteklemiyor (hata ayıklama için kapatılmış olması sayılmaz).
        self.exclusion_failed = self._exclude and not self.excluded
        # Kayıt modunda pencere içeriği okunamıyorsa çeviri okunan bölgenin dışına (üstüne) alınır.
        self._force_above = False
        self.set_capture_visible(cfg.show_in_recordings)

        # Tam ekran pencereler üste çıkabildiği için periyodik olarak en üste al.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self.apply_geometry)
        self._topmost_timer.start(1000)

        # Çevirinin ekranda kalış süresi (bekleme / en kısa gösterim) için gecikmeli gizleme.
        self._shown_at = 0.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_now)

    # --- Durum ---
    def show_subtitle(self, _original: str, translated: str, layout=None) -> None:
        self._hide_timer.stop()
        if translated != self._text:
            self._shown_at = time.monotonic()
        self._text = translated
        self._layout = layout
        self.update()
        self._refresh()

    def clear_subtitle(self, reason: str = "reset") -> None:
        """Çeviriyi kaldır.

        reason:
          "gone"     – orijinal altyazı kayboldu: `linger_ms` kadar bekle, `min_display_ms` dolmadan gizleme
          "replaced" – yeni altyazı geldi, çevirisi bekleniyor: yalnızca `min_display_ms` dolana kadar tut
          "reset"    – hedef/bölge değişti veya durduruldu: hemen gizle
        """
        if not self._text or reason == "reset":
            self._hide_now()
            return
        now = time.monotonic()
        until = self._shown_at + self.cfg.min_display_ms / 1000
        if reason == "gone":
            until = max(until, now + self.cfg.linger_ms / 1000)
        delay_ms = int((until - now) * 1000)
        if delay_ms <= 0:
            self._hide_now()
        else:
            self._hide_timer.start(delay_ms)

    def _hide_now(self) -> None:
        self._hide_timer.stop()
        self._text = ""
        self._refresh()

    def set_client(self, client: winutil.Rect) -> None:
        self._client = tuple(client)
        self.apply_geometry()

    def set_target_visible(self, visible: bool) -> None:
        self._target_visible = visible
        self._refresh()

    def set_suspended(self, suspended: bool) -> None:
        self._suspended = suspended
        self._refresh()

    def set_preview_hidden(self, hidden: bool) -> None:
        self._preview_hidden = hidden
        self._refresh()

    def set_capture_visible(self, visible: bool) -> None:
        """True: OBS/ekran görüntüsü çeviriyi görür. False: ekran yakalamadan hariç tutulur."""
        self._capture_visible = visible
        if self._exclude:
            winutil.set_capture_excluded(int(self.winId()), not visible)

    def set_force_above(self, force: bool) -> None:
        if force != self._force_above:
            self._force_above = force
            self.update()
            self.apply_geometry()

    def effective_fracs(self) -> list[float] | None:
        """Çevirinin gerçekte gösterileceği alan (kayıt modundaki zorunlu taşıma dahil)."""
        fracs = overlay_fracs(self.cfg)
        region = self.cfg.target().region
        if fracs and region and self._force_above and rects_overlap(fracs, region):
            return above_region(region)
        return fracs

    def _covers_original(self) -> bool:
        return self.cfg.overlay_mode == "cover" and not self._force_above

    def _refresh(self) -> None:
        should_show = (
            bool(self._text) and self._client is not None and self._target_visible and not self._suspended
            and not self._preview_hidden and self.effective_fracs() is not None
        )
        if should_show and not self.isVisible():
            self.show()
            self.set_capture_visible(self._capture_visible)
            self.apply_geometry()
        elif not should_show and self.isVisible():
            self.hide()

    def apply_geometry(self) -> None:
        if not self.isVisible() or self._client is None:
            return
        fracs = self.effective_fracs()
        if fracs is None:
            self.hide()
            return
        self.update()
        winutil.place_window(int(self.winId()), *winutil.region_to_rect(fracs, self._client))

    # --- Çizim ---
    def paintEvent(self, _event) -> None:
        if not self._text:
            return
        p = QPainter(self)
        if self._layout is not None and self._covers_original():
            # Orijinalin yerine, satır sayısı ve boyutuna uygun kutu
            paint_fitted_box(p, self.rect(), self._text, self.cfg, self._layout)
        else:
            paint_subtitle_box(p, self.rect(), self._text, self.cfg, self.cfg.max_font_px)
        p.end()
