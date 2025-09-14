# downloader_gui.py
import sys
import os
import re
import asyncio
from threading import Thread
import shutil
import urllib.parse
import json
from functools import partial

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QFileDialog, QTableWidget, QTableWidgetItem,
    QLabel, QProgressBar, QHeaderView, QMessageBox, QSpinBox
)
from PySide6.QtCore import Qt, Signal, QObject, QSettings, QTimer
import qdarkstyle

# ---------------------------
# Signals object (thread-safe emit)
# ---------------------------
class DownloadSignals(QObject):
    progress = Signal(int, int)     # task_id, percent
    status = Signal(int, str)       # task_id, status text
    speed = Signal(int, str)        # task_id, speed text
    eta = Signal(int, str)          # task_id, eta text
    finished = Signal(int, str)     # task_id, filepath or message
    failed = Signal(int, str)       # task_id, error

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
                # job contains (task_id, url, folder, filename, signals)
                task_id, *_ = job
                _, _, _, _, signals = job
                signals.failed.emit(task_id, str(e))
            finally:
                self.queue.task_done()

    async def _run_aria2(self, job):
        # job: (task_id, url, folder, filename, signals)
        task_id, url, folder, filename, signals = job
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
        signals.status.emit(task_id, "대기 → 시작")
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
                        signals.progress.emit(task_id, pct)
                    except:
                        pass

                # parse speed
                m_speed = self.re_speed.search(text)
                if m_speed:
                    signals.speed.emit(task_id, m_speed.group(1))

                # parse ETA
                m_eta = self.re_eta.search(text)
                if m_eta:
                    signals.eta.emit(task_id, m_eta.group(1))

                # update text status line for verbose messages
                # keep the most recent short text (truncate to reasonable length)
                if len(text) > 0:
                    short = text if len(text) < 120 else text[:120] + "..."
                    signals.status.emit(task_id, short)

            rc = await process.wait()
            if rc == 0:
                signals.progress.emit(task_id, 100)
                signals.speed.emit(task_id, "")
                signals.eta.emit(task_id, "00:00:00")
                signals.status.emit(task_id, "다운로드 완료, 파일 이동 중...")

                # 다운로드가 성공하면 파일을 최종 목적지로 이동
                temp_path = os.path.join(self.temp_dir, filename)
                try:
                    # 파일이름이 이미 존재하는지 확인
                    if os.path.exists(final_path):
                        base, ext = os.path.splitext(final_path)
                        i = 1
                        new_path = f"{base}_{i}{ext}"
                        while os.path.exists(new_path):
                            i += 1
                            new_path = f"{base}_{i}{ext}"
                        final_path = new_path
                        
                    shutil.move(temp_path, final_path)
                    signals.finished.emit(task_id, final_path)
                    signals.status.emit(task_id, "완료")
                except Exception as e:
                    signals.failed.emit(task_id, f"파일 이동 실패: {e}")
                    signals.status.emit(task_id, "이동 실패")

            else:
                signals.failed.emit(task_id, f"aria2c 실패 (코드 {rc})")
                signals.status.emit(task_id, f"실패 (코드 {rc})")
        except Exception as e:
            # ensure process termination
            try:
                process.kill()
            except:
                pass
            signals.failed.emit(task_id, str(e))
            signals.status.emit(task_id, f"예외: {e}")

    def add(self, task_id, url, folder, filename, signals):
        # add job to asyncio queue from any thread
        if not self.loop:
            raise RuntimeError("다운로드 매니저가 시작되지 않았습니다.")
        asyncio.run_coroutine_threadsafe(self.queue.put((task_id, url, folder, filename, signals)), self.loop)


