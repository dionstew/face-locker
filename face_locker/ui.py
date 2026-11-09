from __future__ import annotations

import base64
from datetime import datetime
import queue
import time
from zoneinfo import ZoneInfo

from .display import Backlight
from .ipc import Client
from .qt import Qt, QtCore, QtGui, QtWidgets as W, Signal


STYLE = """
* { font-family: 'DejaVu Sans'; font-size: 13px; }
QMainWindow, QWidget#root { background: #101919; color: #edf4ef; }
QWidget { color: #edf4ef; }
QFrame#sidebar { background: #142322; border-right: 1px solid #29403a; }
QLabel#brand { font-size: 21px; font-weight: 800; letter-spacing: 2px; color: #d7f3c2; }
QLabel#eyebrow { color: #a8c18e; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
QLabel#title { font-size: 27px; font-weight: 700; }
QLabel#hero { font-size: 34px; font-weight: 700; }
QLabel#muted { color: #a0b1aa; }
QLabel#metric { font-size: 26px; font-weight: 600; color: #e7f4dd; }
QLabel#chip { background: #243b32; color: #c4e69f; padding: 7px 11px; border-radius: 12px; font-size: 11px; }
QFrame#card { background: #192a26; border: 1px solid #30443c; border-radius: 14px; }
QFrame#heroCard { background: #20382d; border: 1px solid #3b5540; border-radius: 16px; }
QPushButton { background: #263b34; border: 1px solid #3b5147; border-radius: 9px; padding: 10px 14px; min-height: 24px; }
QPushButton:hover { background: #334d40; }
QPushButton:pressed { background: #456544; }
QPushButton:disabled { color: #708078; background: #1c2a25; border-color: #2a3932; }
QPushButton#primary { background: #c4e69f; color: #17271b; border: none; font-weight: 700; padding: 14px 18px; }
QPushButton#primary:hover { background: #d4efb7; }
QPushButton#primary:disabled { background: #536548; color: #a4b29a; }
QPushButton#nav { background: transparent; text-align: left; border: none; padding: 12px; }
QPushButton#nav:checked { background: #2a4235; color: #d3efb4; }
QPushButton#danger { color: #f4b6a6; border-color: #775044; }
QLabel#camera { background: #080f0e; border: 1px solid #3b5147; border-radius: 12px; color: #9aada0; }
QLineEdit, QComboBox { background: #172722; border: 1px solid #49614f; border-radius: 8px; padding: 10px; min-height: 22px; }
QComboBox QAbstractItemView { background: #23392d; color: #eff7ea; selection-background-color: #426044; }
QTableWidget { background: #172622; alternate-background-color: #1c2e27; border: 1px solid #30443c; border-radius: 8px; gridline-color: #30443c; }
QHeaderView::section { background: #263d31; color: #c3d4bc; border: none; padding: 9px; }
QTableWidget::item { padding: 7px; }
QTableWidget::item:selected { background: #3c5841; }
QCalendarWidget QWidget { background: #1c3027; alternate-background-color: #263e2e; }
QCalendarWidget QToolButton { background: #263e2e; color: #e7f2e1; padding: 5px; border-radius: 5px; }
QCalendarWidget QAbstractItemView { selection-background-color: #719352; selection-color: #ffffff; }
QProgressBar { background: #172720; border: 1px solid #3a5141; border-radius: 5px; text-align: center; min-height: 16px; }
QProgressBar::chunk { background: #88ae68; border-radius: 4px; }
QDialog, QMessageBox { background: #192a24; }
QTabWidget::pane { border: 1px solid #30443c; background: #172622; }
QTabBar::tab { background: #20372c; color: #aebeaa; padding: 9px 12px; border: 1px solid #30443c; }
QTabBar::tab:selected { background: #354f36; color: #e2f1d4; }
QScrollBar:vertical { background: #182720; width: 12px; }
QScrollBar::handle:vertical { background: #526c51; min-height: 25px; border-radius: 5px; }
"""

COMPACT = """
* { font-size: 11px; }
QLabel#hero { font-size: 25px; }
QLabel#title { font-size: 21px; }
QLabel#metric { font-size: 20px; }
QLabel#eyebrow { font-size: 9px; }
QPushButton { padding: 6px 10px; min-height: 22px; }
QPushButton#primary { padding: 10px 12px; }
"""


