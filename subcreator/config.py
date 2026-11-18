from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "SubCreator"
CONFIG_PATH = APP_DIR / "config.json"
LEGACY_PATH = Path(__file__).resolve().parent.parent / "config.json"
# 2: varsayılan çeviri motoru Google'dan yerel Hy-MT2 modeline geçti.
# 3: Google çevirisi kaldırıldı; yalnızca yerel model.
CONFIG_VERSION = 3

LANGUAGES = {
    "tr": "Türkçe",
    "en": "İngilizce",
    "de": "Almanca",
    "fr": "Fransızca",
    "es": "İspanyolca",
    "it": "İtalyanca",
    "ru": "Rusça",
    "ar": "Arapça",
}


@dataclass
class Target:
    """Okunacak uygulama: exe adı, isteğe bağlı başlık filtresi ve altyazı bölgesi."""

    process_name: str
    window_title: str = ""
    # Pencerenin istemci alanına göre oran: [x, y, w, h] (0..1)
    region: list[float] | None = None
    # Çevirinin gösterileceği yer (overlay_mode == "custom" iken), aynı oran biçiminde.
    overlay_region: list[float] | None = None


@dataclass
class Config:
    targets: dict[str, Target] = field(default_factory=lambda: {"chiaki.exe": Target("chiaki.exe")})
    active_target: str = "chiaki.exe"

    source_lang: str = "en"
    target_lang: str = "tr"
    # "local" (önerilen/indirilmiş Hy-MT2 modeli) veya "local:<model dosyası>".
    translator: str = "local"
    config_version: int = CONFIG_VERSION

    # "cover": orijinal altyazının üstüne (kapatarak), "above": hemen üstüne,
    # "custom": önizlemede çizilen yere (Target.overlay_region).
    overlay_mode: str = "cover"
    hide_when_unfocused: bool = True
    # Çeviri OBS kayıtlarında / ekran görüntülerinde görünsün mü. Kapalıyken pencere ekran
    # yakalamadan hariç tutulur (OCR kendi Türkçe yazımızı okumasın diye).
    show_in_recordings: bool = False
    font_family: str = "Segoe UI"
    max_font_px: int = 40
    bg_opacity: float = 0.85
    # Orijinal altyazı kaybolduktan sonra çevirinin ekranda kalmaya devam edeceği süre.
    linger_ms: int = 500
    # Her çevirinin, orijinal erken kaybolsa bile en az bu kadar görünmesi (okumaya zaman tanır).
    min_display_ms: int = 0

    min_confidence: float = 0.6
    interval_ms: int = 120
    force_ocr_ms: int = 1500
    clear_after_empty: int = 2

    def target(self) -> Target:
        key = self.active_target
        if key not in self.targets:
            self.targets[key] = Target(key)
        return self.targets[key]

    def set_target(self, process_name: str, window_title: str = "") -> Target:
        self.active_target = process_name
        t = self.target()
        t.window_title = window_title
        return t

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        source = path
        if not path.exists() and path == CONFIG_PATH and LEGACY_PATH.exists():
            source = LEGACY_PATH  # ilk prototipin proje klasöründeki ayarı
        if not source.exists():
            return cls()
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("config.json okunamadı (%s), varsayılanlar kullanılıyor", exc)
            return cls()

        if "targets" not in data and ("region" in data or "process_name" in data):
            data = _migrate_legacy(data)
            cfg = cls._from_dict(data)
            if source != path:
                try:
                    cfg.save(path)
                    log.info("Eski ayarlar %s → %s taşındı", source, path)
                except OSError:
                    log.exception("Taşınan ayarlar kaydedilemedi")
            return cfg
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict) -> "Config":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known and k != "targets"}
        # Google çevirisi kaldırıldı: eski ayarlardaki motor ne olursa olsun yerel modele geçir.
        if not str(values.get("translator", "local")).startswith("local"):
            values["translator"] = "local"
        values["config_version"] = CONFIG_VERSION
        cfg = cls(**values)
        raw_targets = data.get("targets")
        if isinstance(raw_targets, dict) and raw_targets:
            target_fields = {f.name for f in fields(Target)}
            cfg.targets = {
                key: Target(**{k: v for k, v in t.items() if k in target_fields})
                for key, t in raw_targets.items()
                if isinstance(t, dict) and t.get("process_name")
            } or cfg.targets
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def above_region(region: list[float], gap: float = 0.005) -> list[float]:
    x, y, w, h = region
    return [x, max(0.0, y - h - gap), w, h]


def rects_overlap(a: list[float], b: list[float]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def overlay_fracs(cfg: Config) -> list[float] | None:
    """Çevirinin gösterileceği alan, pencereye göre oran olarak. Bölge yoksa None."""
    target = cfg.target()
    if not target.region:
        return None
    if cfg.overlay_mode == "custom" and target.overlay_region:
        return target.overlay_region
    if cfg.overlay_mode in ("above", "custom"):
        return above_region(target.region)
    return target.region


def _migrate_legacy(data: dict) -> dict:
    """İlk prototipin tek hedefli ayarını (process_name/window_title/region) yeni biçime çevirir."""
    data = dict(data)
    process = (data.pop("process_name", "") or "chiaki").strip()
    if not process.lower().endswith(".exe"):
        process += ".exe"
    key = process.lower()
    data["targets"] = {
        key: {
            "process_name": process,
            "window_title": data.pop("window_title", "") or "",
            "region": data.pop("region", None),
        }
    }
    data["active_target"] = key
    return data