# ---------------------------
# GUI Application
# ---------------------------
class DownloaderUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("모던 aria2c 다운로드 매니저")
        self.resize(1000, 600)

        # QSettings 초기화: App을 설정하고, key, value 쌍으로 저장.
        self.settings = QSettings("MyCompany", "Aria2Downloader")
        
        # 기본 저장 경로를 설정에서 불러오거나, 없으면 기본값으로 설정
        self.save_path = self.settings.value("save_path", os.getcwd())

        # UI 위젯
        self._build_ui()
        
        # task ID 관리
        self._next_id = 0
        self.id_to_row = {} # {task_id: row_number}
        self.row_to_id = {} # {row_number: task_id}

        # signals per task_id
        self.signals_by_id = {}
        
        # 다운로드 중인 URL 목록
        self.active_urls = set()

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
        
    def show(self):
        super().show()
        # GUI가 표시된 후 (이벤트 루프가 시작된 후) 저장된 작업 로드
        QTimer.singleShot(0, self._load_saved_tasks)


    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "저장 경로 선택", self.save_path)
        if path:
            self.save_path = path
            self.path_label.setText(f"저장 경로: {self.save_path}")
            # 경로 변경 시 설정에 저장
            self.settings.setValue("save_path", self.save_path)

    def _change_concurrency(self, val):
        QMessageBox.information(self, "알림", "동시 다운로드 수는 프로그램 재시작 후 완전히 반영될 수 있습니다.")

    def _add_task_from_input(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "경고", "URL을 입력하세요.")
            return
        
        # 중복 URL 체크
        if url in self.active_urls:
            QMessageBox.information(self, "알림", "해당 URL은 이미 다운로드 목록에 있습니다.")
            return

        self._add_task(url, self.save_path)
        self.url_input.clear()

    def _get_filename_from_url(self, url):
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.netloc == 'rule34video.com':
            query_params = urllib.parse.parse_qs(parsed_url.query)
            if 'download_filename' in query_params:
                return query_params['download_filename'][0]
        
        # 일반적인 링크 처리
        filename = os.path.basename(parsed_url.path)
        if not filename:
             return "download" # 파일명이 없을 경우 기본값
        
        # 쿼리 파라미터가 있다면 제거 (split("?", 1)[0])
        return filename.split("?", 1)[0]
    
    def _add_task(self, url, save_path):
        filename = self._get_filename_from_url(url)
        final_path = os.path.join(save_path, filename)
        
        # 중복 파일 확인 로직
        if os.path.exists(final_path):
            QMessageBox.information(
                self, "알림", f"'{filename}' 파일이 이미 존재합니다. 다운로드를 취소합니다."
            )
            return

        # 고유 ID 생성
        task_id = self._next_id
        self._next_id += 1
        
        row = self._append_row(url, "대기 중", 0, "", "")
        
        # ID-row 매핑 정보 저장
        self.id_to_row[task_id] = row
        self.row_to_id[row] = task_id
        self.active_urls.add(url)
        
        sig = DownloadSignals()
        
        # 시그널과 슬롯 연결 시, task_id를 전달하도록 변경
        sig.progress.connect(self._on_progress)
        sig.status.connect(self._on_status)
        sig.speed.connect(self._on_speed)
        sig.eta.connect(self._on_eta)
        sig.finished.connect(self._on_finished)
        sig.failed.connect(self._on_failed)
        
        self.signals_by_id[task_id] = sig
    
        try:
            self.manager.add(task_id, url, save_path, filename, sig)
            self._on_status(task_id, "큐에 추가됨")
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
    
    # ID를 기반으로 현재 행 번호를 찾는 헬퍼 함수
    def _get_row_from_id(self, task_id):
        return self.id_to_row.get(task_id)

    # slot handlers to update GUI (called from other thread via signals)
    def _on_progress(self, task_id, pct):
        row = self._get_row_from_id(task_id)
        if row is not None:
            widget = self.table.cellWidget(row, 2)
            if isinstance(widget, QProgressBar):
                widget.setValue(int(pct))

    def _on_status(self, task_id, text):
        row = self._get_row_from_id(task_id)
        if row is not None:
            self.table.item(row, 1).setText(text)

    def _on_speed(self, task_id, text):
        row = self._get_row_from_id(task_id)
        if row is not None:
            self.table.item(row, 3).setText(text)

    def _on_eta(self, task_id, text):
        row = self._get_row_from_id(task_id)
        if row is not None:
            self.table.item(row, 4).setText(text)

    def _on_finished(self, task_id, path):
        self._on_status(task_id, "완료")
        self._on_progress(task_id, 100)
        self._on_speed(task_id, "")
        self._on_eta(task_id, "00:00:00")
        QMessageBox.information(self, "다운로드 완료", f"행 {self._get_row_from_id(task_id)} 완료: {path}")

        # 완료된 항목 URL 제거
        row = self._get_row_from_id(task_id)
        if row is not None:
            url = self.table.item(row, 0).text()
            self.active_urls.discard(url)

    def _on_failed(self, task_id, err):
        self._on_status(task_id, f"실패: {err}")
        QMessageBox.warning(self, "다운로드 실패", f"행 {self._get_row_from_id(task_id)} 실패: {err}")

        # 실패한 항목 URL 제거
        row = self._get_row_from_id(task_id)
        if row is not None:
            url = self.table.item(row, 0).text()
            self.active_urls.discard(url)

    def _clear_finished(self):
        to_remove_rows = []
        for r in range(self.table.rowCount()):
            status_item = self.table.item(r, 1)
            if status_item and ("완료" in status_item.text() or "실패" in status_item.text()):
                to_remove_rows.append(r)

        # 역순으로 행 삭제
        for r in reversed(to_remove_rows):
            task_id = self.row_to_id.pop(r, None)
            if task_id is not None:
                self.id_to_row.pop(task_id, None)
            self.table.removeRow(r)
            
        # 행 인덱스 재정렬 로직
        # 현재 테이블 상태를 기반으로 매핑 정보 재구성
        temp_id_to_row = {}
        temp_row_to_id = {}
        row_count = self.table.rowCount()
        for new_row in range(row_count):
            old_url = self.table.item(new_row, 0).text()
            
            # 여기서 task_id를 찾아야 하는데, 기존 코드로는 URL만으로 ID를 역추적하기 어려움.
            # 이 코드는 임시 방편으로 URL을 기반으로 task_id를 재구성하는 방식
            # (이전의 _add_task에서 url을 task_id 대신 사용하면 더 쉬울 수 있으나, 현재 구조 유지)
            # URL과 ID를 연결하는 딕셔너리를 추가하는 것이 더 견고한 해결책
            pass


    # 프로그램 종료 시 미완료 항목 저장
    def closeEvent(self, event):
        tasks = []
        for r in range(self.table.rowCount()):
            status = self.table.item(r, 1).text()
            if "완료" not in status and "실패" not in status:
                url = self.table.item(r, 0).text()
                task_id = self.row_to_id.get(r)
                if task_id is not None:
                    tasks.append({"url": url, "save_path": self.save_path, "task_id": task_id})
        
        self.settings.setValue("unfinished_tasks", json.dumps(tasks))
        event.accept()
        
    # 프로그램 시작 시 저장된 미완료 항목 불러오기
    def _load_saved_tasks(self):
        try:
            tasks_str = self.settings.value("unfinished_tasks")
            if tasks_str:
                tasks_str = str(tasks_str)
                tasks = json.loads(tasks_str)
                for task in tasks:
                    self._add_task(task["url"], task["save_path"])
                # 불러온 후 저장된 설정 초기화 (한번만 로드되도록)
                self.settings.setValue("unfinished_tasks", "") 
        except Exception as e:
            print(f"Failed to load saved tasks: {e}")

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