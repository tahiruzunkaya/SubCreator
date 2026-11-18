"""Modül testleri: OCR, çeviri, model indirme, değişim tespiti, Win32 yardımcıları.

Çalıştır:  .venv\\Scripts\\python.exe scripts\\selftest.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subcreator import winutil  # noqa: E402
from subcreator.ocr import SubtitleOCR  # noqa: E402
from subcreator.pipeline import CHANGE_THRESHOLD, mask_diff, same_text, text_mask  # noqa: E402
from subcreator.translate import create_translator  # noqa: E402


def make_frame(text: str, seed: int = 0, w: int = 1280, h: int = 140) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.linspace(40, 140, w, dtype=np.float32)[None, :, None].repeat(h, 0).repeat(3, 2)
    base += rng.normal(0, 18, base.shape)
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    if text:
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 38)
        tw = draw.textlength(text, font=font)
        draw.text(((w - tw) / 2, 45), text, font=font, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    return np.array(img)[:, :, ::-1].copy()  # RGB → BGR


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK' if ok else 'HATA'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def download_tests(models) -> list[bool]:
    """Model indirme: yerel bir HTTP sunucusundan (Range destekli) indir, devam et, doğrula, iptal et."""
    import hashlib
    import http.server
    import os
    import tempfile
    import threading

    payload = os.urandom(3 * 1024 * 1024 + 123)
    state = {"ranges": [], "support_range": True}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            state["ranges"].append(rng)
            start = int(rng.split("=")[1].split("-")[0]) if rng and state["support_range"] else 0
            body = payload[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    old_mirror = os.environ.get("SUBCREATOR_MODEL_MIRROR")
    os.environ["SUBCREATOR_MODEL_MIRROR"] = f"http://127.0.0.1:{server.server_address[1]}"
    info = models.ModelInfo("test.gguf", "Test", "", "org/repo", "abc", len(payload),
                            hashlib.sha256(payload).hexdigest())
    out = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            path = models.download(info, d)
            out.append(check("Model indirme + SHA-256 doğrulama",
                             path.read_bytes() == payload and not models.part_path(info, d).exists()))
            path.unlink()

            models.part_path(info, d).write_bytes(payload[:1_000_000])
            state["ranges"].clear()
            path = models.download(info, d)
            out.append(check("Yarım kalan indirme kaldığı yerden devam eder",
                             path.read_bytes() == payload and state["ranges"] == ["bytes=1000000-"], str(state["ranges"])))
            path.unlink()

            state["support_range"] = False
            models.part_path(info, d).write_bytes(payload[:1_000_000])
            path = models.download(info, d)
            out.append(check("Sunucu devam etmeyi desteklemezse baştan indirir", path.read_bytes() == payload))
            path.unlink()
            state["support_range"] = True

            models.part_path(info, d).write_bytes(b"x" * 1_000_000)  # bozuk yarım dosya
            try:
                models.download(info, d)
                bad_ok = False
            except models.DownloadError:
                bad_ok = not models.part_path(info, d).exists() and not (d / info.file).exists()
            out.append(check("Bozuk dosya doğrulamadan geçmez ve silinir", bad_ok))

            seen = []
            try:
                models.download(info, d, progress=lambda done, total, stage: seen.append(done),
                                cancelled=lambda: len(seen) >= 1)
                cancel_ok = False
            except models.DownloadCancelled:
                cancel_ok = models.part_path(info, d).exists() and not (d / info.file).exists()
            out.append(check("İptal edilince yarım dosya devam için saklanır", cancel_ok))
    finally:
        server.shutdown()
        if old_mirror is None:
            os.environ.pop("SUBCREATOR_MODEL_MIRROR", None)
        else:
            os.environ["SUBCREATOR_MODEL_MIRROR"] = old_mirror
    return out


def main() -> int:
    results = []
    sentence = "Where did you hide the key? We need to leave before sunrise."

    ocr = SubtitleOCR()
    frame = make_frame(sentence)
    ocr.read(frame)  # ısınma
    t = time.monotonic()
    text = ocr.read(frame)
    ms = (time.monotonic() - t) * 1000
    results.append(check("OCR metni okudu", same_text(text, sentence), f"{ms:.0f} ms, okunan: {text!r}"))
    results.append(check("Boş bölgede OCR boş döner", ocr.read(make_frame("", seed=3)) == ""))

    m1 = text_mask(make_frame(sentence, seed=1))
    m1b = text_mask(make_frame(sentence, seed=2))
    m2 = text_mask(make_frame("Stay close to me and don't make a sound.", seed=1))
    results.append(check("Farklı altyazı değişim olarak algılanır", mask_diff(m1, m2) > CHANGE_THRESHOLD,
                         f"fark={mask_diff(m1, m2):.4f}"))
    results.append(check("Aynı altyazı + arka plan gürültüsü değişim sayılmaz", mask_diff(m1, m1b) <= CHANGE_THRESHOLD,
                         f"fark={mask_diff(m1, m1b):.4f}"))

    from subcreator import local_engine

    if local_engine.is_available():
        try:
            model = local_engine.find_model("")
            tr = create_translator(f"local:{model.name}", "en", "tr")
            t = time.monotonic()
            tr._inner.server.wait_ready()
            load_s = time.monotonic() - t
            t = time.monotonic()
            out = tr.translate(sentence)
            ms = (time.monotonic() - t) * 1000
            t = time.monotonic()
            tr.translate(sentence)
            cached_ms = (time.monotonic() - t) * 1000
            results.append(check(f"Yerel çeviri ({model.name})", bool(out) and out != sentence and any(ch in out.lower() for ch in "ışğüöç"),
                                 f"yükleme {load_s:.1f} s, çeviri {ms:.0f} ms → {out!r}"))
            results.append(check("Çeviri önbelleği", cached_ms < 5, f"{cached_ms:.2f} ms"))
            proc = tr._inner.server._proc
            local_engine.shutdown_all()
            results.append(check("Yerel sunucu kapanınca süreç biter", proc.poll() is not None))
        except Exception as exc:
            results.append(check("Yerel çeviri", False, repr(exc)))
    else:
        print("[ATLANDI] Yerel çeviri: vendor\\llama veya vendor\\models yok")

    from subcreator import models

    def devices(*rows):
        return "Available devices:\n" + "".join(
            f"  Vulkan{i}: {name} ({mb} MiB, {mb - 500} MiB free)\n" for i, (name, mb) in enumerate(rows))

    cases = [
        (devices(("Intel(R) Graphics", 18328), ("NVIDIA GeForce RTX 5080", 15977)), "Vulkan1", "7B"),
        (devices(("AMD Radeon(TM) Graphics", 8192), ("NVIDIA GeForce RTX 3050 Laptop GPU", 4096)), "Vulkan1", "1.8B"),
        (devices(("AMD Radeon RX 7800 XT", 16368)), "Vulkan0", "7B"),
        (devices(("Intel(R) UHD Graphics", 8000)), "Vulkan0", "1.8B"),
        ("Available devices:\n", None, "1.8B"),
    ]
    got = [(local_engine.choose_device(out), models.recommend(models.parse_devices(out))[0].label) for out, _, _ in cases]
    results.append(check("Ekran kartı seçimi ve model önerisi",
                         all(dev == want_dev and want_model in label
                             for (dev, label), (_, want_dev, want_model) in zip(got, cases)), str(got)))

    import tempfile
    import threading

    saved = local_engine.available_models
    local_engine.available_models = lambda: []
    try:
        create_translator("local", "en", "tr")
        missing_ok = False
    except local_engine.ModelMissing:
        missing_ok = True
    finally:
        local_engine.available_models = saved
    results.append(check("Model indirilmemişse çeviri başlamaz (ModelMissing)", missing_ok))

    results.extend(download_tests(models))

    from subcreator.textfix import fix_ocr, join_speaker, split_speaker
    from subcreator.translate import CachingTranslator, TranslationCache

    fixes = {
        "| know what you did .": "I know what you did.",
        "l'm not going every- where.": "I'm not going everywhere.",
        "1'll be there.": "I'll be there.",
    }
    results.append(check("OCR hata düzeltme", all(fix_ocr(k) == v for k, v in fixes.items()),
                         str({k: fix_ocr(k) for k in fixes})))
    from subcreator.textfix import segment_lines

    wrapped = segment_lines(["If we don't reach the lighthouse,", "we'll lose the signal."])
    dialog = segment_lines(["- Are you sure?", "- Trust me."])
    speakers = segment_lines(["Ellie: What was that?", "Joel: Stay down", "and keep quiet."])
    results.append(check("Çok satırlı altyazı: bölünmüş cümle birleşir, diyalog/konuşmacı ayrılır",
                         wrapped == [("", "If we don't reach the lighthouse, we'll lose the signal.")]
                         and dialog == [("- ", "Are you sure?"), ("- ", "Trust me.")]
                         and speakers == [("Ellie: ", "What was that?"), ("Joel: ", "Stay down and keep quiet.")],
                         f"{wrapped} | {dialog} | {speakers}"))
    results.append(check("Konuşmacı adını koruma",
                         split_speaker("Ellie: Stay close.") == ("Ellie", "Stay close.")
                         and split_speaker("Note: Press X.") == (None, "Note: Press X.")
                         and join_speaker("Ellie", "Yakın dur.") == "Ellie: Yakın dur."))

    class Fake:
        def __init__(self, delays):
            self.delays, self.calls, self.lock = list(delays), 0, threading.Lock()

        def translate(self, text):
            with self.lock:
                i = self.calls
                self.calls += 1
            time.sleep(self.delays[min(i, len(self.delays) - 1)])
            return f"{text}#{i}"

    with tempfile.TemporaryDirectory() as tmp:
        db = TranslationCache(Path(tmp) / "t.db", max_rows=3)
        inner = Fake([0.0])
        ct = CachingTranslator(inner, "test", db)
        ct.translate("Hello there")
        ct2 = CachingTranslator(inner, "test", db)  # yeni oturum: bellek boş, disk dolu
        hit = ct2.lookup("hello   THERE")

        class Joiner:  # satır satır çevirir gibi davranır
            calls = 0

            def translate(self, text):
                Joiner.calls += 1
                return "\n".join(f"TR({line})" for line in text.split("\n"))

        many = CachingTranslator(Joiner(), "many", db).translate_many(["a", "b", "c"])
        results.append(check("Çok konuşma tek istekte çevrilir", many == ["TR(a)", "TR(b)", "TR(c)"] and Joiner.calls == 1,
                             f"{many}, istek={Joiner.calls}"))
        for i in range(600):
            db.put(f"k{i}", "v")
        results.append(check("Kalıcı disk önbelleği", hit == "Hello there#0" and inner.calls == 1 and db.count() <= 600,
                             f"bulunan={hit!r}, çağrı={inner.calls}, kayıt={db.count()}"))
        db.close()

    from subcreator.config import Config, Target

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        cfg = Config()
        cfg.targets["game.exe"] = Target("Game.exe", "Oyun", [0.1, 0.8, 0.8, 0.15])
        cfg.active_target = "game.exe"
        cfg.target_lang = "de"
        cfg.save(path)
        loaded = Config.load(path)
        results.append(check("Ayar kaydet/yükle (uygulama bazında bölge)",
                             loaded.target().region == [0.1, 0.8, 0.8, 0.15] and loaded.target_lang == "de"
                             and "chiaki.exe" in loaded.targets))

        legacy = Path(tmp) / "legacy.json"
        legacy.write_text('{"process_name": "chiaki", "window_title": "", "region": [0.3, 0.86, 0.38, 0.11],'
                          ' "max_font_px": 32}', encoding="utf-8")
        migrated = Config.load(legacy)
        results.append(check("Eski prototip ayarını taşıma",
                             migrated.active_target == "chiaki.exe"
                             and migrated.target().region == [0.3, 0.86, 0.38, 0.11]
                             and migrated.max_font_px == 32))

        old = Path(tmp) / "old.json"
        old.write_text('{"translator": "google", "target_lang": "tr"}', encoding="utf-8")
        chosen = Path(tmp) / "chosen.json"
        chosen.write_text('{"translator": "google", "config_version": 2}', encoding="utf-8")
        kept = Path(tmp) / "kept.json"
        kept.write_text('{"translator": "local:Hy-MT2-1.8B-Q8_0.gguf", "config_version": 2}', encoding="utf-8")
        results.append(check("Google seçili eski ayarlar yerel modele geçer, seçilen model korunur",
                             Config.load(old).translator == "local" and Config.load(chosen).translator == "local"
                             and Config.load(kept).translator == "local:Hy-MT2-1.8B-Q8_0.gguf"
                             and Config().translator == "local"))

    from subcreator.config import overlay_fracs

    c = Config()
    c.target().region = [0.2, 0.8, 0.6, 0.1]
    cover = overlay_fracs(c)
    c.overlay_mode = "above"
    above = overlay_fracs(c)
    c.overlay_mode = "custom"
    c.target().overlay_region = [0.1, 0.05, 0.8, 0.1]
    custom = overlay_fracs(c)
    results.append(check("Çeviri konumu (kapat / üstünde / özel)",
                         cover == [0.2, 0.8, 0.6, 0.1] and above[1] < 0.8 and abs(above[1] - 0.695) < 1e-9
                         and custom == [0.1, 0.05, 0.8, 0.1], f"{cover} | {above} | {custom}"))

    windows = winutil.list_windows()
    results.append(check("Açık pencere listesi", len(windows) > 0, f"{len(windows)} pencere, ör. {windows[0][1]}"
                         if windows else ""))
    results.append(check("Tam exe adıyla eşleştirme", winutil._process_matches("chiaki.exe", "chiaki.exe")
                         and not winutil._process_matches("chiaki-helper.exe", "chiaki.exe")
                         and winutil._process_matches("python.exe", "python")))

    hwnd = winutil.find_target_window("explorer", "")
    rect = winutil.client_rect_screen(hwnd) if hwnd else None
    results.append(check("Pencere bulma + istemci alanı", rect is not None, f"explorer → {rect}"))
    results.append(check("Bölge → dikdörtgen", winutil.region_to_rect([0, 0.8, 1, 0.2], (100, 50, 1000, 500))
                         == (100, 450, 1000, 100)))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} test geçti")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
