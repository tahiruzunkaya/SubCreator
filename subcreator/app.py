from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import local_engine
from .config import APP_DIR, Config, Target, above_region
from .mainwindow import MainWindow
from .modeldialog import ModelDialog
from .overlay import SubtitleOverlay
from .pipeline import SubtitleWorker
from .translate import TranslationCache

log = logging.getLogger("subcreator")
LOG_PATH = APP_DIR / "subcreator.log"
CACHE_PATH = APP_DIR / "translations.db"


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(0, 120, 215))
        p.setPen(Qt.NoPen)
        m = max(1, size // 32)
        p.drawRoundedRect(m, m, size - 2 * m, size - 2 * m, size * 0.2, size * 0.2)
        font = QFont("Segoe UI")
        font.setPixelSize(max(6, int(size * 0.47)))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        p.drawText(pm.rect(), Qt.AlignCenter, "TR")
        p.end()
        icon.addPixmap(pm)
    return icon


class Controller:
    def __init__(self, app: QApplication, cfg: Config, persist: bool) -> None:
        self.app = app
        self.cfg = cfg
        self.persist = persist
        self.icon = app_icon()
        self._model_dialog_open = False

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_now)

        self.overlay = SubtitleOverlay(cfg)
        if self.overlay.exclusion_failed and cfg.overlay_mode == "cover":
            log.warning("Overlay ekran yakalamadan hariç tutulamadı; çeviri altyazının üstüne konacak.")
            cfg.overlay_mode = "above"

        try:
            self.cache: TranslationCache | None = TranslationCache(CACHE_PATH)
        except Exception:
            log.exception("Çeviri önbelleği açılamadı, önbelleksiz devam ediliyor")
            self.cache = None

        self.worker = SubtitleWorker(cfg, self.cache)
        self._build_tray()
        self.main = MainWindow(cfg, self, self.icon)

        self.worker.subtitle.connect(self.overlay.show_subtitle)
        self.worker.subtitle.connect(self.main.add_log)
        self.worker.cleared.connect(self.overlay.clear_subtitle)
        self.worker.client_changed.connect(self.overlay.set_client)
        self.worker.target_visible.connect(self.overlay.set_target_visible)
        self.worker.status.connect(self._on_status)
        self.worker.preview.connect(self.main.show_preview)
        self.worker.preview_unavailable.connect(self.main.preview_unavailable)
        self._window_capture_ok = True
        self.worker.window_capture_supported.connect(self._on_window_capture_supported)

        self.worker.start()

    # ------------------------------------------------------------------ tepsi
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setToolTip("SubCreator")
        menu = QMenu()
        menu.addAction("Ayarları göster", self.show_main)
        self.run_action = QAction("Çeviriyi çalıştır", menu, checkable=True)
        self.run_action.toggled.connect(self.set_running)
        menu.addAction(self.run_action)
        menu.addSeparator()
        menu.addAction("Çıkış", self.quit)
        self.tray.setContextMenu(menu)
        self._menu = menu
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show_main()

    def show_main(self) -> None:
        self.main.showNormal()
        self.main.raise_()
        self.main.activateWindow()

    def notify(self, text: str) -> None:
        log.info(text)
        self.tray.showMessage("SubCreator", text, self.icon, 4000)

    def _on_status(self, text: str) -> None:
        self.tray.setToolTip(f"SubCreator – {text}")
        self.main.set_status(text)

    def set_preview_enabled(self, enabled: bool) -> None:
        self.worker.set_preview_enabled(enabled)

    def set_fullscreen_preview(self, on: bool) -> None:
        # Tam ekranda gerçek overlay önizlemenin üstünde süzülmesin; önizlemeyi daha keskin üret.
        self.overlay.set_preview_hidden(on)
        self.worker.preview_max_width = 2560 if on else 1280
        if on:
            self.worker.set_preview_enabled(True)

    # ----------------------------------------------------------------- ayarlar
    def settings_changed(self) -> None:
        self.overlay.set_capture_visible(self.cfg.show_in_recordings)
        self._update_recording_fallback()
        self.overlay.apply_geometry()
        self._save_timer.start()

    def _on_window_capture_supported(self, ok: bool) -> None:
        if ok != self._window_capture_ok:
            log.info("Hedef pencere içeriği doğrudan %s", "okunabiliyor" if ok else "okunamıyor (ekran yakalama)")
        self._window_capture_ok = ok
        self._update_recording_fallback()

    def _update_recording_fallback(self) -> None:
        """Kayıt modunda pencere içeriği okunamıyorsa, çeviri okunan alanın dışına (üstüne) alınır.

        Aksi halde ekrandan okurken kendi Türkçe yazımızı da okurduk.
        """
        force = self.cfg.show_in_recordings and not self._window_capture_ok
        self.overlay.set_force_above(force)
        self.main.set_recording_hint(force)

    def _save_now(self) -> None:
        if self.persist:
            try:
                self.cfg.save()
            except OSError:
                log.exception("Ayarlar kaydedilemedi")

    def _layout_changed(self) -> None:
        """Bölge veya çeviri konumu değişti: overlay'i, önizlemeyi güncelle ve kaydet."""
        self.main.refresh_editor()
        self.overlay.apply_geometry()
        self._save_timer.start()

    def change_target(self, process_name: str, window_title: str) -> None:
        key = process_name.lower()
        if key not in self.cfg.targets:
            self.cfg.targets[key] = Target(process_name)
        self.cfg.set_target(key, window_title)
        self.cfg.target().process_name = process_name
        self.overlay.clear_subtitle()
        self.worker.reset_state()
        self.main.preview_unavailable("Uygulama aranıyor…")
        self._layout_changed()

    def set_region(self, region: list[float]) -> None:
        self.cfg.target().region = region
        self.worker.reset_state()
        self._layout_changed()

    def set_overlay_region(self, region: list[float]) -> None:
        self.cfg.target().overlay_region = region
        if self.cfg.overlay_mode != "custom":
            self.cfg.overlay_mode = "custom"
            self.main.sync_mode_radios()
        self._layout_changed()

    def set_overlay_mode(self, mode: str) -> None:
        self.cfg.overlay_mode = mode
        target = self.cfg.target()
        if mode == "custom" and not target.overlay_region and target.region:
            target.overlay_region = above_region(target.region)
        self._layout_changed()

    def cache_count(self) -> int:
        try:
            return self.cache.count() if self.cache else 0
        except Exception:
            return 0

    def clear_cache(self) -> None:
        if self.cache is None:
            return
        self.cache.clear()
        self.worker.reset_translator()
        self.notify("Çeviri önbelleği temizlendi.")

    def clear_region(self) -> None:
        target = self.cfg.target()
        target.region = None
        target.overlay_region = None
        self.set_running(False)
        self.overlay.clear_subtitle()
        self.worker.reset_state()
        self._layout_changed()

    # ------------------------------------------------------------ çeviri modeli
    def has_model(self) -> bool:
        return bool(local_engine.available_models())

    def open_model_dialog(self, preselect: str | None = None, first_run: bool = False) -> bool:
        """Model indirme penceresi. Model seçildiyse/indirildiyse True."""
        if self._model_dialog_open:
            return False
        if local_engine.find_server() is None:
            self.notify("llama-server.exe bulunamadı: program klasöründeki llama\\ eksik. Zip'i yeniden aç.")
            return False
        self.show_main()
        self._model_dialog_open = True
        try:
            dialog = ModelDialog(self.main, preselect=preselect, first_run=first_run)
            ok = dialog.exec() == ModelDialog.Accepted and dialog.chosen is not None
        finally:
            self._model_dialog_open = False
        if ok:
            self.cfg.translator = f"local:{dialog.chosen}"
            self.worker.reset_translator()
            self._save_timer.start()
        self.main.refresh_engines()
        return ok

    def ensure_model(self) -> None:
        """İlk açılış: model yoksa indirme penceresini göster."""
        if not self.has_model():
            self.open_model_dialog(first_run=True)

    # ------------------------------------------------------------- çalıştırma
    def set_running(self, running: bool) -> None:
        if running and not self.has_model():
            if self.main.isVisible():
                QTimer.singleShot(0, self.open_model_dialog)
            else:
                self.notify("Çeviri modeli indirilmedi: ayarları aç ve modeli indir.")
            running = False
        if running and not self.cfg.target().region:
            self.notify("Önce önizlemede altyazı bölgesini çiz.")
            running = False
        self.worker.set_paused(not running)
        self.overlay.set_suspended(not running)
        self.main.set_running(running)
        self.run_action.blockSignals(True)
        self.run_action.setChecked(running)
        self.run_action.blockSignals(False)

    # ------------------------------------------------------------------ çıkış
    def quit(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_now()
        self.worker.stop()
        self.worker.wait(3000)
        local_engine.shutdown_all()
        if self.cache is not None:
            try:
                self.cache.close()
            except Exception:
                pass
        self.tray.hide()
        self.main.allow_close = True
        self.main.close()
        self.app.quit()


def _parse_region(text: str) -> list[float]:
    parts = [float(v) for v in text.split(",")]
    if len(parts) != 4 or not all(0 <= v <= 1 for v in parts):
        raise argparse.ArgumentTypeError("--region x,y,w,h biçiminde 0..1 arası oranlar olmalı")
    return parts


def _instance_name() -> str:
    """Aynı kullanıcı ve ayar klasörü için tek SubCreator (iki kopya iki overlay ve iki model demek)."""
    key = f"{getpass.getuser()}|{str(APP_DIR).lower()}"
    return "SubCreator-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _activate_existing(name: str) -> bool:
    """Çalışan bir kopya varsa penceresini öne getirmesini iste."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(500):
        return False
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: diğer kopya öne gelebilsin
    except Exception:
        pass
    sock.write(b"show\n")
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _listen_for_instances(name: str, controller: "Controller") -> QLocalServer:
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(name):
        QLocalServer.removeServer(name)
        server.listen(name)

    def on_connection() -> None:
        while server.hasPendingConnections():
            server.nextPendingConnection().deleteLater()
        controller.show_main()

    server.newConnection.connect(on_connection)
    return server


def _setup_logging(debug: bool) -> None:
    handlers: list[logging.Handler] = []
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:  # paketlenmiş (konsolsuz) exe'de stderr yok
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subcreator", description="Uygulama altyazılarını anlık çevirir.")
    parser.add_argument("--minimized", action="store_true", help="Pencereyi açmadan tepside başlat")
    parser.add_argument("--process", help="Bu oturum için hedef exe (ör. chiaki.exe)")
    parser.add_argument("--title", default="", help="Bu oturum için pencere başlığı filtresi")
    parser.add_argument("--region", type=_parse_region, help="Bu oturum için bölge: x,y,w,h (0..1)")
    parser.add_argument("--start", action="store_true", help="Çeviriyi hemen başlat")
    parser.add_argument("--no-save", action="store_true", help="Ayarları kaydetme")
    parser.add_argument("--always-visible", action="store_true", help="Uygulama önde değilken de göster")
    parser.add_argument("--mode", choices=["cover", "above", "custom"], help="Bu oturum için çeviri konumu")
    parser.add_argument("--show-in-recordings", action="store_true", help="Bu oturumda çeviri OBS'de görünsün")
    parser.add_argument("--engine", help="Bu oturum için çeviri modeli: local veya local:<model.gguf>")
    parser.add_argument("--debug", action="store_true", help="Ayrıntılı log")
    args = parser.parse_args(argv)

    _setup_logging(args.debug)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("SubCreator")
    app.setQuitOnLastWindowClosed(False)

    cfg = Config.load()
    persist = not args.no_save
    if args.process:
        key = args.process.lower()
        cfg.targets[key] = Target(args.process, args.title, args.region)
        cfg.active_target = key
        persist = False
    elif args.region is not None:
        cfg.target().region = args.region
        persist = False
    if args.always_visible:
        cfg.hide_when_unfocused = False
        persist = False
    if args.mode:
        cfg.overlay_mode = args.mode
        persist = False
    if args.show_in_recordings:
        cfg.show_in_recordings = True
        persist = False
    if args.engine:
        cfg.translator = args.engine
        persist = False

    # Oturuma özel seçeneklerle (test/hata ayıklama) açılan kopyalar bu kuralın dışında.
    instance = _instance_name()
    if persist and _activate_existing(instance):
        log.info("SubCreator zaten çalışıyor; açık olan kopyanın penceresi öne getirildi.")
        return 0

    controller = Controller(app, cfg, persist)
    if persist:
        controller.instance_server = _listen_for_instances(instance, controller)
    if not args.minimized:
        controller.show_main()
        QTimer.singleShot(300, controller.ensure_model)
    if args.start or (args.minimized and cfg.target().region):
        controller.set_running(True)
    log.info("SubCreator başladı. Ayarlar: %s", APP_DIR)
    return app.exec()
