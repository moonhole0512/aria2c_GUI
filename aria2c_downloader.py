# downloader_gui.py
import sys
import os
import re
import asyncio
from threading import Thread
import shutil
import urllib.parse # 추가

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QFileDialog, QTableWidget, QTableWidgetItem,
    QLabel, QProgressBar, QHeaderView, QMessageBox, QSpinBox
)
from PySide6.QtCore import Qt, Signal, QObject
import qdarkstyle

# ---------------------------
# Signals object (thread-safe emit)
# ---------------------------
class DownloadSignals(QObject):
    progress = Signal(int, int)     # row, percent
    status = Signal(int, str)       # row, status text
    speed = Signal(int, str)        # row, speed text
    eta = Signal(int, str)          # row, eta text
    finished = Signal(int, str)     # row, filepath or message
    failed = Signal(int, str)       # row, error


# ---------------------------
# Asyncio-based Download Manager using aria2c
# ---------------------------
class Aria2DownloadManager:
    def __init__(self, max_concurrent=3, aria_opts=None):
        self.queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.loop = None
        self.thread = None
        self.running = False
        self.aria_opts = aria_opts or []
        # regex patterns to parse aria2c stdout lines
        self.re_percent = re.compile(r'(\d{1,3})%')
        self.re_speed = re.compile(r'([\d\.]+(?:KiB|MiB|GiB)/s)')
        self.re_eta = re.compile(r'ETA\s+([0-9:\-]+)')  # ETA 00:01:23 or '-' sometimes

        # 임시 다운로드 폴더
        self.temp_dir = "temp"
        os.makedirs(self.temp_dir, exist_ok=True)


    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        # create and run an asyncio loop in separate thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # spawn worker tasks
        for _ in range(self.max_concurrent):
            self.loop.create_task(self._worker())
        self.loop.run_forever()

    async def _worker(self):
        while True:
            job = await self.queue.get()
            try:
                await self._run_aria2(job)
            except Exception as e:
                # job contains (row, url, folder, filename, signals)
                row, *_ = job
                _, _, _, _, signals = job
                signals.failed.emit(row, str(e))
            finally:
                self.queue.task_done()

    async def _run_aria2(self, job):
        # job: (row, url, folder, filename, signals)
        row, url, folder, filename, signals = job
        final_path = os.path.join(folder, filename)

        # Build aria2c command
        # -x 16 -s 16 -c : multi-connection + resume
        # --max-tries, --retry-wait : retry behavior
        cmd = [
            "aria2c",
            "-x", "16",
            "-s", "16",
            "-c",
            "--enable-color=false",
            "--summary-interval=1",
            "--max-tries=5",
            "--retry-wait=5",
            "-d", self.temp_dir,  # 임시 폴더로 변경
            "-o", filename,
            url
        ]
        # include any extra options from self.aria_opts
        if self.aria_opts:
            cmd = self.aria_opts + cmd

        # Emit start status
        signals.status.emit(row, "대기 → 시작")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        # read stdout lines and parse progress/speed/eta
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    text = line.decode(errors="ignore").strip()
                except:
                    text = str(line)

                # try parse percent
                m_pct = self.re_percent.search(text)
                if m_pct:
                    try:
                        pct = int(m_pct.group(1))
                        if pct < 0:
                            pct = 0
                        elif pct > 100:
                            pct = 100
                        signals.progress.emit(row, pct)
                    except:
                        pass

                # parse speed
                m_speed = self.re_speed.search(text)
                if m_speed:
                    signals.speed.emit(row, m_speed.group(1))

                # parse ETA
                m_eta = self.re_eta.search(text)
                if m_eta:
                    signals.eta.emit(row, m_eta.group(1))

                # update text status line for verbose messages
                # keep the most recent short text (truncate to reasonable length)
                if len(text) > 0:
                    short = text if len(text) < 120 else text[:120] + "..."
                    signals.status.emit(row, short)

            rc = await process.wait()
            if rc == 0:
                signals.progress.emit(row, 100)
                signals.speed.emit(row, "")
                signals.eta.emit(row, "00:00:00")
                signals.status.emit(row, "다운로드 완료, 파일 이동 중...")

                # 다운로드가 성공하면 파일을 최종 목적지로 이동
                temp_path = os.path.join(self.temp_dir, filename)
                try:
                    shutil.move(temp_path, final_path)
                    signals.finished.emit(row, final_path)
                    signals.status.emit(row, "완료")
                except Exception as e:
                    signals.failed.emit(row, f"파일 이동 실패: {e}")
                    signals.status.emit(row, "이동 실패")

            else:
                signals.failed.emit(row, f"aria2c 실패 (코드 {rc})")
                signals.status.emit(row, f"실패 (코드 {rc})")
        except Exception as e:
            # ensure process termination
            try:
                process.kill()
            except:
                pass
            signals.failed.emit(row, str(e))
            signals.status.emit(row, f"예외: {e}")

    def add(self, row, url, folder, filename, signals):
        # add job to asyncio queue from any thread
        if not self.loop:
            raise RuntimeError("다운로드 매니저가 시작되지 않았습니다.")
        asyncio.run_coroutine_threadsafe(self.queue.put((row, url, folder, filename, signals)), self.loop)


