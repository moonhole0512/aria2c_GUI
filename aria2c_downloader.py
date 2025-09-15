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
            "--auto-file-renaming=false",
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
# Notification Widget
# ---------------------------
class NotificationLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.BypassWindowManagerHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("""
            background-color: rgba(30, 30, 30, 200);
            color: #ffffff;
            border-radius: 8px;
            padding: 10px;
        """)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)

    def show_message(self, message, duration=3000):
        self.setText(message)
        self.adjustSize()
        # Position the notification at the bottom right corner of the main window
        parent_rect = self.parent().geometry()
        self.move(parent_rect.right() - self.width() - 20, parent_rect.bottom() - self.height() - 20)
        self.show()
        self.hide_timer.start(duration)

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

        # Notification Label 추가
        self.notification_label = NotificationLabel(self)

        # UI 위젯
        self._build_ui()
        
        # task ID 관리
        self._next_id = 0
        self.id_to_row = {} # {task_id: row_number}
        self.row_to_id = {} # {row_number: task_id}
        
        # --- 추가: task_id에 해당하는 URL을 저장할 딕셔너리 ---
        self.url_by_id = {}

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
        # --- 수정: "URL"을 "파일명"으로 변경합니다. ---
        self.table.setHorizontalHeaderLabels(["파일명", "상태", "진행률", "속도", "ETA"])
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
            self.notification_label.show_message("경고: URL을 입력하세요.")
            return
        
        # 중복 URL 체크
        if url in self.active_urls:
            self.notification_label.show_message("알림: 해당 URL은 이미 다운로드 목록에 있습니다.")
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

    # 버그 수정: 최종 경로에 파일이 존재할 경우에만 중복 체크
    def _add_task(self, url, save_path):
        filename = self._get_filename_from_url(url)
        final_path = os.path.join(save_path, filename)

        if os.path.exists(final_path):
            self.notification_label.show_message(
                f"알림: '{filename}' 파일이 이미 존재합니다. 다운로드를 취소합니다."
            )
            return

        # 고유 ID 생성
        task_id = self._next_id
        self._next_id += 1
        
        row = self._append_row(filename, "대기 중", 0, "", "")
        
        # ID-row 매핑 정보 저장
        self.id_to_row[task_id] = row
        self.row_to_id[row] = task_id
        
        # URL 정보도 별도로 저장
        self.url_by_id[task_id] = url
        self.active_urls.add(url)
        
        sig = DownloadSignals()
        
        # 시그널과 슬롯 연결
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
            self.notification_label.show_message(f"오류: 다운로드 추가 실패: {e}")

    # 추가된 메서드: 이어받기용. 파일 존재 여부 체크를 하지 않습니다.
    def _add_task_on_resume(self, url, save_path):
        filename = self._get_filename_from_url(url)
        
        # 고유 ID 생성
        task_id = self._next_id
        self._next_id += 1
        
        row = self._append_row(filename, "대기 중", 0, "", "")
        
        # ID-row 매핑 정보 저장
        self.id_to_row[task_id] = row
        self.row_to_id[row] = task_id
        
        # URL 정보도 별도로 저장
        self.url_by_id[task_id] = url
        self.active_urls.add(url)
        
        sig = DownloadSignals()
        
        # 시그널과 슬롯 연결
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
            self.notification_label.show_message(f"오류: 다운로드 추가 실패: {e}")

    def _append_row(self, filename, status, pct, speed_text, eta_text):
        r = self.table.rowCount()
        self.table.insertRow(r)
        filename_item = QTableWidgetItem(filename)
        filename_item.setFlags(filename_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 0, filename_item)
        status_item = QTableWidgetItem(status)
        status_item.setFlags(status_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 1, status_item)
        pb = QProgressBar()
        pb.setValue(int(pct))
        self.table.setCellWidget(r, 2, pb)
        sp_item = QTableWidgetItem(speed_text)
        sp_item.setFlags(sp_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 3, sp_item)
        eta_item = QTableWidgetItem(eta_text)
        eta_item.setFlags(eta_item.flags() ^ Qt.ItemIsEditable)
        self.table.setItem(r, 4, eta_item)
        return r
    
    def _get_row_from_id(self, task_id):
        return self.id_to_row.get(task_id)

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
        
        self.notification_label.show_message(f"다운로드 완료: 행 {self._get_row_from_id(task_id)} - {path}")

        url = self.url_by_id.get(task_id)
        if url:
            self.active_urls.discard(url)

    def _on_failed(self, task_id, err):
        self._on_status(task_id, f"실패: {err}")
        
        self.notification_label.show_message(f"다운로드 실패: 행 {self._get_row_from_id(task_id)} - {err}")

        url = self.url_by_id.get(task_id)
        if url:
            self.active_urls.discard(url)

    def _clear_finished(self):
        unfinished_tasks = []
        for r in range(self.table.rowCount()):
            status_item = self.table.item(r, 1)
            if status_item and "완료" not in status_item.text() and "실패" not in status_item.text():
                task_id = self.row_to_id.get(r)
                if task_id is not None:
                    url = self.url_by_id.get(task_id)
                    unfinished_tasks.append({
                        "task_id": task_id,
                        "url": url,
                        "filename": self.table.item(r, 0).text(),
                        "status": status_item.text(),
                        "progress_value": self.table.cellWidget(r, 2).value() if self.table.cellWidget(r, 2) else 0,
                        "speed": self.table.item(r, 3).text(),
                        "eta": self.table.item(r, 4).text()
                    })

        self.table.setRowCount(0)
        self.id_to_row.clear()
        self.row_to_id.clear()
        self.url_by_id.clear()
        self.active_urls.clear()
        
        for task_data in unfinished_tasks:
            r = self._append_row(
                task_data['filename'], 
                task_data['status'], 
                task_data['progress_value'], 
                task_data['speed'], 
                task_data['eta']
            )
            self.id_to_row[task_data['task_id']] = r
            self.row_to_id[r] = task_data['task_id']
            self.url_by_id[task_data['task_id']] = task_data['url']
            self.active_urls.add(task_data['url'])
            
            self._on_status(task_data['task_id'], "큐에 추가됨")

    def closeEvent(self, event):
        tasks = []
        for task_id, url in self.url_by_id.items():
            row = self.id_to_row.get(task_id)
            if row is not None:
                status = self.table.item(row, 1).text()
                if "완료" not in status and "실패" not in status:
                    tasks.append({"url": url, "save_path": self.save_path})
        
        try:
            self.settings.setValue("unfinished_tasks", json.dumps(tasks))
        except Exception as e:
            self.notification_label.show_message(f"오류: 프로그램 상태 저장 실패: {e}")
            
        event.accept()
        
    def _load_saved_tasks(self):
        try:
            tasks_str = self.settings.value("unfinished_tasks")
            if tasks_str:
                tasks_str = str(tasks_str)
                tasks = json.loads(tasks_str)
                for task in tasks:
                    # 버그 수정: 재시작 시에는 파일 존재 여부 검사 없이 재개
                    self._add_task_on_resume(task["url"], task["save_path"])
                self.settings.setValue("unfinished_tasks", "") 
        except Exception as e:
            self.notification_label.show_message(f"오류: 저장된 다운로드 목록을 불러오지 못했습니다. {e}")

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