def label(text="", name=None, wrap=False):
    widget = W.QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback, name=None):
    widget = W.QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def card(title):
    frame = W.QFrame()
    frame.setObjectName("card")
    layout = W.QVBoxLayout(frame)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(5)
    layout.addWidget(label(title.upper(), "eyebrow"))
    value = label("—", "metric")
    detail = label("Waiting for service", "muted")
    layout.addWidget(value)
    layout.addWidget(detail)
    return frame, value, detail


class ServiceWorker(QtCore.QThread):
    status = Signal(object)
    response = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, settings):
        super().__init__()
        self.client = Client(settings.socket_path, timeout=1.5)
        self.commands = queue.Queue(maxsize=32)
        self.want_frame = False

    def submit(self, command, **kwargs):
        try:
            self.commands.put_nowait((command, kwargs))
        except queue.Full:
            self.failed.emit(command, "Please wait for the current requests to finish")

    def run(self):
        next_poll = 0.0
        while not self.isInterruptionRequested() or not self.commands.empty():
            try:
                command, kwargs = self.commands.get(timeout=0.05)
            except queue.Empty:
                command = None
            if command:
                try:
                    self.response.emit(command, self.client.call(command, **kwargs))
                except Exception as exc:
                    self.failed.emit(command, str(exc))
            if not self.isInterruptionRequested() and time.monotonic() >= next_poll:
                try:
                    self.status.emit(self.client.call("status", with_frame=self.want_frame))
                except Exception as exc:
                    self.failed.emit("status", str(exc))
                next_poll = time.monotonic() + (0.2 if self.want_frame else 0.6)