# ---------------------------
# GUI Application
# ---------------------------
class DownloaderUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("모던 aria2c 다운로드 매니저")
        self.resize(1000, 600)

        # 기본 저장 경로
        self.save_path = os.getcwd()

        # UI 위젯
        self._build_ui()

        # signals per row
        self.signals_by_row = {}

        # manager: 동시 3개
        self.manager = Aria2DownloadManager(max_concurrent=3)
        self.manager.start()

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        # top controls: path, concurrency control
        top = QHBoxLayout()
        self.path_label = QLabel(f"저장 경로: {self.save_path}")
        btn_browse = QPushButton("경로 변경")
        btn_browse.clicked.connect(self._choose_folder)
        top.addWidget(self.path_label)
        top.addWidget(btn_browse)

        top.addStretch()

        top.addWidget(QLabel("동시 다운로드 최대:"))
        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 8)
        self.spin_concurrency.setValue(3)
        self.spin_concurrency.valueChanged.connect(self._change_concurrency)
        top.addWidget(self.spin_concurrency)

        layout.addLayout(top)

        # URL input row
        input_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("다운로드할 URL을 입력하세요")
        btn_add = QPushButton("추가")
        btn_add.clicked.connect(self._add_task_from_input)
        input_layout.addWidget(self.url_input)
        input_layout.addWidget(btn_add)
        layout.addLayout(input_layout)

        # table: URL | 상태 | progress bar | speed | ETA
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["URL", "상태", "진행률", "속도", "ETA"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)

        layout.addWidget(self.table)

        # footer: 실행/중지 (for future), clear etc.
        footer = QHBoxLayout()
        btn_clear = QPushButton("완료된 항목 제거")
        btn_clear.clicked.connect(self._clear_finished)
        footer.addWidget(btn_clear)
        footer.addStretch()
        layout.addLayout(footer)

        self.setCentralWidget(central)

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "저장 경로 선택", self.save_path)
        if path:
            self.save_path = path
            self.path_label.setText(f"저장 경로: {self.save_path}")

    def _change_concurrency(self, val):
        # 재시작 필요하면 재시작 로직을 추가해야 함.
        # 현재는 간단히 알림만 표시.
        QMessageBox.information(self, "알림", "동시 다운로드 수는 프로그램 재시작 후 완전히 반영될 수 있습니다.")
        # (원하면 manager 재생성 로직 추가 가능)

    def _add_task_from_input(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "경고", "URL을 입력하세요.")
            return

        # 'rule34video.com' 링크에 대한 예외 처리
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.netloc == 'rule34video.com':
            # 쿼리 파라미터에서 download_filename을 추출
            query_params = urllib.parse.parse_qs(parsed_url.query)
            if 'download_filename' in query_params:
                filename = query_params['download_filename'][0]
                # 다운로드 링크에서 쿼리 파라미터 제거
                clean_url = parsed_url._replace(query='', params='').geturl()
                url = clean_url
            else:
                filename = os.path.basename(parsed_url.path) or "download"
        else:
            # 일반적인 링크 처리
            filename = os.path.basename(url.split("?", 1)[0]) or "download"

        row = self._append_row(url, "대기 중", 0, "", "")
        
        sig = DownloadSignals()
        
        # 람다 함수를 사용하여 시그널과 슬롯 연결
        sig.progress.connect(lambda row, p: self._on_progress(row, p))
        sig.status.connect(lambda row, s: self._on_status(row, s))
        sig.speed.connect(lambda row, s: self._on_speed(row, s))
        sig.eta.connect(lambda row, e: self._on_eta(row, e))
        sig.finished.connect(lambda row, p: self._on_finished(row, p))
        sig.failed.connect(lambda row, e: self._on_failed(row, e))
        
        self.signals_by_row[row] = sig
    
        try:
            self.manager.add(row, url, self.save_path, filename, sig)
            self.url_input.clear()
            self._on_status(row, "큐에 추가됨")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"다운로드 추가 실패: {e}")

    def _append_row(self, url, status, pct, speed_text, eta_text):
        r = self.table.rowCount()
        self.table.insertRow(r)
        # URL
        url_item = QTableWidgetItem(url)
        url_item.setFlags(url_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 0, url_item)
        # Status
        status_item = QTableWidgetItem(status)
        status_item.setFlags(status_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 1, status_item)
        # ProgressBar in column 2
        pb = QProgressBar()
        pb.setValue(int(pct))
        self.table.setCellWidget(r, 2, pb)
        # speed
        sp_item = QTableWidgetItem(speed_text)
        sp_item.setFlags(sp_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 3, sp_item)
        # eta
        eta_item = QTableWidgetItem(eta_text)
        eta_item.setFlags(eta_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 4, eta_item)
        return r

    # slot handlers to update GUI (called from other thread via signals)
    def _on_progress(self, row, pct):
        widget = self.table.cellWidget(row, 2)
        if isinstance(widget, QProgressBar):
            widget.setValue(int(pct))

    def _on_status(self, row, text):
        if row < self.table.rowCount():
            self.table.item(row, 1).setText(text)

    def _on_speed(self, row, text):
        if row < self.table.rowCount():
            self.table.item(row, 3).setText(text)

    def _on_eta(self, row, text):
        if row < self.table.rowCount():
            self.table.item(row, 4).setText(text)

    def _on_finished(self, row, path):
        self._on_status(row, "완료")
        self._on_progress(row, 100)
        self._on_speed(row, "")
        self._on_eta(row, "00:00:00")
        QMessageBox.information(self, "다운로드 완료", f"행 {row} 완료: {path}")

    def _on_failed(self, row, err):
        self._on_status(row, f"실패: {err}")
        QMessageBox.warning(self, "다운로드 실패", f"행 {row} 실패: {err}")

    def _clear_finished(self):
        # remove rows where status == '완료' or contains '실패'
        to_remove = []
        for r in range(self.table.rowCount()):
            st = self.table.item(r, 1).text()
            if "완료" in st or "실패" in st:
                to_remove.append(r)
        # remove from bottom up
        for r in reversed(to_remove):
            self.table.removeRow(r)

# ---------------------------
# Main
# ---------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api='pyside6')) 
    win = DownloaderUI()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()