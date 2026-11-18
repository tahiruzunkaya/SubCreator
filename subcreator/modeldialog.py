"""Çeviri modelini indirme penceresi (ilk açılışta ve ayarlardan model seçilince açılır)."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from . import local_engine, models

log = logging.getLogger(__name__)


class _GpuThread(QThread):
    found = Signal(object)  # list[GpuDevice]

    def run(self) -> None:
        self.found.emit(local_engine.list_devices())


class _DownloadThread(QThread):
    progress = Signal(float, float, str)  # indirilen, toplam (bayt; 2 GB üstü için float), aşama
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, info: models.ModelInfo) -> None:
        super().__init__()
        self.info = info
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            path = models.download(
                self.info,
                progress=lambda done, total, stage: self.progress.emit(float(done), float(total), stage),
                cancelled=lambda: self._cancel,
            )
        except models.DownloadCancelled:
            self.failed.emit("")
        except models.DownloadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # disk, izin vb.
            log.exception("Model indirilemedi")
            self.failed.emit(f"Beklenmeyen hata: {exc}")
        else:
            self.succeeded.emit(str(path))


class ModelDialog(QDialog):
    """Model seç ve indir. Başarılı olunca `chosen` indirilen/seçilen modelin dosya adıdır."""

    def __init__(self, parent=None, preselect: str | None = None, first_run: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Çeviri modelini indir")
        self.setMinimumWidth(560)
        self.chosen: str | None = None
        self._download: _DownloadThread | None = None
        self._speed = models.Speedometer()
        self._user_picked = preselect is not None
        downloaded = {p.name.lower() for p in local_engine.available_models()}

        lay = QVBoxLayout(self)
        title = QLabel(
            "SubCreator'ın çalışması için bir çeviri modeli gerekiyor" if first_run else "Çeviri modeli"
        )
        f = title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)
        intro = QLabel(
            "Çeviri, bu bilgisayarın ekran kartında çalışan bir yapay zeka modeliyle yapılır. Model bir kez "
            "indirilir; sonrasında internet gerekmez ve okunan altyazılar hiçbir yere gönderilmez."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.gpu_label = QLabel("Ekran kartı inceleniyor…")
        self.gpu_label.setWordWrap(True)
        self.gpu_label.setStyleSheet("color: #4aa3ff;")
        lay.addWidget(self.gpu_label)

        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for info in models.CATALOG:
            done = info.file.lower() in downloaded
            radio = QRadioButton(f"{info.label}  –  {'indirildi' if done else info.size_text + ' indirme'}")
            f = radio.font()
            f.setBold(True)
            radio.setFont(f)
            desc = QLabel(info.description)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: gray; margin-left: 22px;")
            self.group.addButton(radio)
            self.radios[info.file] = radio
            radio.toggled.connect(self._on_choice_changed)
            radio.clicked.connect(self._on_user_pick)
            lay.addWidget(radio)
            lay.addWidget(desc)
        start = models.catalog_entry(preselect or "") or models.CATALOG[0]
        self.radios[start.file].setChecked(True)

        src = QLabel(
            f'Modeller: Tencent <a href="https://github.com/Tencent-Hunyuan/Hy-MT2">Hy-MT2</a> (Apache 2.0), '
            f"Hugging Face'ten indirilir.<br>Klasör: {models.user_models_dir()}"
        )
        src.setOpenExternalLinks(True)
        src.setWordWrap(True)
        src.setStyleSheet("color: gray;")
        src.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lay.addWidget(src)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.later_btn = QPushButton("Daha sonra")
        self.later_btn.clicked.connect(self.reject)
        self.cancel_btn = QPushButton("İptal")
        self.cancel_btn.clicked.connect(self._cancel_download)
        self.cancel_btn.hide()
        self.go_btn = QPushButton("İndir")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._start)
        buttons.addWidget(self.later_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.go_btn)
        lay.addLayout(buttons)
        self._on_choice_changed()

        self._gpu = _GpuThread(self)
        self._gpu.found.connect(self._on_gpu)
        self._gpu.start()

    # ------------------------------------------------------------------ seçim
    def selected(self) -> models.ModelInfo:
        for file, radio in self.radios.items():
            if radio.isChecked():
                return models.catalog_entry(file)
        return models.CATALOG[0]

    def _is_downloaded(self, info: models.ModelInfo) -> bool:
        return any(p.name.lower() == info.file.lower() for p in local_engine.available_models())

    def _on_user_pick(self) -> None:
        self._user_picked = True

    def _on_choice_changed(self, *_args) -> None:
        if self._download is not None or not hasattr(self, "go_btn"):  # kurulum sırasında düğme henüz yok
            return
        info = self.selected()
        if self._is_downloaded(info):
            self.go_btn.setText("Bu modeli kullan")
        elif models.part_path(info, models.user_models_dir()).exists():
            self.go_btn.setText("İndirmeye devam et")
        else:
            self.go_btn.setText(f"İndir ({info.size_text})")

    def _on_gpu(self, devices) -> None:
        info, reason = models.recommend(devices)
        self.gpu_label.setText(f"{reason}  Önerilen: {info.label}.")
        if not self._user_picked and self._download is None:
            self.radios[info.file].setChecked(True)
        for file, radio in self.radios.items():
            label = radio.text().replace("  (önerilen)", "")
            radio.setText(label + ("  (önerilen)" if file == info.file else ""))

    # ----------------------------------------------------------------- indirme
    def _start(self) -> None:
        info = self.selected()
        if self._is_downloaded(info):
            self.chosen = info.file
            self.accept()
            return
        self._download = _DownloadThread(info)
        self._download.progress.connect(self._on_progress)
        self._download.succeeded.connect(self._on_done)
        self._download.failed.connect(self._on_failed)
        self._speed = models.Speedometer()
        for radio in self.radios.values():
            radio.setEnabled(False)
        self.go_btn.hide()
        self.later_btn.hide()
        self.cancel_btn.show()
        self.bar.show()
        self.status.setStyleSheet("")
        self.status.setText("Bağlanılıyor…")
        self._download.start()

    def _on_progress(self, done: float, total: float, stage: str) -> None:
        self.bar.setValue(int(1000 * done / total) if total else 0)
        if stage == "check":
            self.status.setText(f"Önceki indirme kontrol ediliyor… %{100 * done / total:.0f}")
        elif stage == "verify":
            self.status.setText("Doğrulandı, hazırlanıyor…")
        else:
            self.status.setText(self._speed.update(int(done), int(total)))

    def _finish_download(self) -> None:
        if self._download is not None:
            self._download.wait(2000)
        self._download = None
        for radio in self.radios.values():
            radio.setEnabled(True)
        self.cancel_btn.hide()
        self.cancel_btn.setEnabled(True)
        self.go_btn.show()
        self.later_btn.show()

    def _on_done(self, path: str) -> None:
        self._finish_download()
        self.chosen = Path(path).name
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._finish_download()
        self._on_choice_changed()
        if message:
            self.status.setText(f"İndirilemedi: {message}")
            self.status.setStyleSheet("color: #e05050;")
        else:
            self.status.setText("İndirme durduruldu. Daha sonra kaldığı yerden devam edebilirsin.")
            self.status.setStyleSheet("")

    def _cancel_download(self) -> None:
        if self._download is not None:
            self.cancel_btn.setEnabled(False)
            self.status.setText("Durduruluyor…")
            self._download.cancel()

    # ------------------------------------------------------------------ kapatma
    def reject(self) -> None:
        if self._download is not None:  # pencere kapatılırken indirme sürüyorsa önce durdur
            self._download.cancel()
            self._download.wait(5000)
            self._download = None
        super().reject()
