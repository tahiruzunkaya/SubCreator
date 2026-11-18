"""Yakala → değişim tespiti → OCR → çeviri döngüsü (arka plan thread'i).

Ayarlar (`Config`) her turda yeniden okunur; arayüzden yapılan değişiklikler anında uygulanır.

Yakalama kaynağı:
- Hedef uygulama öndeyken: ekran (hızlı; overlay ekran yakalamadan hariç tutulduğu için görünmez).
- SubCreator veya başka bir pencere öndeyken: PrintWindow ile doğrudan pencere içeriği
  (önü kapalı olsa da doğru görüntü). Desteklenmezse ekrana düşer.
"""
from __future__ import annotations

import difflib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from . import winutil
from .capture import ScreenGrabber, WindowCapturer
from .config import Config
from .ocr import OcrResult, SubtitleOCR
from .local_engine import ModelMissing
from .textfix import segment_lines, segments_text
from .translate import TranslationCache, create_translator

log = logging.getLogger(__name__)

# Parlak (altyazı benzeri) piksel maskesinde değişen piksel oranı eşikleri.
CHANGE_THRESHOLD = 0.008  # son OCR'a göre bu kadar değiştiyse yeni altyazı olabilir
STABLE_THRESHOLD = 0.004  # ardışık iki kare arasında bundan az değişim = sabit
TEXT_LUMA = 170
PREVIEW_INTERVAL = 0.2
PREVIEW_MAX_WIDTH = 1280
# Çalışırken bu kadar süre çeviri istenmezse yerel sunucuyu yokla (çöktüyse yeniden başlatılır).
KEEPALIVE_SECONDS = 45
# Pencere içeriği bu kadar süre kesintisiz okunamazsa "desteklenmiyor" sayılır.
PW_FAIL_GRACE = 2.0


