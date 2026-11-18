"""Yerel çeviri motoru: Hy-MT2 (Tencent, Apache 2.0) modelini llama.cpp'nin llama-server'ı ile çalıştırır.

- Program paketindeki `llama\\llama-server.exe` kullanılır (geliştirmede `vendor\\llama`).
- Modeller `%LOCALAPPDATA%\\SubCreator\\models` klasöründe (ilk açılışta indirilir, bkz. models.py);
  program klasöründeki `models\\` ve geliştirmede `vendor\\models` de aranır.
- Sunucu gizli bir alt süreç olarak, yalnızca 127.0.0.1'de açılır; internet gerekmez.
- Windows "iş nesnesi" ile SubCreator kapanınca (çökse bile) sunucu da kapanır.
- Ekran kartı Vulkan ile kullanılır; harici kart (NVIDIA/AMD) dahili karta tercih edilir.
"""
from __future__ import annotations

import ctypes
import logging
import re
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import requests

from .config import APP_DIR
from .models import CATALOG, GpuDevice, best_device, catalog_entry, parse_devices, user_models_dir

log = logging.getLogger(__name__)

SERVER_EXE = "llama-server.exe"
LOG_PATH = APP_DIR / "llama-server.log"
LOAD_TIMEOUT = 120.0
REQUEST_TIMEOUT = 30.0
RETRY_AFTER_FAILURE = 30.0
# RTX 5080'de cümle başına ~110 ms, 1.8B'den belirgin şekilde doğal çeviri (bkz. README).
DEFAULT_MODEL = CATALOG[0].file

# Modelin istem şablonu (Hy-MT2 README'deki varsayılan İngilizce şablon).
PROMPT = (
    "Translate the following text into {target}. Note that you should **only output the translated result"
    " without any additional explanation**:\n\n{text}"
)
WARMUP_TEXTS = (
    "Hello.",
    "You ready?",
    "Our backpacks are still here from last time, so grab your gear and let's go.",
    "What was that noise?\nStay down and keep quiet, they're coming this way.",
)
# Hy-MT2'nin desteklediği dillerin İngilizce adları (şablonda bu adlar kullanılır).
LANGUAGE_NAMES = {
    "tr": "Turkish", "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "ru": "Russian", "ar": "Arabic",
}

# Bilinen modeller: dosya adındaki boyuta göre arayüzde gösterilecek ad.
_MODEL_LABELS = (("1.8b", "Hy-MT2 1.8B (hızlı)"), ("7b", "Hy-MT2 7B (kaliteli)"), ("30b", "Hy-MT2 30B"))


class LocalEngineError(RuntimeError):
    """Yerel çeviri sunucusu başlatılamadı veya yanıt vermedi."""


class ModelMissing(LocalEngineError):
    """Çeviri modeli henüz indirilmedi."""


