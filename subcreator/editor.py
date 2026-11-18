"""Canlı önizleme üzerinde altyazı bölgesini ve çeviri konumunu çizme/taşıma/boyutlandırma."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .config import Config
from .overlay import paint_fitted_box, paint_subtitle_box

REGION = "region"
OVERLAY = "overlay"
COLORS = {REGION: QColor(0, 170, 255), OVERLAY: QColor(255, 150, 0)}
LABELS = {REGION: "Altyazı (okunan alan)", OVERLAY: "Çeviri"}
HANDLE = 8  # kenar/köşe yakalama mesafesi (px)
MIN_SIZE = 8

_CURSORS = {
    frozenset("lt"): Qt.SizeFDiagCursor, frozenset("rb"): Qt.SizeFDiagCursor,
    frozenset("rt"): Qt.SizeBDiagCursor, frozenset("lb"): Qt.SizeBDiagCursor,
    frozenset("l"): Qt.SizeHorCursor, frozenset("r"): Qt.SizeHorCursor,
    frozenset("t"): Qt.SizeVerCursor, frozenset("b"): Qt.SizeVerCursor,
}


class RegionEditor(QWidget):
    region_edited = Signal(list)
    overlay_edited = Signal(list)
    tool_changed = Signal(str)
    double_clicked = Signal()

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.setMouseTracking(True)
        self.setMinimumSize(560, 315)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tool = REGION
        self.sample_text = "Çeviri burada görünecek"
        self.sample_layout = None  # son altyazının satır yerleşimi (önizlemede gerçeğe yakın çizim için)
        self._image: QImage | None = None
        self._client_w = 0
        self._message = "Uygulama bekleniyor…"
        self._rects: dict[str, list[float] | None] = {REGION: None, OVERLAY: None}
        self._overlay_custom = False
        self._drag: dict | None = None

    # ------------------------------------------------------------------ durum
    def sizeHint(self) -> QSize:
        return QSize(880, 495)

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.update()

    def set_frame(self, image: QImage, client_width: int) -> None:
        self._image = image
        self._client_w = client_width
        self._message = None
        self.update()

    def set_message(self, text: str) -> None:
        self._message = text
        self.update()

    def set_rects(self, region: list[float] | None, overlay: list[float] | None, overlay_custom: bool) -> None:
        self._rects = {REGION: region, OVERLAY: overlay}
        self._overlay_custom = overlay_custom
        self.update()

    # ------------------------------------------------------------- koordinat
    def _image_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        if self._image is not None and not self._image.isNull():
            iw, ih = self._image.width(), self._image.height()
        else:
            iw, ih = 16, 9
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        return QRectF((w - dw) / 2, (h - dh) / 2, dw, dh)

    def _to_widget(self, fr: list[float]) -> QRectF:
        ir = self._image_rect()
        return QRectF(ir.x() + fr[0] * ir.width(), ir.y() + fr[1] * ir.height(), fr[2] * ir.width(), fr[3] * ir.height())

    def _to_frac(self, r: QRectF) -> list[float]:
        ir = self._image_rect()
        r = r.intersected(ir)
        return [
            round((r.x() - ir.x()) / ir.width(), 4),
            round((r.y() - ir.y()) / ir.height(), 4),
            round(r.width() / ir.width(), 4),
            round(r.height() / ir.height(), 4),
        ]

    # ------------------------------------------------------------------ fare
    def _hit(self, pos: QPointF) -> tuple[str, str, frozenset] | None:
        order = [self.tool, OVERLAY if self.tool == REGION else REGION]
        for kind in order:
            fr = self._rects[kind]
            if not fr:
                continue
            r = self._to_widget(fr)
            grown = r.adjusted(-HANDLE, -HANDLE, HANDLE, HANDLE)
            if not grown.contains(pos):
                continue
            edges = set()
            if abs(pos.x() - r.left()) <= HANDLE:
                edges.add("l")
            elif abs(pos.x() - r.right()) <= HANDLE:
                edges.add("r")
            if abs(pos.y() - r.top()) <= HANDLE:
                edges.add("t")
            elif abs(pos.y() - r.bottom()) <= HANDLE:
                edges.add("b")
            if edges:
                return kind, "resize", frozenset(edges)
            if r.contains(pos):
                return kind, "move", frozenset()
        return None

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.LeftButton or self._image is None:
            return
        pos = e.position()
        hit = self._hit(pos)
        if hit:
            kind, mode, edges = hit
            if kind != self.tool:
                self.tool = kind
                self.tool_changed.emit(kind)
            self._drag = {"kind": kind, "mode": mode, "edges": edges, "start": pos,
                          "rect": self._to_widget(self._rects[kind]), "live": None}
        else:
            self._drag = {"kind": self.tool, "mode": "new", "start": pos, "live": None}

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._drag = None
            self.double_clicked.emit()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        d = self._drag
        if d is None:
            hit = self._hit(pos)
            if hit is None:
                self.setCursor(Qt.CrossCursor if self._image is not None else Qt.ArrowCursor)
            elif hit[1] == "move":
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(_CURSORS.get(hit[2], Qt.SizeAllCursor))
            return

        ir = self._image_rect()
        if d["mode"] == "new":
            r = QRectF(d["start"], pos).normalized()
        elif d["mode"] == "move":
            r = QRectF(d["rect"]).translated(pos - d["start"])
            dx = max(ir.left() - r.left(), 0) + min(ir.right() - r.right(), 0)
            dy = max(ir.top() - r.top(), 0) + min(ir.bottom() - r.bottom(), 0)
            r.translate(dx, dy)
        else:
            r = QRectF(d["rect"])
            if "l" in d["edges"]:
                r.setLeft(pos.x())
            if "r" in d["edges"]:
                r.setRight(pos.x())
            if "t" in d["edges"]:
                r.setTop(pos.y())
            if "b" in d["edges"]:
                r.setBottom(pos.y())
            r = r.normalized()
        d["live"] = r.intersected(ir)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        d = self._drag
        self._drag = None
        if e.button() != Qt.LeftButton or d is None:
            return
        r = d.get("live")
        if r is None or r.width() < MIN_SIZE or r.height() < MIN_SIZE:
            self.update()
            return
        fr = self._to_frac(r)
        self._rects[d["kind"]] = fr
        if d["kind"] == REGION:
            self.region_edited.emit(fr)
        else:
            self._overlay_custom = True
            self.overlay_edited.emit(fr)
        self.update()

    # ------------------------------------------------------------------ çizim
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(17, 17, 17))
        ir = self._image_rect()
        if self._image is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(ir, self._image)

        live_kind = self._drag["kind"] if self._drag and self._drag.get("live") is not None else None
        # Önce çeviri kutusu (altta), sonra okunan bölge çerçevesi (üstte).
        for kind in (OVERLAY, REGION):
            if kind == live_kind:
                r = self._drag["live"]
            elif self._rects[kind]:
                r = self._to_widget(self._rects[kind])
            else:
                continue
            self._paint_rect(p, kind, r)

        if self._message:
            p.fillRect(self.rect(), QColor(0, 0, 0, 150))
            self._paint_hint(p, self._message)
        elif self._image is not None and not self._rects[REGION] and live_kind != REGION:
            self._paint_hint(p, "Altyazının çıktığı alanı fareyle çiz  •  Çift tık: tam ekran")
        p.end()

    def _paint_rect(self, p: QPainter, kind: str, r: QRectF) -> None:
        color = COLORS[kind]
        ri = r.toRect()
        if kind == OVERLAY:
            # Önizleme küçültülmüş olduğu için yazı boyutunu da aynı oranda küçült.
            scale = self._image_rect().width() / self._client_w if self._client_w else 1.0
            if ri.width() > 4 and ri.height() > 4:
                # Altyazı bölgesi düzenlenirken alttaki orijinal yazı görünsün diye çeviri kutusunu soluklaştır.
                p.save()
                if self.tool == REGION:
                    p.setOpacity(0.35)
                max_px = max(8, int(self.cfg.max_font_px * scale))
                if self.sample_layout is not None and self.cfg.overlay_mode == "cover":
                    paint_fitted_box(p, ri, self.sample_text, self.cfg, self.sample_layout, max_px)
                else:
                    paint_subtitle_box(p, ri, self.sample_text, self.cfg, max_px)
                p.restore()
            pen = QPen(color, 2, Qt.SolidLine if self._overlay_custom else Qt.DashLine)
        else:
            fill = QColor(color)
            fill.setAlpha(35)
            p.fillRect(r, fill)
            pen = QPen(color, 2)
        p.setBrush(Qt.NoBrush)
        p.setPen(pen)
        p.drawRect(r)

        # Etiket
        font = QFont("Segoe UI")
        font.setPixelSize(12)
        font.setBold(True)
        p.setFont(font)
        label = LABELS[kind]
        tw = p.fontMetrics().horizontalAdvance(label) + 10
        tag = QRect(int(r.left()), int(r.top()) - 18, tw, 18)
        if tag.top() < 0:
            tag.moveTop(int(r.top()))
        p.fillRect(tag, color)
        p.setPen(QColor(0, 0, 0))
        p.drawText(tag, Qt.AlignCenter, label)

        # Aktif aracın köşe tutamaçları
        if kind == self.tool:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
                p.drawRect(QRectF(pt.x() - 4, pt.y() - 4, 8, 8))

    def _paint_hint(self, p: QPainter, text: str) -> None:
        font = QFont("Segoe UI")
        font.setPixelSize(18)
        p.setFont(font)
        metrics = p.fontMetrics()
        box = metrics.boundingRect(text).adjusted(-14, -8, 14, 8)
        box.moveCenter(self.rect().center())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(box, 8, 8)
        p.setPen(QColor(255, 255, 255))
        p.drawText(box, Qt.AlignCenter, text)
