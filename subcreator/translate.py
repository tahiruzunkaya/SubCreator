"""Çeviri katmanları.

Katmanlar (dıştan içe):
  CachingTranslator → bellek + kalıcı disk önbelleği (tekrar eden cümleler anında)
  LocalTranslator   → yerel Hy-MT2 modeli (local_engine.py, internetsiz)
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class Translator(Protocol):
    def translate(self, text: str) -> str: ...


def normalize_key(text: str) -> str:
    return " ".join(text.split()).lower()


class TranslationCache:
    """SQLite tabanlı kalıcı çeviri önbelleği (thread-safe). En eski kullanılanlar budanır."""

    def __init__(self, path: Path, max_rows: int = 50_000) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, used REAL)")
        self._db.commit()
        self._puts = 0

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
            if row is not None:
                self._db.execute("UPDATE cache SET used = ? WHERE key = ?", (time.time(), key))
                self._db.commit()
            return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache (key, value, used) VALUES (?, ?, ?)", (key, value, time.time()))
            self._puts += 1
            if self._puts % 500 == 0:
                self._db.execute(
                    "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY used DESC LIMIT -1 OFFSET ?)",
                    (self.max_rows,),
                )
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM cache")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class CachingTranslator:
    """Önce bellek, sonra disk önbelleği; ikisinde de yoksa asıl motoru çağırır."""

    def __init__(self, inner, namespace: str, disk: TranslationCache | None = None, size: int = 1000) -> None:
        self._inner = inner
        self._namespace = namespace
        self._disk = disk
        self._size = size
        self._memory: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, text: str) -> str:
        return f"{self._namespace}|{normalize_key(text)}"

    def lookup(self, text: str) -> str | None:
        """Ağa çıkmadan, yalnızca önbellekten bakar."""
        key = self._key(text)
        with self._lock:
            if key in self._memory:
                self._memory.move_to_end(key)
                return self._memory[key]
        if self._disk is not None:
            value = self._disk.get(key)
            if value is not None:
                self._remember(key, value)
                return value
        return None

    def translate(self, text: str) -> str:
        cached = self.lookup(text)
        if cached is not None:
            return cached
        result = self._inner.translate(text)
        self._store(text, result)
        return result

    def lookup_many(self, texts: list[str]) -> list[str] | None:
        """Hepsi önbellekteyse çevirileri, değilse None döndürür (ağa çıkmaz)."""
        out = [self.lookup(t) for t in texts]
        return out if all(o is not None for o in out) else None

    def translate_many(self, texts: list[str]) -> list[str]:
        """Birden fazla konuşmayı çevirir: önbellekte olmayanlar tek istekte, satır satır gönderilir.

        Servis satır sayısını korumazsa her biri ayrı ayrı çevrilir.
        """
        results = [self.lookup(t) for t in texts]
        missing = [i for i, r in enumerate(results) if r is None]
        if len(missing) == 1:
            results[missing[0]] = self.translate(texts[missing[0]])
        elif missing:
            joined = self._inner.translate("\n".join(texts[i] for i in missing))
            parts = [p.strip() for p in joined.split("\n")]
            if len(parts) == len(missing) and all(parts):
                for i, part in zip(missing, parts):
                    self._store(texts[i], part)
                    results[i] = part
            else:
                for i in missing:
                    results[i] = self.translate(texts[i])
        return results

    def _store(self, text: str, result: str) -> None:
        if not result:
            return
        key = self._key(text)
        self._remember(key, result)
        if self._disk is not None:
            try:
                self._disk.put(key, result)
            except sqlite3.Error as exc:
                log.warning("Önbelleğe yazılamadı: %s", exc)

    def _remember(self, key: str, value: str) -> None:
        with self._lock:
            self._memory[key] = value
            self._memory.move_to_end(key)
            if len(self._memory) > self._size:
                self._memory.popitem(last=False)

    def warm_up(self) -> None:
        if hasattr(self._inner, "warm_up"):
            self._inner.warm_up()

    def state(self) -> str:
        """Motorun durumu: "ready", "loading" (yerel model yükleniyor) veya "failed"."""
        return self._inner.state() if hasattr(self._inner, "state") else "ready"


def create_translator(name: str, source: str, target: str, disk: TranslationCache | None = None) -> CachingTranslator:
    """name: "local" (önerilen/indirilmiş model) veya "local:<model dosyası>".

    Model indirilmemişse `local_engine.ModelMissing` yükseltir.
    """
    from . import local_engine

    model = local_engine.find_model(name.partition(":")[2] if name.startswith("local:") else "")
    if model is None:
        raise local_engine.ModelMissing("Çeviri modeli indirilmedi")
    if local_engine.find_server() is None:
        raise local_engine.LocalEngineError("llama-server.exe bulunamadı (program klasöründeki llama\\ eksik)")
    inner = local_engine.LocalTranslator(model, source, target)
    return CachingTranslator(inner, f"local|{model.stem.lower()}|{source}|{target}", disk)