# --------------------------------------------------------------------------- dosyalar
def base_dirs() -> list[Path]:
    """llama/ ve models/ klasörlerinin aranacağı yerler (paket, sonra geliştirme ortamı)."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    root = Path(__file__).resolve().parent.parent
    dirs += [root / "vendor", root]
    return dirs


def model_dirs() -> list[Path]:
    return [user_models_dir()] + [d / "models" for d in base_dirs()]


def find_server() -> Path | None:
    for d in base_dirs():
        exe = d / "llama" / SERVER_EXE
        if exe.exists():
            return exe
    return None


def available_models() -> list[Path]:
    """İndirilmiş modeller (yarım kalan `.part` dosyaları sayılmaz)."""
    seen: dict[str, Path] = {}
    for d in model_dirs():
        try:
            paths = sorted(d.glob("*.gguf"))
        except OSError:
            continue
        for path in paths:
            seen.setdefault(path.name.lower(), path)
    return list(seen.values())


def model_label(path: Path | str) -> str:
    info = catalog_entry(Path(path).name)
    if info is not None:
        return info.label
    name = Path(path).name.lower()
    for key, label in _MODEL_LABELS:
        if f"-{key}" in name:
            return label
    return Path(path).stem


def find_model(name: str) -> Path | None:
    """Dosya adına göre modeli bul; boşsa veya yoksa önerilen modeli (yoksa en küçüğünü) döndür."""
    models = available_models()
    if not models:
        return None
    for wanted in (name, DEFAULT_MODEL):
        for m in models:
            if wanted and m.name.lower() == wanted.lower():
                return m
    return min(models, key=lambda p: p.stat().st_size)


def is_available() -> bool:
    return find_server() is not None and bool(available_models())


# --------------------------------------------------------------------------- iş nesnesi
class _JobObject:
    """Alt süreçleri, SubCreator kapanınca (çökse bile) Windows'un kapatması için."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXTENDED(ctypes.Structure):
        pass

    _EXTENDED._fields_ = [
        ("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

    def __init__(self) -> None:
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.handle = self._k32.CreateJobObjectW(None, None)
        if not self.handle:
            return
        info = self._EXTENDED()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._k32.SetInformationJobObject(
            self.handle, self.JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            self.handle = None

    def add(self, proc: subprocess.Popen) -> None:
        if self.handle and not self._k32.AssignProcessToJobObject(self.handle, int(proc._handle)):
            log.debug("Süreç iş nesnesine eklenemedi: %s", ctypes.get_last_error())


_job: _JobObject | None = None


def _job_object() -> _JobObject:
    global _job
    if _job is None:
        _job = _JobObject()
    return _job


# --------------------------------------------------------------------------- sunucu
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def list_devices(exe: Path | None = None) -> list[GpuDevice]:
    """llama-server'ın gördüğü Vulkan aygıtları (ekran kartları)."""
    exe = exe or find_server()
    if exe is None:
        return []
    try:
        out = subprocess.run(
            [str(exe), "--list-devices"], capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Ekran kartları listelenemedi: %s", exc)
        return []
    return parse_devices(out.stdout + out.stderr)


def pick_device(exe: Path) -> str | None:
    """Vulkan aygıtları arasından harici ekran kartını seç (dahili kartlar paylaşımlı belleği
    büyük gösterdiği için bellek boyutuna göre seçilemez). Aygıt yoksa None (CPU)."""
    dev = best_device(list_devices(exe))
    return dev.id if dev else None


def choose_device(list_devices_output: str) -> str | None:
    dev = best_device(parse_devices(list_devices_output))
    return dev.id if dev else None


class LlamaServer:
    """Tek bir model için arka planda çalışan llama-server. `start()` beklemez; `wait_ready()` bekler."""

    def __init__(self, exe: Path, model: Path) -> None:
        self.exe = exe
        self.model = model
        self.port = 0
        self.device: str | None = None
        self.error: str | None = None
        self._failed_at = 0.0
        self._proc: subprocess.Popen | _Starting | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.trust_env = False  # sistem vekil sunucusu 127.0.0.1 isteklerini bozmasın

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def state(self) -> str:
        """"ready", "loading", "failed" veya "stopped"."""
        if self._ready.is_set() and self.alive():
            return "ready"
        if self.error:
            return "failed"
        return "loading" if self.alive() else "stopped"

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.alive():
                return
            if self.error and time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE:
                return  # az önce başarısız oldu; her altyazıda yeniden denemesin
            self._ready.clear()
            self.error = None
            self.port = _free_port()
            # Süreç oluşturulana kadar ikinci bir start() yeni sunucu açmasın.
            marker = self._proc = _Starting()
        threading.Thread(target=self._launch, args=(marker,), name="llama-server", daemon=True).start()

    def _launch(self, marker: "_Starting") -> None:
        try:
            if self.device is None:
                self.device = pick_device(self.exe) or "none"
            args = [
                str(self.exe), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                "-c", "2048", "-np", "1", "--no-webui", "-rea", "off", "--log-file", str(LOG_PATH),
            ]
            if self.device != "none":
                args += ["-dev", self.device, "-ngl", "999"]
            else:
                args += ["-ngl", "0"]
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(self.exe.parent), creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _job_object().add(proc)
            with self._lock:
                if self._proc is not marker:  # bu arada stop() çağrıldı
                    proc.kill()
                    return
                self._proc = proc
            log.info("Yerel çeviri sunucusu başlatılıyor: %s (aygıt: %s)", self.model.name, self.device)
            t0 = time.monotonic()
            while time.monotonic() - t0 < LOAD_TIMEOUT:
                if proc.poll() is not None:
                    raise LocalEngineError(f"llama-server kapandı (kod {proc.returncode}), ayrıntı: {LOG_PATH}")
                try:
                    if self._session.get(self.base_url + "/health", timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                raise LocalEngineError(f"Model {LOAD_TIMEOUT:.0f} sn içinde yüklenemedi")
            # Isınma: ekran kartı ilk farklı uzunluktaki isteklerde yavaş (~1 sn); altyazı
            # uzunluğundaki birkaç örnekle bu bekleme oyun başlamadan ödenir.
            for sample in WARMUP_TEXTS:
                self._chat(PROMPT.format(target="Turkish", text=sample), max_tokens=64)
            self._ready.set()
            log.info("Yerel çeviri modeli hazır (%.1f sn)", time.monotonic() - t0)
        except Exception as exc:
            log.error("Yerel çeviri sunucusu başlatılamadı: %s", exc)
            with self._lock:
                self._failed_at = time.monotonic()
                self.error = str(exc)
                proc, self._proc = self._proc, None
            if proc is not None and not isinstance(proc, _Starting) and proc.poll() is None:
                proc.kill()

    def wait_ready(self, timeout: float = LOAD_TIMEOUT) -> None:
        self.start()
        deadline = time.monotonic() + timeout
        while not self._ready.wait(0.1):
            if self.error:
                raise LocalEngineError(self.error)
            if time.monotonic() > deadline:
                raise LocalEngineError("Yerel model hâlâ yükleniyor")
        if not self.alive():
            self._ready.clear()
            raise LocalEngineError("llama-server beklenmedik şekilde kapandı")

    def _chat(self, prompt: str, max_tokens: int = 256) -> str:
        resp = self._session.post(
            self.base_url + "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,  # aynı cümle hep aynı çeviri (önbellek ve titremesiz gösterim)
                "repeat_penalty": 1.05,
                "max_tokens": max_tokens,
                "cache_prompt": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready.clear()
        if proc is not None and not isinstance(proc, _Starting) and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass


class _Starting:
    """Süreç henüz oluşturulmadan önce `LlamaServer._proc` yerine geçen yer tutucu."""

    def poll(self):
        return None


_servers: dict[str, LlamaServer] = {}
_servers_lock = threading.Lock()


def get_server(model: Path) -> LlamaServer:
    """Model başına tek sunucu. Başka bir model istenirse eskisi kapatılır (ekran kartı belleği)."""
    exe = find_server()
    if exe is None:
        raise LocalEngineError("llama-server.exe bulunamadı")
    key = str(model).lower()
    with _servers_lock:
        for other_key in [k for k in _servers if k != key]:
            _servers.pop(other_key).stop()
        if key not in _servers:
            _servers[key] = LlamaServer(exe, model)
        return _servers[key]


def shutdown_all() -> None:
    with _servers_lock:
        servers = list(_servers.values())
        _servers.clear()
    for s in servers:
        s.stop()


# --------------------------------------------------------------------------- çevirmen
class LocalTranslator:
    """`translate(text)` arayüzü; CachingTranslator'ın içinde kullanılır."""

    def __init__(self, model: Path, source: str, target: str) -> None:
        self.model = model
        self.source = source
        self.target = target
        self.server = get_server(model)
        self.server.start()

    def translate(self, text: str) -> str:
        self.server.wait_ready()
        target = LANGUAGE_NAMES.get(self.target, self.target)
        return self.server._chat(PROMPT.format(target=target, text=text))

    def warm_up(self) -> None:
        self.server.start()  # çökmüşse yeniden başlat

    def state(self) -> str:
        return self.server.state()
