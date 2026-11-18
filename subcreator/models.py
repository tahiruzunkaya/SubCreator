"""Çeviri modelleri: katalog, ekran kartına göre öneri ve Hugging Face'ten indirme.

Modeller programla birlikte dağıtılmaz (7B dosyası 4,6 GB); ilk açılışta kullanıcının seçtiği
model `%LOCALAPPDATA%\\SubCreator\\models` klasörüne indirilir. Böylece program yeni bir sürümle
değiştirildiğinde model yeniden indirilmez.

- İndirme yarıda kalırsa `.part` dosyasından devam eder (HTTP Range).
- Dosya, katalogdaki SHA-256 ile doğrulanmadan kullanılmaz.
- Adresler belirli bir commit'e sabitlenmiştir: depodaki dosya değişse bile doğrulama bozulmaz.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

log = logging.getLogger(__name__)

HF_BASE = "https://huggingface.co"
CHUNK = 1 << 20
DISK_MARGIN = 300 * 1024 * 1024  # indirme sonrası diskte kalması gereken en az boş alan
# 8 GB'lık kartlar sürücü/ekran payı düşülünce ~7,5-8 GB gösterir; 7B yaklaşık 5 GB kullanır.
MIN_VRAM_7B_MB = 7000


@dataclass(frozen=True)
class ModelInfo:
    file: str
    label: str
    description: str
    repo: str
    revision: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        # Kısıtlı ağlar için ayna sunucu (ör. şirket içi): SUBCREATOR_MODEL_MIRROR=http://sunucu
        base = os.environ.get("SUBCREATOR_MODEL_MIRROR", HF_BASE).rstrip("/")
        return f"{base}/{self.repo}/resolve/{self.revision}/{self.file}"

    @property
    def page(self) -> str:
        return f"{HF_BASE}/{self.repo}"

    @property
    def size_text(self) -> str:
        return f"{self.size / 1e9:.1f} GB".replace(".", ",")


CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo(
        file="Hy-MT2-7B-Q4_K_M.gguf",
        label="Hy-MT2 7B (kaliteli)",
        description="En doğal çeviri. En az 8 GB belleği olan bir ekran kartı önerilir.",
        repo="tencent/Hy-MT2-7B-GGUF",
        revision="ab8472660ac61fac25f1af43fac2599d52a8a775",
        size=4_624_648_896,
        sha256="9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b",
    ),
    ModelInfo(
        file="Hy-MT2-1.8B-Q8_0.gguf",
        label="Hy-MT2 1.8B (hızlı)",
        description="Daha hızlı ve hafif; 4 GB ekran kartı belleği yeter. Çeviri biraz daha az doğal.",
        repo="tencent/Hy-MT2-1.8B-GGUF",
        revision="a0c709d9fac510f2c807aa3af52872340dc37a4a",
        size=1_908_528_192,
        sha256="5c3fe0b1408a5ceb0143184ef247b11b579c525f4b02b060e6c851bb76fef1a4",
    ),
)


def catalog_entry(file_name: str) -> ModelInfo | None:
    for m in CATALOG:
        if m.file.lower() == file_name.lower():
            return m
    return None


def user_models_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "SubCreator" / "models"


# --------------------------------------------------------------------------- ekran kartı
@dataclass(frozen=True)
class GpuDevice:
    id: str  # llama-server aygıt adı, ör. "Vulkan0"
    name: str
    total_mb: int
    free_mb: int

    @property
    def discrete(self) -> bool:
        """Harici kart mı? Dahili kartlar (Intel, AMD Radeon Graphics) paylaşımlı belleği büyük
        gösterdiği için bellek boyutuna değil ada bakılır."""
        return bool(re.search(
            r"nvidia|geforce|rtx|quadro|radeon\s*(\(tm\)\s*)?(rx|pro)|\brx\s*\d|arc\(tm\)\s*[ab]\d",
            self.name, re.IGNORECASE,
        ))


def parse_devices(output: str) -> list[GpuDevice]:
    """`llama-server --list-devices` çıktısı: "  Vulkan0: NVIDIA GeForce RTX 5080 (15977 MiB, 15209 MiB free)"."""
    devices = []
    for m in re.finditer(r"^\s*(\w+\d+):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)", output, re.MULTILINE):
        devices.append(GpuDevice(m.group(1), m.group(2), int(m.group(3)), int(m.group(4))))
    return devices


def best_device(devices: list[GpuDevice]) -> GpuDevice | None:
    """Harici kartlar arasından belleği en büyük olanı; harici kart yoksa ilk aygıt."""
    discrete = [d for d in devices if d.discrete]
    if discrete:
        return max(discrete, key=lambda d: d.total_mb)
    return devices[0] if devices else None


def recommend(devices: list[GpuDevice]) -> tuple[ModelInfo, str]:
    """Ekran kartına göre önerilen model ve kullanıcıya gösterilecek açıklama."""
    big, small = CATALOG[0], CATALOG[1]
    dev = best_device(devices)
    if dev is None:
        return small, "Uygun ekran kartı bulunamadı; çeviri işlemcide yavaş çalışabilir."
    gb = f"{dev.total_mb / 1024:.0f} GB"
    if dev.discrete and dev.total_mb >= MIN_VRAM_7B_MB:
        return big, f"Ekran kartı: {dev.name} ({gb}). 7B modeli rahat çalışır."
    if dev.discrete:
        return small, f"Ekran kartı: {dev.name} ({gb}). 7B için bellek az olabilir; 1.8B önerilir."
    return small, f"Harici ekran kartı bulunamadı ({dev.name}); hafif 1.8B modeli önerilir."


# --------------------------------------------------------------------------- indirme
class DownloadError(RuntimeError):
    pass


class DownloadCancelled(Exception):
    pass


ProgressFn = Callable[[int, int, str], None]  # indirilen bayt, toplam bayt, aşama ("check"/"download"/"verify")


def part_path(info: ModelInfo, dest_dir: Path) -> Path:
    return dest_dir / (info.file + ".part")


def download(
    info: ModelInfo,
    dest_dir: Path | None = None,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    session: requests.Session | None = None,
) -> Path:
    """Modeli indir, SHA-256 ile doğrula ve `dest_dir/<dosya>` yolunu döndür.

    Yarım kalan `.part` dosyası varsa kaldığı yerden devam eder. İptal edilirse `.part` korunur.
    """
    dest_dir = dest_dir or user_models_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / info.file
    part = part_path(info, dest_dir)
    report = progress or (lambda *_: None)
    session = session or requests.Session()

    have = part.stat().st_size if part.exists() else 0
    if have > info.size:
        part.unlink()
        have = 0
    free = shutil.disk_usage(dest_dir).free
    need = info.size - have + DISK_MARGIN
    if free < need:
        raise DownloadError(
            f"Diskte yeterli boş alan yok: {need / 1e9:.1f} GB gerekli, {free / 1e9:.1f} GB boş ({dest_dir.drive})"
        )

    digest = hashlib.sha256()
    if have:  # önceki indirmenin hash'ini çıkar (devam edilen kısım da doğrulanmalı)
        done = 0
        with open(part, "rb") as f:
            while chunk := f.read(8 * CHUNK):
                if cancelled():
                    raise DownloadCancelled()
                digest.update(chunk)
                done += len(chunk)
                report(done, info.size, "check")

    if have < info.size:
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            resp = session.get(info.url, headers=headers, stream=True, timeout=(20, 60))
        except requests.RequestException as exc:
            raise DownloadError(f"Bağlantı kurulamadı: {exc}") from exc
        with resp:
            if have and resp.status_code == 200:  # sunucu kaldığı yerden devam etmeyi desteklemedi
                have, digest = 0, hashlib.sha256()
            elif resp.status_code not in (200, 206):
                raise DownloadError(f"Sunucu hatası: HTTP {resp.status_code}")
            log.info("Model indiriliyor: %s (%d/%d bayt hazır)", info.file, have, info.size)
            try:
                with open(part, "ab" if have else "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        if cancelled():
                            raise DownloadCancelled()
                        f.write(chunk)
                        digest.update(chunk)
                        have += len(chunk)
                        report(have, info.size, "download")
                        if have > info.size:
                            break
            except requests.RequestException as exc:
                raise DownloadError(f"İndirme yarıda kesildi: {exc}") from exc

    if have != info.size:
        if have > info.size:
            part.unlink(missing_ok=True)
        raise DownloadError(f"Dosya eksik indi ({have} / {info.size} bayt); tekrar deneyin")
    report(have, info.size, "verify")
    if digest.hexdigest() != info.sha256:
        part.unlink(missing_ok=True)
        raise DownloadError("İndirilen dosya doğrulanamadı (SHA-256 uyuşmuyor); tekrar deneyin")
    os.replace(part, final)
    log.info("Model indirildi ve doğrulandı: %s", final)
    return final


class Speedometer:
    """İndirme hızı ve kalan süre (son birkaç saniyenin ortalaması)."""

    def __init__(self, window: float = 4.0) -> None:
        self.window = window
        self.samples: list[tuple[float, int]] = []

    def update(self, done: int, total: int) -> str:
        now = time.monotonic()
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.pop(0)
        t0, d0 = self.samples[0]
        text = f"{done / 1e9:.2f} / {total / 1e9:.2f} GB".replace(".", ",")
        if now - t0 >= 0.5 and done > d0:
            rate = (done - d0) / (now - t0)
            left = (total - done) / rate
            eta = f"{left / 60:.0f} dk" if left >= 90 else f"{left:.0f} sn"
            text += f"  •  {rate / 1e6:.1f} MB/sn  •  yaklaşık {eta} kaldı".replace(".", ",")
        return text
