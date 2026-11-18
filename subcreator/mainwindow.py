"""Ana ayar penceresi: okunacak uygulama, canlı önizleme üzerinde bölge/çeviri konumu ve ayarlar."""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import __version__, local_engine, models, winutil
from .config import LANGUAGES, Config, overlay_fracs
from .editor import COLORS, OVERLAY, REGION, RegionEditor

if TYPE_CHECKING:
    from .app import Controller

MAX_LOG_ITEMS = 100


def _tool_button(text: str, kind: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCheckable(True)
    c = COLORS[kind].name()
    btn.setStyleSheet(
        f"QPushButton {{ border: 2px solid {c}; border-radius: 6px; padding: 5px 12px; }}"
        f"QPushButton:checked {{ background: {c}; color: black; font-weight: bold; }}"
    )
    return btn


HELP_TEXT = "Sürükleyerek çiz  •  kutuyu taşı  •  kenarından boyutlandır"


class FullscreenPreview(QWidget):
    """Önizlemeyi tam ekranda gösterir; ana penceredeki düzenleyici buraya taşınır ve geri alınır."""

    closed = Signal()

    def __init__(self, main: "MainWindow") -> None:
        super().__init__(None, Qt.Window)
        self.setWindowTitle("SubCreator – Önizleme (tam ekran)")
        self.setWindowIcon(main.windowIcon())
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)

        bar = QHBoxLayout()
        self.region_tool = _tool_button("Altyazı bölgesi", REGION)
        self.overlay_tool = _tool_button("Çeviri konumu", OVERLAY)
        tools = QButtonGroup(self)
        tools.addButton(self.region_tool)
        tools.addButton(self.overlay_tool)
        self.region_tool.clicked.connect(lambda: main.set_tool(REGION))
        self.overlay_tool.clicked.connect(lambda: main.set_tool(OVERLAY))
        clear_btn = QPushButton("Bölgeyi temizle")
        clear_btn.clicked.connect(main.controller.clear_region)
        help_lbl = QLabel(HELP_TEXT + "  •  çift tık / Esc: tam ekrandan çık")
        help_lbl.setStyleSheet("color: gray;")
        exit_btn = QPushButton("Tam ekrandan çık (Esc)")
        exit_btn.clicked.connect(self.close)
        bar.addWidget(QLabel("Çiz:"))
        bar.addWidget(self.region_tool)
        bar.addWidget(self.overlay_tool)
        bar.addSpacing(12)
        bar.addWidget(clear_btn)
        bar.addStretch(1)
        bar.addWidget(help_lbl)
        bar.addSpacing(12)
        bar.addWidget(exit_btn)
        self._layout.addLayout(bar)

        QShortcut(QKeySequence(Qt.Key_Escape), self, self.close)
        QShortcut(QKeySequence(Qt.Key_F11), self, self.close)

    def set_editor(self, editor: RegionEditor) -> None:
        self._layout.addWidget(editor, 1)

    def sync_tool(self, tool: str) -> None:
        (self.region_tool if tool == REGION else self.overlay_tool).setChecked(True)

    def closeEvent(self, e) -> None:
        super().closeEvent(e)
        self.closed.emit()


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, controller: "Controller", icon) -> None:
        super().__init__()
        self.cfg = cfg
        self.controller = controller
        self.allow_close = False
        self._tray_hint_shown = False
        self._fullscreen: FullscreenPreview | None = None

        self.setWindowTitle(f"SubCreator {__version__} – Anlık Altyazı Çevirisi")
        self.setWindowIcon(icon)
        self.resize(1400, 880)

        root = QWidget()
        layout = QHBoxLayout(root)
        settings = QWidget()
        settings.setLayout(self._build_settings_column())
        settings.setFixedWidth(370)
        layout.addWidget(settings)
        layout.addLayout(self._build_live_column(), 1)
        self.setCentralWidget(root)

        self.refresh_targets()
        self.refresh_editor()
        QShortcut(QKeySequence(Qt.Key_F11), self, self.toggle_fullscreen)

    # ------------------------------------------------------------------ yapı
    def _build_settings_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)

        # 1. Kaynak uygulama
        src = QGroupBox("1. Okunacak uygulama")
        src_l = QVBoxLayout(src)
        row = QHBoxLayout()
        self.target_combo = QComboBox()
        self.target_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.target_combo.setMinimumContentsLength(20)
        self.target_combo.activated.connect(self._on_target_activated)
        refresh = QPushButton("Yenile")
        refresh.setToolTip("Açık pencere listesini yenile")
        refresh.clicked.connect(self.refresh_targets)
        row.addWidget(self.target_combo, 1)
        row.addWidget(refresh)
        src_l.addLayout(row)
        self.match_title_chk = QCheckBox("Pencere başlığını da eşleştir")
        self.match_title_chk.setToolTip(
            "Aynı programın birden fazla penceresi varsa (ör. tarayıcı) yalnızca bu başlıktaki pencereyi okur."
        )
        self.match_title_chk.setChecked(bool(self.cfg.target().window_title))
        self.match_title_chk.toggled.connect(lambda _c: self._on_target_activated(self.target_combo.currentIndex()))
        src_l.addWidget(self.match_title_chk)
        col.addWidget(src)

        # 2. Çeviri
        tr = QGroupBox("2. Çeviri")
        tr_l = QFormLayout(tr)
        self.lang_combo = QComboBox()
        for code, name in LANGUAGES.items():
            if code != self.cfg.source_lang:
                self.lang_combo.addItem(name, code)
        self._select_data(self.lang_combo, self.cfg.target_lang)
        self.lang_combo.currentIndexChanged.connect(self._on_settings_changed)
        tr_l.addRow("Altyazı dili:", QLabel(LANGUAGES.get(self.cfg.source_lang, self.cfg.source_lang)))
        tr_l.addRow("Çeviri dili:", self.lang_combo)
        self.engine_combo = QComboBox()
        self.engine_combo.setToolTip(
            "Çeviri bu bilgisayarın ekran kartında çalışan modelle yapılır:\n"
            "internet gerekmez, okunan metin dışarı gönderilmez."
        )
        self.refresh_engines()
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        tr_l.addRow("Çeviri modeli:", self.engine_combo)
        col.addWidget(tr)

        # 3. Görünüm
        look = QGroupBox("3. Çeviri konumu ve görünüm")
        look_l = QFormLayout(look)
        self.mode_cover = QRadioButton("Orijinal altyazının üstüne (kapat)")
        self.mode_above = QRadioButton("Orijinal altyazının hemen üstüne")
        self.mode_custom = QRadioButton("Önizlemede çizdiğim yere")
        self.mode_group = QButtonGroup(self)
        mode_box = QVBoxLayout()
        for btn in (self.mode_cover, self.mode_above, self.mode_custom):
            self.mode_group.addButton(btn)
            mode_box.addWidget(btn)
        self.mode_group.buttonToggled.connect(self._on_mode_toggled)
        look_l.addRow("Konum:", mode_box)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(14, 96)
        self.font_spin.setSuffix(" px")
        self.font_spin.setValue(self.cfg.max_font_px)
        self.font_spin.valueChanged.connect(self._on_settings_changed)
        look_l.addRow("En büyük yazı:", self.font_spin)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(round(self.cfg.bg_opacity * 100))
        self.opacity_slider.valueChanged.connect(self._on_settings_changed)
        look_l.addRow("Arka plan opaklığı:", self.opacity_slider)
        self.linger_spin = self._ms_spin(self.cfg.linger_ms)
        self.linger_spin.setToolTip(
            "Orijinal altyazı ekrandan kalktıktan sonra çevirinin ne kadar daha görüneceği.\n"
            "Çeviri orijinalden biraz geç geldiği için okumaya zaman tanır."
        )
        look_l.addRow("Kaybolunca bekleme:", self.linger_spin)
        self.min_display_spin = self._ms_spin(self.cfg.min_display_ms)
        self.min_display_spin.setToolTip(
            "Her çeviri, orijinal altyazı daha erken kalksa bile en az bu kadar ekranda kalır.\n"
            "Yeni bir çeviri gelirse beklemeden onun yerini alır."
        )
        look_l.addRow("En kısa gösterim:", self.min_display_spin)
        self.unfocused_chk = QCheckBox("Yalnızca uygulama öndeyken göster")
        self.unfocused_chk.setChecked(self.cfg.hide_when_unfocused)
        self.unfocused_chk.toggled.connect(self._on_settings_changed)
        look_l.addRow(self.unfocused_chk)
        self.recording_chk = QCheckBox("Kayıtta ve yayında göster (OBS, ekran görüntüsü)")
        self.recording_chk.setToolTip(
            "Açıkken çeviri OBS'in 'Ekran Yakalama' kaynağında, ekran görüntülerinde ve ekran paylaşımında görünür.\n"
            "Altyazı bu modda doğrudan uygulamanın pencere içeriğinden okunur (kendi çevirimizi okumamak için).\n"
            "Not: OBS'te yalnızca uygulamanın 'Pencere Yakalama'sını kullanırsan çeviri ayrı pencere olduğu için görünmez."
        )
        self.recording_chk.setChecked(self.cfg.show_in_recordings)
        self.recording_chk.toggled.connect(self._on_settings_changed)
        look_l.addRow(self.recording_chk)
        self.recording_hint = QLabel(
            "Bu uygulamanın pencere içeriği doğrudan okunamıyor. Kayıt modunda çeviri, okunan altyazının "
            "hemen üstünde gösteriliyor (aksi halde kendi çevirimizi okurduk)."
        )
        self.recording_hint.setWordWrap(True)
        self.recording_hint.setStyleSheet("color: #e0a030;")
        self.recording_hint.hide()
        look_l.addRow(self.recording_hint)
        col.addWidget(look)

        # Gelişmiş
        adv = QGroupBox("Gelişmiş")
        adv_l = QFormLayout(adv)
        self.conf_spin = QDoubleSpinBox()
        self.conf_spin.setRange(0.3, 0.95)
        self.conf_spin.setSingleStep(0.05)
        self.conf_spin.setValue(self.cfg.min_confidence)
        self.conf_spin.setToolTip("Düşük değer daha çok metin yakalar ama hatalı okuma artar.")
        self.conf_spin.valueChanged.connect(self._on_settings_changed)
        adv_l.addRow("OCR güven eşiği:", self.conf_spin)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(50, 1000)
        self.interval_spin.setSingleStep(10)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setValue(self.cfg.interval_ms)
        self.interval_spin.setToolTip("Ekranın kontrol edilme sıklığı. Düşük değer daha hızlı ama daha çok CPU.")
        self.interval_spin.valueChanged.connect(self._on_settings_changed)
        adv_l.addRow("Kontrol aralığı:", self.interval_spin)
        self.cache_btn = QPushButton()
        self.cache_btn.setToolTip(
            "Daha önce çevrilen cümleler saklanır ve tekrar geldiklerinde anında gösterilir. "
            "Çeviriler hatalı kaldıysa temizleyebilirsin."
        )
        self.cache_btn.clicked.connect(self._on_clear_cache)
        adv_l.addRow("Çeviri önbelleği:", self.cache_btn)
        col.addWidget(adv)

        col.addStretch(1)
        self.sync_mode_radios()
        return col

    def _build_live_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)

        prev_box = QGroupBox("Önizleme")
        prev_l = QVBoxLayout(prev_box)
        bar = QHBoxLayout()
        self.region_tool = _tool_button("Altyazı bölgesi", REGION)
        self.overlay_tool = _tool_button("Çeviri konumu", OVERLAY)
        tools = QButtonGroup(self)
        tools.setExclusive(True)
        tools.addButton(self.region_tool)
        tools.addButton(self.overlay_tool)
        self.region_tool.setChecked(True)
        self.region_tool.clicked.connect(lambda: self.set_tool(REGION))
        self.overlay_tool.clicked.connect(lambda: self.set_tool(OVERLAY))
        clear_btn = QPushButton("Bölgeyi temizle")
        clear_btn.clicked.connect(self.controller.clear_region)
        help_lbl = QLabel(HELP_TEXT)
        help_lbl.setStyleSheet("color: gray;")
        fs_btn = QPushButton("⛶  Tam ekran")
        fs_btn.setToolTip("Önizlemeyi tam ekranda aç (F11 veya önizlemeye çift tık)")
        fs_btn.clicked.connect(self.toggle_fullscreen)
        bar.addWidget(QLabel("Çiz:"))
        bar.addWidget(self.region_tool)
        bar.addWidget(self.overlay_tool)
        bar.addSpacing(12)
        bar.addWidget(clear_btn)
        bar.addStretch(1)
        bar.addWidget(help_lbl)
        bar.addSpacing(12)
        bar.addWidget(fs_btn)
        prev_l.addLayout(bar)

        self.editor = RegionEditor(self.cfg)
        self.editor.region_edited.connect(self.controller.set_region)
        self.editor.overlay_edited.connect(self.controller.set_overlay_region)
        self.editor.tool_changed.connect(self._sync_tool_buttons)
        self.editor.double_clicked.connect(self.toggle_fullscreen)
        self._prev_layout = prev_l
        prev_l.addWidget(self.editor, 1)
        self.region_label = QLabel()
        self.region_label.setStyleSheet("color: gray;")
        prev_l.addWidget(self.region_label)
        col.addWidget(prev_box, 5)

        log_box = QGroupBox("Son altyazılar")
        log_l = QVBoxLayout(log_box)
        self.log_list = QListWidget()
        self.log_list.setWordWrap(True)
        self.log_list.setAlternatingRowColors(True)
        log_l.addWidget(self.log_list)
        log_box.setMaximumHeight(190)
        col.addWidget(log_box, 1)

        row = QHBoxLayout()
        self.start_btn = QPushButton()
        self.start_btn.setCheckable(True)
        self.start_btn.setMinimumHeight(44)
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.start_btn.setFont(f)
        self.start_btn.toggled.connect(self.controller.set_running)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        row.addWidget(self.start_btn, 1)
        row.addWidget(self.status_label, 2)
        col.addLayout(row)
        self.set_running(False)
        return col

    # --------------------------------------------------------------- yardımcılar
    def _ms_spin(self, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 10000)
        spin.setSingleStep(100)
        spin.setSuffix(" ms")
        spin.setValue(value)
        spin.valueChanged.connect(self._on_settings_changed)
        return spin

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def set_tool(self, tool: str) -> None:
        self.editor.set_tool(tool)
        self._sync_tool_buttons(tool)

    def _sync_tool_buttons(self, tool: str) -> None:
        (self.region_tool if tool == REGION else self.overlay_tool).setChecked(True)
        if self._fullscreen is not None:
            self._fullscreen.sync_tool(tool)

    # ------------------------------------------------------------ tam ekran
    def toggle_fullscreen(self) -> None:
        if self._fullscreen is None:
            self._enter_fullscreen()
        else:
            self._fullscreen.close()

    def _enter_fullscreen(self) -> None:
        fs = FullscreenPreview(self)
        fs.closed.connect(self._exit_fullscreen)
        self._editor_index = self._prev_layout.indexOf(self.editor)
        self._prev_layout.removeWidget(self.editor)
        fs.set_editor(self.editor)
        self._fullscreen = fs
        self._sync_tool_buttons(self.editor.tool)
        screen = self.screen()
        if screen is not None:
            fs.move(screen.geometry().topLeft())
        fs.showFullScreen()
        fs.activateWindow()
        # Ekran yakalamaya düşülen uygulamalarda önizleme kendi penceremizi göstermesin.
        winutil.exclude_from_capture(int(fs.winId()))
        self.controller.set_fullscreen_preview(True)

    def _exit_fullscreen(self) -> None:
        fs = self._fullscreen
        if fs is None:
            return
        self._fullscreen = None
        self._prev_layout.insertWidget(self._editor_index, self.editor, 1)
        self.editor.show()
        fs.deleteLater()
        self.controller.set_fullscreen_preview(False)
        self.activateWindow()

    # ------------------------------------------------------------ hedef seçimi
    def refresh_targets(self) -> None:
        active = self.cfg.active_target.lower()
        combo = self.target_combo
        combo.blockSignals(True)
        combo.clear()

        entries: list[tuple[str, str, bool]] = []
        open_exes = set()
        for _hwnd, exe, title in winutil.list_windows():
            entries.append((exe, title, True))
            open_exes.add(exe.lower())
        for key, target in self.cfg.targets.items():
            if key.lower() not in open_exes:
                entries.append((target.process_name, target.window_title, False))
        entries.sort(key=lambda e: (e[0].lower() != active, not e[2], e[0].lower(), e[1].lower()))

        select = -1
        want_title = self.cfg.target().window_title.lower()
        for exe, title, is_open in entries:
            label = f"{exe}  —  {title}" if is_open else f"{exe}  —  (şu an açık değil, kayıtlı ayar)"
            combo.addItem(label, (exe, title))
            combo.setItemData(combo.count() - 1, label, Qt.ToolTipRole)
            if exe.lower() == active and select < 0 and (not want_title or want_title in title.lower()):
                select = combo.count() - 1
        if select < 0:
            for i in range(combo.count()):
                if combo.itemData(i)[0].lower() == active:
                    select = i
                    break
        combo.setCurrentIndex(max(0, select))
        combo.blockSignals(False)

    def _on_target_activated(self, index: int) -> None:
        if index < 0:
            return
        exe, title = self.target_combo.itemData(index)
        self.controller.change_target(exe, title if self.match_title_chk.isChecked() else "")

    # ----------------------------------------------------------------- ayarlar
    def sync_mode_radios(self) -> None:
        btn = {"above": self.mode_above, "custom": self.mode_custom}.get(self.cfg.overlay_mode, self.mode_cover)
        self.mode_group.blockSignals(True)
        btn.setChecked(True)
        self.mode_group.blockSignals(False)

    def _on_mode_toggled(self, button, checked: bool) -> None:
        if not checked:
            return
        mode = {self.mode_cover: "cover", self.mode_above: "above", self.mode_custom: "custom"}[button]
        self.controller.set_overlay_mode(mode)

    def refresh_engines(self) -> None:
        """Model listesi: indirilmiş modeller ve indirilebilecek olanlar ("… – indir")."""
        combo = self.engine_combo
        combo.blockSignals(True)
        combo.clear()
        downloaded = local_engine.available_models()
        names = {p.name.lower() for p in downloaded}
        for path in downloaded:
            combo.addItem(local_engine.model_label(path), f"local:{path.name}")
        for info in models.CATALOG:
            if info.file.lower() not in names:
                combo.addItem(f"{info.label} – indir ({info.size_text})", f"download:{info.file}")
        current = self.cfg.translator
        if combo.findData(current) < 0:
            model = local_engine.find_model(current.partition(":")[2])
            current = f"local:{model.name}" if model else None
        if current is not None:
            self._select_data(combo, current)
        else:
            combo.setCurrentIndex(-1)
            combo.setPlaceholderText("Model indirilmedi – listeden seç")
        combo.blockSignals(False)

    def _on_engine_changed(self, *_args) -> None:
        data = self.engine_combo.currentData()
        if isinstance(data, str) and data.startswith("download:"):
            # İndirme penceresini aç; iptal edilirse liste önceki seçime döner.
            self.controller.open_model_dialog(data.partition(":")[2])
            return
        self._on_settings_changed()

    def _on_settings_changed(self, *_args) -> None:
        cfg = self.cfg
        cfg.target_lang = self.lang_combo.currentData()
        engine = self.engine_combo.currentData()
        if isinstance(engine, str) and engine.startswith("local:"):
            cfg.translator = engine
        cfg.max_font_px = self.font_spin.value()
        cfg.bg_opacity = self.opacity_slider.value() / 100
        cfg.linger_ms = self.linger_spin.value()
        cfg.min_display_ms = self.min_display_spin.value()
        cfg.hide_when_unfocused = self.unfocused_chk.isChecked()
        cfg.show_in_recordings = self.recording_chk.isChecked()
        cfg.min_confidence = round(self.conf_spin.value(), 2)
        cfg.interval_ms = self.interval_spin.value()
        self.editor.update()
        self.controller.settings_changed()

    def set_recording_hint(self, visible: bool) -> None:
        self.recording_hint.setVisible(visible)

    def refresh_cache_button(self) -> None:
        self.cache_btn.setText(f"Temizle ({self.controller.cache_count()} cümle)")

    def _on_clear_cache(self) -> None:
        self.controller.clear_cache()
        self.refresh_cache_button()

    def refresh_editor(self) -> None:
        t = self.cfg.target()
        self.editor.set_rects(t.region, overlay_fracs(self.cfg), self.cfg.overlay_mode == "custom")
        if t.region:
            x, y, w, h = (round(v * 100) for v in t.region)
            self.region_label.setText(
                f"{t.process_name}: altyazı bölgesi soldan %{x}, üstten %{y}, genişlik %{w}, yükseklik %{h}"
            )
        else:
            self.region_label.setText(f"{t.process_name}: henüz altyazı bölgesi seçilmedi")

    # --------------------------------------------------------- canlı durumlar
    def set_running(self, running: bool) -> None:
        self.start_btn.blockSignals(True)
        self.start_btn.setChecked(running)
        self.start_btn.setText("■  Durdur" if running else "▶  Başlat")
        self.start_btn.blockSignals(False)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def show_preview(self, image: QImage, client_width: int) -> None:
        self.editor.set_frame(image, client_width)

    def preview_unavailable(self, message: str) -> None:
        self.editor.set_message(message)

    def add_log(self, original: str, translated: str, layout=None) -> None:
        self.editor.sample_text = translated
        self.editor.sample_layout = layout
        self.editor.update()
        item = QListWidgetItem(f"{translated}\n{original}")
        item.setToolTip(original)
        self.log_list.insertItem(0, item)
        while self.log_list.count() > MAX_LOG_ITEMS:
            self.log_list.takeItem(self.log_list.count() - 1)
        if self.isVisible():
            self.refresh_cache_button()

    # ------------------------------------------------- görünürlük ve kapanış
    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.controller.set_preview_enabled(True)
        self.refresh_cache_button()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.controller.set_preview_enabled(False)

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange:
            self.controller.set_preview_enabled(not self.isMinimized() and self.isVisible())

    def closeEvent(self, e: QCloseEvent) -> None:
        if self.allow_close:
            e.accept()
            return
        e.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.controller.notify(
                "SubCreator arka planda çalışmaya devam ediyor. Çıkmak için tepsideki TR simgesine sağ tıkla."
            )