class TouchInput(W.QDialog):
    def __init__(self, title, *, secret=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420 if secret else 640)
        layout = W.QVBoxLayout(self)
        layout.addWidget(label(title, "title"))
        self.input = W.QLineEdit()
        self.input.setMaxLength(12 if secret else 60)
        if secret:
            self.input.setEchoMode(W.QLineEdit.Password)
        self.input.returnPressed.connect(self.accept)
        layout.addWidget(self.input)
        rows = ["123", "456", "789", "0"] if secret else ["1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]
        for chars in rows:
            row = W.QHBoxLayout()
            for char in chars:
                key = button(char, lambda checked=False, c=char: self.input.insert(c))
                key.setMinimumWidth(32)
                row.addWidget(key)
            layout.addLayout(row)
        actions = W.QHBoxLayout()
        if not secret:
            actions.addWidget(button("Space", lambda: self.input.insert(" ")))
        actions.addWidget(button("⌫", self.input.backspace))
        actions.addWidget(button("Cancel", self.reject))
        actions.addWidget(button("Continue", self.accept, "primary"))
        layout.addLayout(actions)
        self.input.setFocus()

    @classmethod
    def get(cls, title, parent, secret=False):
        dialog = cls(title, secret=secret, parent=parent)
        accepted = dialog.exec() == W.QDialog.Accepted
        return dialog.input.text(), accepted


class CameraView(W.QLabel):
    def __init__(self):
        super().__init__("Camera is in standby")
        self.setObjectName("camera")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(240, 160)
        self.setSizePolicy(W.QSizePolicy.Ignored, W.QSizePolicy.Expanding)
        self.original = None

    def show_frame(self, data):
        pixmap = QtGui.QPixmap()
        if pixmap.loadFromData(data):
            self.original = pixmap
            self._fit()

    def clear_frame(self):
        self.original = None
        self.clear()
        self.setText("Camera is in standby")

    def _fit(self):
        if self.original:
            self.setPixmap(self.original.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


class SleepOverlay(W.QWidget):
    wake_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setStyleSheet("background: black; color: #33403b;")
        layout = W.QVBoxLayout(self)
        message = label("Touch to wake")
        message.setAlignment(Qt.AlignCenter)
        layout.addWidget(message)
        message.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.StrongFocus)
        self.hide()

    def mousePressEvent(self, event):
        # Wake on release to consume the complete first tap.
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()
        self.wake_requested.emit()

    def keyPressEvent(self, event):
        event.accept()
        self.wake_requested.emit()


class MainWindow(W.QMainWindow):
    def __init__(self, settings, *, windowed=False):
        super().__init__()
        self.settings = settings
        self.zone = ZoneInfo(settings.timezone)
        self.token = None
        self.user_records = []
        self.latest = None
        self.last_frame_id = -1
        self.history_dirty = True
        self.connected = False
        self.backlight = Backlight(settings.backlight_path)
        self.setWindowTitle("Face Locker")
        self.setMinimumSize(800, 480)
        self.resize(1024, 600)
        self.setStyleSheet(STYLE)
        self._compact = False
        root = W.QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = W.QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = W.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(148)
        nav = W.QVBoxLayout(sidebar)
        nav.setContentsMargins(12, 24, 12, 18)
        nav.addWidget(label("FACE\nLOCKER", "brand"))
        nav.addWidget(label("LOCAL ACCESS", "eyebrow"))
        nav.addSpacing(24)
        self.nav_buttons = []
        for index, text in enumerate(("Dashboard", "Camera", "Calendar", "Administration")):
            item = button(text, lambda checked=False, i=index: self.navigate(i), "nav")
            item.setCheckable(True)
            nav.addWidget(item)
            self.nav_buttons.append(item)
        nav.addStretch()
        nav.addWidget(button("Standby", self.standby))
        nav.addWidget(button("Display off", self.sleep_display))
        nav.addWidget(label("RASPBERRY PI 4", "eyebrow"))
        shell.addWidget(sidebar)
        content = W.QVBoxLayout()
        content.setContentsMargins(22, 16, 22, 16)
        content.setSpacing(12)
        top = W.QHBoxLayout()
        self.breadcrumb = label("SYSTEM / OVERVIEW", "eyebrow")
        self.mode_chip = label("CONNECTING", "chip")
        self.clock = label("", "muted")
        top.addWidget(self.breadcrumb)
        top.addStretch()
        top.addWidget(self.mode_chip)
        top.addWidget(self.clock)
        content.addLayout(top)
        self.pages = W.QStackedWidget()
        content.addWidget(self.pages, 1)
        self.notice = label("Connecting to the local application service…", "muted", True)
        self.notice.setMinimumHeight(32)
        content.addWidget(self.notice)
        shell.addLayout(content, 1)
        self._dashboard()
        self._camera()
        self._calendar()
        self._admin()
        self.overlay = SleepOverlay(root)
        self.overlay.wake_requested.connect(self.wake_display)
        self.worker = ServiceWorker(settings)
        self.worker.status.connect(self.on_status)
        self.worker.response.connect(self.on_response)
        self.worker.failed.connect(self.on_failure)
        self.worker.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.navigate(0)
        self.tick()
        if windowed:
            self.show()
        else:
            self.showFullScreen()

    def _dashboard(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        title = W.QHBoxLayout()
        title.addWidget(label("Dashboard", "title"))
        title.addStretch()
        self.ready_text = label("●  Waiting", "muted")
        title.addWidget(self.ready_text)
        layout.addLayout(title)
        hero = W.QFrame()
        hero.setObjectName("heroCard")
        hero_layout = W.QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 18, 22, 18)
        words = W.QVBoxLayout()
        words.addWidget(label("A FAMILIAR FACE. A SIMPLE WELCOME.", "eyebrow", True))
        words.addWidget(label("Your access\nstarts here.", "hero"))
        words.addWidget(label("Tap to start a private, on-device face check.", "muted", True))
        hero_layout.addLayout(words, 1)
        self.start_button = button("Start\nrecognition  →", self.start_scan, "primary")
        self.start_button.setMinimumHeight(90)
        hero_layout.addWidget(self.start_button)
        layout.addWidget(hero, 1)
        grid = W.QGridLayout()
        self.metrics = {}
        for i, (key, title) in enumerate((("battery", "Battery voltage"), ("lock", "Actuator status"),
                                         ("controller", "Serial controller"), ("activity", "Today's activity"))):
            panel, value, detail = card(title)
            self.metrics[key] = (value, detail)
            grid.addWidget(panel, i // 2, i % 2)
        layout.addLayout(grid)
        self.pages.addWidget(page)

    def _camera(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Face recognition", "title"))
        row = W.QHBoxLayout()
        self.camera_view = CameraView()
        row.addWidget(self.camera_view, 1)
        side = W.QVBoxLayout()
        self.scan_state = label("READY", "eyebrow")
        side.addWidget(self.scan_state)
        self.camera_hint = label("Start a check to activate the camera.", "title", True)
        self.camera_hint.setMaximumWidth(230)
        side.addWidget(self.camera_hint)
        self.progress = W.QProgressBar()
        self.progress.hide()
        side.addWidget(self.progress)
        side.addStretch()
        self.camera_start = button("Start recognition", self.start_scan, "primary")
        side.addWidget(self.camera_start)
        side.addWidget(button("Cancel / standby", self.standby))
        if self.settings.demo:
            side.addWidget(label("SIMULATED FACE", "eyebrow"))
            self.scenario = W.QComboBox()
            for text, value in (("Enrolled person", "known"), ("Unknown person", "unknown"),
                                ("Liveness failure", "spoof"), ("No face", "no_face")):
                self.scenario.addItem(text, value)
            self.scenario.currentIndexChanged.connect(
                lambda: self.worker.submit("demo_scenario", scenario=self.scenario.currentData()))
            side.addWidget(self.scenario)
        row.addLayout(side)
        layout.addLayout(row, 1)
        layout.addWidget(label("Camera capture stops in standby. Preview images are never saved.", "muted", True))
        self.pages.addWidget(page)

    def _calendar(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = W.QHBoxLayout()
        title.addWidget(label("Access calendar", "title"))
        title.addStretch()
        title.addWidget(button("Refresh", self.refresh_history))
        layout.addLayout(title)
        row = W.QHBoxLayout()
        self.calendar = W.QCalendarWidget()
        today = datetime.now(self.zone).date()
        self.calendar.setSelectedDate(QtCore.QDate(today.year, today.month, today.day))
        self.calendar.setGridVisible(False)
        self.calendar.setVerticalHeaderFormat(W.QCalendarWidget.NoVerticalHeader)
        self.calendar.setMaximumWidth(340)
        self.calendar.setMinimumWidth(270)
        weekend = QtGui.QTextCharFormat()
        weekend.setForeground(QtGui.QColor("#c7b88d"))
        self.calendar.setWeekdayTextFormat(Qt.Saturday, weekend)
        self.calendar.setWeekdayTextFormat(Qt.Sunday, weekend)
        self.calendar.selectionChanged.connect(self.refresh_history)
        row.addWidget(self.calendar)
        list_layout = W.QVBoxLayout()
        self.history_title = label("No activity yet", "muted", True)
        list_layout.addWidget(self.history_title)
        self.history_table = self.make_table(("Time", "Person", "Outcome"))
        self.event_table = self.make_table(("Time", "Source", "Event"))
        tabs = W.QTabWidget()
        tabs.addTab(self.history_table, "Access attempts")
        tabs.addTab(self.event_table, "Device events")
        list_layout.addWidget(tabs, 1)
        row.addLayout(list_layout, 1)
        layout.addLayout(row, 1)
        layout.addWidget(label("Times shown in " + self.settings.timezone + ". Select an entry for details.", "muted", True))
        self.history_table.cellClicked.connect(self.history_details)
        self.history_records = []
        self.pages.addWidget(page)

    def _admin(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Administration", "title"))
        self.admin_pages = W.QStackedWidget()
        login = W.QWidget()
        login_layout = W.QVBoxLayout(login)
        login_layout.addStretch()
        login_layout.addWidget(label("Manage familiar faces.", "hero"))
        self.admin_explanation = label("Sign in to enroll people and manage their access.", "muted", True)
        login_layout.addWidget(self.admin_explanation)
        login_layout.addWidget(button("Enter administrator PIN", self.login, "primary"))
        if self.settings.demo:
            login_layout.addWidget(label("Demo PIN: 123456 · simulated hardware only", "muted"))
        login_layout.addStretch()
        self.admin_pages.addWidget(login)
        manage = W.QWidget()
        manage_layout = W.QVBoxLayout(manage)
        manage_layout.setContentsMargins(0, 0, 0, 0)
        top = W.QHBoxLayout()
        top.addWidget(button("+ Enroll person", self.enroll, "primary"))
        top.addWidget(button("Refresh", self.refresh_users))
        top.addStretch()
        top.addWidget(button("Sign out", self.logout))
        manage_layout.addLayout(top)
        self.users_table = self.make_table(("Person", "Access", "Samples"))
        manage_layout.addWidget(self.users_table, 1)
        actions = W.QHBoxLayout()
        actions.addWidget(button("Enable / disable selected", self.toggle_user))
        actions.addWidget(button("Delete selected", self.delete_user, "danger"))
        manage_layout.addLayout(actions)
        manage_layout.addWidget(label("Enrollment is supervised. Collect several clear samples with slight angle changes.", "muted", True))
        self.admin_pages.addWidget(manage)
        layout.addWidget(self.admin_pages, 1)
        self.pages.addWidget(page)

    @staticmethod
    def make_table(headers):
        table = W.QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(W.QHeaderView.Stretch)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(44)
        table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        table.setSelectionMode(W.QAbstractItemView.SingleSelection)
        table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        return table

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        for i, item in enumerate(self.nav_buttons):
            item.setChecked(i == index)
        self.breadcrumb.setText(("SYSTEM / OVERVIEW", "ACCESS / CAMERA", "ACTIVITY / CALENDAR", "SYSTEM / ADMINISTRATION")[index])
        if hasattr(self, "worker"):
            self.worker.want_frame = index == 1
            if index == 2:
                self.refresh_history()
            if index == 3 and self.token:
                self.refresh_users()

    def start_scan(self):
        self.worker.submit("scan")

    def standby(self):
        self.worker.submit("standby")

    def login(self):
        pin, accepted = TouchInput.get("Administrator PIN", self, secret=True)
        if accepted:
            self.worker.submit("login", pin=pin)

    def logout(self):
        if self.token:
            self.worker.submit("logout", token=self.token)
        self.token = None
        self.user_records = []
        self.users_table.setRowCount(0)
        self.admin_pages.setCurrentIndex(0)

    def enroll(self):
        name, accepted = TouchInput.get("Name of the person to enroll", self)
        if accepted and name.strip():
            self.worker.submit("enroll", token=self.token, name=name.strip())

    def refresh_users(self):
        if self.token:
            self.worker.submit("users", token=self.token)

    def selected_user(self):
        row = self.users_table.currentRow()
        if 0 <= row < len(self.user_records):
            return self.user_records[row]
        self.notice.setText("Select a person first")
        return None

    def toggle_user(self):
        user = self.selected_user()
        if user:
            self.worker.submit("set_active", token=self.token, user_id=user["id"], active=not bool(user["active"]))

    def delete_user(self):
        user = self.selected_user()
        if user and W.QMessageBox.question(self, "Delete person", f"Delete {user['name']} and their face templates?",
                                           W.QMessageBox.Yes | W.QMessageBox.No, W.QMessageBox.No) == W.QMessageBox.Yes:
            self.worker.submit("delete_user", token=self.token, user_id=user["id"])

    def refresh_history(self):
        if hasattr(self, "worker"):
            self.worker.submit("history", day=self.calendar.selectedDate().toString("yyyy-MM-dd"))
            self.worker.submit("events", day=self.calendar.selectedDate().toString("yyyy-MM-dd"))

    def history_details(self, row, column):
        if row < len(self.history_records):
            record = self.history_records[row]
            self.notice.setText(record["reason"] + (" · Controller: " + record["controller_result"] if record.get("controller_result") else ""))

    def tick(self):
        self.clock.setText(datetime.now(self.zone).strftime("%a %d %b  %H:%M"))

    def on_status(self, status):
        previous_state = self.latest["state"] if self.latest else None
        self.latest, self.connected = status, True
        state = status["state"]
        mode = status["mode"]
        self.mode_chip.setText({"demo": "DEMO · SIMULATED", "observation": "OBSERVATION", "live": "LOCAL · LIVE"}[mode])
        self.notice.setText(status["message"])
        ready = status["camera_ready"] and state not in ("scanning", "enrolling", "unlocking") and not status["controller"]["pending"]
        self.start_button.setEnabled(ready and status["users_count"] > 0)
        self.camera_start.setEnabled(ready and status["users_count"] > 0)
        self.ready_text.setText("●  Ready" if ready else "●  " + state.replace("_", " ").title())
        self.scan_state.setText(state.upper())
        self.camera_hint.setText(status["message"])
        controller = status["controller"]
        voltage = controller["battery_voltage"]
        self.metrics["battery"][0].setText(f"{voltage:.2f} V" if voltage is not None else "Unavailable")
        age = controller["age_seconds"]
        self.metrics["battery"][1].setText(f"Updated {age:.1f}s ago" if controller["telemetry_fresh"] else "Waiting for fresh telemetry")
        self.metrics["lock"][0].setText(controller["actuator"].title())
        self.metrics["lock"][1].setText("Door: " + controller["door"])
        self.metrics["controller"][0].setText("Online" if controller["connected"] else "Offline")
        self.metrics["controller"][1].setText("Simulated serial link" if status["demo"] else self.settings.serial_port)
        self.metrics["activity"][0].setText(str(status["today"]["attempts"]))
        self.metrics["activity"][1].setText(f"{status['today']['authorized']} recognized · {status['users_count']} active people")
        if not status["admin_configured"]:
            self.admin_explanation.setText("Set an administrator PIN on the Pi with: python app.py --set-admin-pin")
        if status["frame"] and status["frame_id"] != self.last_frame_id:
            self.camera_view.show_frame(base64.b64decode(status["frame"]))
            self.last_frame_id = status["frame_id"]
        elif state in ("idle", "unavailable"):
            self.camera_view.clear_frame()
        session = status.get("session")
        enrolling = bool(session and session["kind"] == "enroll")
        self.progress.setVisible(enrolling)
        if enrolling:
            self.progress.setMaximum(session["target"])
            self.progress.setValue(session["samples"])
        if previous_state in ("scanning", "enrolling", "unlocking") and state not in ("scanning", "enrolling", "unlocking"):
            self.history_dirty = True
            if self.pages.currentIndex() == 2:
                self.refresh_history()

    def on_response(self, command, data):
        if command in ("scan", "enroll"):
            self.navigate(1)
        elif command == "standby":
            self.navigate(0)
        elif command == "login":
            self.token = data["token"]
            self.admin_pages.setCurrentIndex(1)
            self.refresh_users()
        elif command == "users":
            self.user_records = data
            self.users_table.setRowCount(len(data))
            for row, user in enumerate(data):
                for column, value in enumerate((user["name"], "Enabled" if user["active"] else "Disabled", user["samples"])):
                    self.users_table.setItem(row, column, W.QTableWidgetItem(str(value)))
        elif command in ("delete_user", "set_active"):
            self.refresh_users()
        elif command == "history":
            self.history_records = data
            self.history_title.setText(f"{self.calendar.selectedDate().toString('dd MMM yyyy')} · {len(data)} attempts")
            self.history_table.setRowCount(len(data))
            for row, record in enumerate(data):
                at = datetime.fromisoformat(record["started_at"]).astimezone(self.zone).strftime("%H:%M:%S")
                for column, value in enumerate((at, record["name"] or "—", record["decision"].replace("_", " ").title())):
                    item = W.QTableWidgetItem(value)
                    item.setToolTip(record["reason"])
                    self.history_table.setItem(row, column, item)
        elif command == "events":
            self.event_table.setRowCount(len(data))
            for row, record in enumerate(data):
                at = datetime.fromisoformat(record["timestamp"]).astimezone(self.zone).strftime("%H:%M:%S")
                for column, value in enumerate((at, record["source"], record["event_type"].replace("_", " "))):
                    item = W.QTableWidgetItem(value)
                    item.setToolTip(record["details"])
                    self.event_table.setItem(row, column, item)

    def on_failure(self, command, error):
        if command == "status":
            self.connected = False
            self.mode_chip.setText("SERVICE OFFLINE")
            self.ready_text.setText("●  Unavailable")
            self.start_button.setEnabled(False)
            self.camera_start.setEnabled(False)
            self.notice.setText("Local service unavailable · " + error)
            for key in ("battery", "lock", "controller"):
                self.metrics[key][0].setText("Unavailable")
                self.metrics[key][1].setText("Waiting for service")
            self.camera_view.clear_frame()
        else:
            self.notice.setText(error)
            W.QMessageBox.warning(self, "Action unavailable", error)
            if "session expired" in error.lower():
                self.logout()

    def sleep_display(self):
        self.worker.submit("standby")
        self.logout()
        self.worker.want_frame = False
        self.overlay.setGeometry(self.centralWidget().rect())
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.setFocus()
        try:
            self.backlight.off()
        except OSError as exc:
            self.notice.setText("Screen blanked; backlight control unavailable: " + str(exc))

    def wake_display(self):
        try:
            self.backlight.wake()
        except OSError as exc:
            self.notice.setText("Backlight wake failed: " + str(exc))
        self.overlay.hide()
        self.navigate(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.height() < 560
        if hasattr(self, "_compact") and compact != self._compact:
            self._compact = compact
            self.setStyleSheet(STYLE + (COMPACT if compact else ""))
        if hasattr(self, "overlay"):
            self.overlay.setGeometry(self.centralWidget().rect())

    def closeEvent(self, event):
        try:
            self.backlight.wake()
        except OSError:
            pass
        self.worker.submit("standby")
        if self.token:
            self.worker.submit("logout", token=self.token)
        self.worker.requestInterruption()
        self.worker.wait(4500)
        event.accept()


def run_ui(settings, windowed=False):
    from .qt import check_runtime
    check_runtime()
    app = W.QApplication.instance() or W.QApplication([])
    app.setApplicationName("Face Locker")
    window = MainWindow(settings, windowed=windowed)
    return app.exec()