def text_mask(bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    small = cv2.resize(gray, (max(1, w // 4), max(1, h // 4)), interpolation=cv2.INTER_AREA)
    return (small > TEXT_LUMA).astype(np.float32)


def mask_diff(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    return float(np.mean(np.abs(a - b)))


def same_text(a: str, b: str) -> bool:
    """OCR'ın küçük oynamalarını (bir harf farkı vb.) aynı altyazı say."""
    if not a or not b:
        return a == b
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.92


@dataclass(frozen=True)
class SubtitleLayout:
    """Orijinal altyazının bölge içindeki yeri (0..1 oran): çeviri buna göre boyutlanır/konumlanır."""

    block: tuple[float, float, float, float]  # x, y, w, h
    line_height: float  # tek satır yüksekliği, bölge yüksekliğine oran
    lines: int


def make_layout(result: OcrResult) -> SubtitleLayout | None:
    block = result.block()
    if block is None or not result.width or not result.height:
        return None
    left, top, right, bottom = block
    w, h = result.width, result.height
    return SubtitleLayout(
        block=(left / w, top / h, (right - left) / w, (bottom - top) / h),
        line_height=result.line_height() / h,
        lines=len(result.lines),
    )


def touches_edge(result: OcrResult, margin: float = 2.0) -> bool:
    """Metin bölgenin üst/alt kenarına değiyorsa altyazı kesiliyor olabilir."""
    block = result.block()
    return block is not None and (block[1] <= margin or block[3] >= result.height - margin)


def compose(segments: list[tuple[str, str]], translations: list[str]) -> str:
    return "\n".join(prefix + tr for (prefix, _body), tr in zip(segments, translations))


def to_qimage(bgr: np.ndarray, max_width: int = PREVIEW_MAX_WIDTH) -> QImage:
    h, w = bgr.shape[:2]
    if w > max_width:
        bgr = cv2.resize(bgr, (max_width, max(1, round(h * max_width / w))), interpolation=cv2.INTER_AREA)
    bgr = np.ascontiguousarray(bgr)
    h, w = bgr.shape[:2]
    return QImage(bgr.data, w, h, bgr.strides[0], QImage.Format_BGR888).copy()


class SubtitleWorker(QThread):
    subtitle = Signal(str, str, object)  # orijinal, çeviri, yerleşim (SubtitleLayout veya None)
    cleared = Signal(str)  # neden: "gone" (altyazı kayboldu), "replaced" (yenisi geliyor), "reset"
    client_changed = Signal(object)  # hedef pencerenin istemci alanı (x, y, w, h), fiziksel piksel
    target_visible = Signal(bool)
    status = Signal(str)
    preview = Signal(QImage, int)  # tüm pencere görüntüsü, istemci genişliği (fiziksel px)
    preview_unavailable = Signal(str)
    window_capture_supported = Signal(bool)  # hedef pencerenin içeriği doğrudan okunabiliyor mu

    def __init__(self, cfg: Config, cache: TranslationCache | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.cache = cache
        self._stop = False
        self._paused = True
        self._reset = False
        self._preview_enabled = False
        self.preview_max_width = PREVIEW_MAX_WIDTH
        # Her yeni altyazıda artar; geç gelen (eskimiş) çeviriler bununla ayıklanır.
        self._latest_id = 0
        self._translator = None
        self._translator_key = None
        self._last_network = 0.0
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="translate")

    # --- Ana thread'den çağrılır ---
    def stop(self) -> None:
        self._stop = True

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def is_paused(self) -> bool:
        return self._paused

    def set_preview_enabled(self, enabled: bool) -> None:
        """Ayar penceresi açıkken önizleme kareleri üret; kapalıyken CPU harcama."""
        self._preview_enabled = enabled

    def reset_state(self) -> None:
        """Hedef pencere veya bölge değişti: pencereyi yeniden ara, mevcut altyazıyı unut."""
        self._reset = True

    def reset_translator(self) -> None:
        """Önbellek temizlendi: bellekteki çevirileri de unutmak için motoru yeniden oluştur."""
        self._translator_key = None
        self._reset = True

    # --- Çeviri (arka plan havuzu) ---
    def _ensure_translator(self):
        cfg = self.cfg
        key = (cfg.translator, cfg.source_lang, cfg.target_lang)
        if key != self._translator_key:
            self._translator = create_translator(*key, disk=self.cache)
            self._translator_key = key
            self._warm_up()
            return self._translator, True
        return self._translator, False

    def _warm_up(self) -> None:
        self._last_network = time.monotonic()
        translator = self._translator
        if translator is not None:
            self._executor.submit(translator.warm_up)

    def _start_translation(self, translator, text: str, segments, layout, ocr_ms: float) -> None:
        """Yeni altyazı: önbellekte varsa hemen göster, yoksa arka planda çevir."""
        self._latest_id += 1
        rid = self._latest_id
        bodies = [body for _prefix, body in segments]
        cached = translator.lookup_many(bodies)
        if cached is not None:
            result = compose(segments, cached)
            log.info("OCR %.0f ms, önbellek | %s → %s", ocr_ms, text, result)
            self.subtitle.emit(text, result, layout)
            return
        # Eski çeviri yeni İngilizce altyazının üstünde kalmasın (en kısa gösterim süresi dolunca).
        self.cleared.emit("replaced")
        self._last_network = time.monotonic()
        self._executor.submit(self._translate_job, translator, text, segments, layout, rid, ocr_ms)

    def _translate_job(self, translator, text: str, segments, layout, rid: int, ocr_ms: float) -> None:
        t0 = time.monotonic()
        bodies = [body for _prefix, body in segments]
        try:
            translated = [t or b for t, b in zip(translator.translate_many(bodies), bodies)]
        except Exception as exc:
            log.warning("Çeviri hatası: %s", exc)
            translated = [f"[çeviri yok] {b}" for b in bodies]
        if rid != self._latest_id:
            log.debug("Eskimiş çeviri atlandı: %s", text)
            return
        tr_ms = (time.monotonic() - t0) * 1000
        result = compose(segments, translated)
        log.info("OCR %.0f ms, çeviri %.0f ms | %s → %s", ocr_ms, tr_ms, text, result)
        self.subtitle.emit(text, result, layout)

    def _invalidate(self) -> None:
        self._latest_id += 1

    # --- Arka plan thread'i ---
    def run(self) -> None:
        self.status.emit("OCR modeli yükleniyor…")
        try:
            self._ensure_translator()  # çeviri modelini OCR modeli yüklenirken yükle
        except ModelMissing:
            log.info("Çeviri modeli henüz indirilmedi")
        except Exception:
            log.exception("Çeviri motoru oluşturulamadı")
        try:
            grabber = ScreenGrabber()
            window_capturer = WindowCapturer()
            ocr = SubtitleOCR(self.cfg.min_confidence)
        except Exception:
            log.exception("Başlatma hatası")
            self.status.emit("Başlatma hatası, log dosyasına bak")
            return
        try:
            self._loop(grabber, window_capturer, ocr)
        except Exception:
            log.exception("Beklenmeyen hata")
            self.status.emit("Beklenmeyen hata, log dosyasına bak")
        finally:
            grabber.close()
            window_capturer.close()
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self, grabber: ScreenGrabber, window_capturer: WindowCapturer, ocr: SubtitleOCR) -> None:
        hwnd = None
        last_lookup = 0.0
        last_client = None
        last_roi = None
        last_visible = None
        last_status = ""
        last_preview = 0.0
        printwindow_warned = False
        last_wc_supported = None
        pw_fail_since = None
        window_capture_ok = True
        prev_mask = None
        ocr_mask = None
        next_force = 0.0
        current = ""
        empty_count = 0
        edge_warn_until = 0.0

        def set_status(text: str) -> None:
            nonlocal last_status
            if text != last_status:
                last_status = text
                log.info(text)
                self.status.emit(text)

        def set_visible(v: bool) -> None:
            nonlocal last_visible
            if v != last_visible:
                last_visible = v
                self.target_visible.emit(v)

        while not self._stop:
            t0 = time.monotonic()
            cfg = self.cfg

            if self._reset:
                self._reset = False
                hwnd, last_lookup, last_client, last_roi = None, 0.0, None, None
                # yeni hedef pencere: okunabilirliği yeniden belirle
                last_wc_supported, pw_fail_since, window_capture_ok = None, None, True
                prev_mask = ocr_mask = None
                current, next_force = "", 0.0
                self._invalidate()
                self.cleared.emit("reset")

            target = cfg.target()
            if not winutil.is_window(hwnd) or t0 - last_lookup > 2.0:
                hwnd = winutil.find_target_window(target.process_name, target.window_title)
                last_lookup = t0
            if hwnd is None:
                set_visible(False)
                msg = f"{target.process_name} penceresi bulunamadı (açık ve simge durumunda değil mi?)"
                set_status(msg)
                if self._preview_enabled:
                    self.preview_unavailable.emit(msg)
                time.sleep(0.5)
                continue

            client = winutil.client_rect_screen(hwnd)
            if client is None or client[2] < 50 or client[3] < 50:
                set_visible(False)
                time.sleep(0.5)
                continue
            if client != last_client:
                last_client = client
                self.client_changed.emit(client)

            region = target.region
            roi = winutil.region_to_rect(region, client) if region else None
            if roi != last_roi:
                last_roi = roi
                prev_mask = ocr_mask = None

            state = winutil.foreground_state(hwnd)
            running = not self._paused and roi is not None
            visible = state in ("target", "self") or not cfg.hide_when_unfocused
            want_preview = self._preview_enabled and t0 - last_preview >= PREVIEW_INTERVAL
            # Pencere içeriğini doğrudan oku (overlay ve öndeki pencereler görüntüye girmez):
            # - hedef önde değilse (üstünde başka pencere olabilir)
            # - kayıt modunda (overlay ekran yakalamaya açık; ekrandan okursak kendi çevirimizi okuruz)
            ocr_from_window = running and visible and (state != "target" or cfg.show_in_recordings)

            full = pw_frame = None
            if want_preview or ocr_from_window:
                full = pw_frame = window_capturer.capture(hwnd, client[2], client[3])
                if full is None:
                    try:
                        full = grabber.grab(client)
                    except Exception as exc:
                        log.debug("Yakalama hatası: %s", exc)
                # Okunamama, ekranda görüntü varken PW_FAIL_GRACE süresince sürerse "desteklenmiyor"
                # say. Oyunun gerçekten siyah olduğu kareler (geçiş, yükleme) ve pencere öne
                # gelirken birkaç karelik boşluklar uyarının yanıp sönmesine yol açmasın.
                if pw_frame is not None or (full is not None and int(full.max()) < 16):
                    pw_fail_since = None
                elif pw_fail_since is None:
                    pw_fail_since = t0
                window_capture_ok = pw_fail_since is None or t0 - pw_fail_since < PW_FAIL_GRACE
                if window_capture_ok != last_wc_supported:
                    last_wc_supported = window_capture_ok
                    self.window_capture_supported.emit(window_capture_ok)
                    if not window_capture_ok and not printwindow_warned:
                        printwindow_warned = True
                        log.info("PrintWindow bu uygulamada çalışmadı; ekran yakalama kullanılacak")
                if full is not None and want_preview:
                    last_preview = t0
                    self.preview.emit(to_qimage(full, self.preview_max_width), client[2])

            if not running:
                set_visible(False)
                set_status("Durduruldu" if roi else "Altyazı bölgesi seçilmedi")
                time.sleep(0.05)
                continue

            set_visible(visible)
            if not visible:
                set_status("Hedef pencere önde değil, bekleniyor")
                time.sleep(0.2)
                continue

            try:
                translator, changed_engine = self._ensure_translator()
            except ModelMissing:
                set_status("Çeviri modeli indirilmedi: ayarlardaki Çeviri modeli listesinden indir")
                time.sleep(1.0)
                continue
            except Exception:
                log.exception("Çeviri motoru oluşturulamadı")
                set_status("Çeviri motoru oluşturulamadı, log dosyasına bak")
                time.sleep(1.0)
                continue
            if changed_engine:
                current = ""  # dil/motor değiştiyse mevcut altyazıyı yeniden çevir
            elif t0 - self._last_network > KEEPALIVE_SECONDS:
                self._warm_up()  # boşta kapanan bağlantıyı yeniden aç / çöken yerel sunucuyu başlat

            engine_state = translator.state()
            if engine_state == "loading":
                set_status("Yerel çeviri modeli yükleniyor…")
            elif engine_state == "failed":
                set_status("Yerel çeviri modeli başlatılamadı, log dosyasına bak")
            elif t0 < edge_warn_until:
                set_status("Altyazı bölgenin kenarına değiyor, satırlar kesilebilir: bölgeyi biraz büyüt")
            else:
                set_status("Çalışıyor")

            if ocr_from_window and pw_frame is None and cfg.show_in_recordings and window_capture_ok:
                # Kayıt modunda pencere içeriği bu karede okunamadı (geçici olabilir). Ekrandan
                # okursak overlay'deki kendi Türkçe yazımızı da okuruz: bu kareyi atla.
                time.sleep(0.05)
                continue

            try:
                if ocr_from_window and full is not None and full.shape[:2] == (client[3], client[2]):
                    rx, ry = roi[0] - client[0], roi[1] - client[1]
                    frame = np.ascontiguousarray(full[ry:ry + roi[3], rx:rx + roi[2]])
                else:
                    frame = grabber.grab(roi)
            except Exception as exc:
                log.debug("Yakalama hatası: %s", exc)
                time.sleep(0.5)
                continue
            if frame.size == 0:
                time.sleep(0.2)
                continue

            mask = text_mask(frame)
            moving = prev_mask is not None and mask_diff(mask, prev_mask) > STABLE_THRESHOLD
            prev_mask = mask
            changed = mask_diff(mask, ocr_mask) > CHANGE_THRESHOLD

            if (changed and not moving) or t0 >= next_force:
                ocr_mask = mask
                ocr.min_confidence = cfg.min_confidence
                t_ocr = time.monotonic()
                result = ocr.read_lines(frame)
                ocr_ms = (time.monotonic() - t_ocr) * 1000
                segments = segment_lines([line.text for line in result.lines])
                text = segments_text(segments)
                if text and touches_edge(result):
                    edge_warn_until = t0 + 5.0

                if not text:
                    empty_count += 1
                    if current and empty_count >= cfg.clear_after_empty:
                        current = ""
                        self._invalidate()  # bekleyen çeviri artık gösterilmesin
                        self.cleared.emit("gone")
                    # Altyazı kaybolmuş olabilir; teyit için kısa sürede tekrar bak.
                    next_force = t0 + (0.3 if current else cfg.force_ocr_ms / 1000)
                else:
                    empty_count = 0
                    next_force = t0 + cfg.force_ocr_ms / 1000
                    if not same_text(text, current):
                        current = text
                        # Çeviri arka planda; OCR döngüsü beklemeden devam eder.
                        self._start_translation(translator, text, segments, make_layout(result), ocr_ms)

            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, cfg.interval_ms / 1000 - elapsed))
