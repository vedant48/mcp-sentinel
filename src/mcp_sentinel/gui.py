from __future__ import annotations

import asyncio
import json
import threading
import time

from aiohttp import web
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .approval import ApprovalQueue
from .gateway import Gateway

STYLE = """
QWidget { background: #10151f; color: #e9edf5; font: 14px 'DejaVu Sans'; }
QLabel#brand { font-size: 27px; font-weight: 700; color: #f5f7fb; }
QLabel#muted { color: #99a8be; }
QLabel#eyebrow { color: #6adbc0; font-size: 11px; font-weight: 700; }
QLabel#title { font-size: 22px; font-weight: 600; }
QLabel#stats { background: #1b2533; padding: 16px; border-radius: 9px; color: #b8c8dd; }
QListWidget, QTextEdit { background: #161e2b; border: 1px solid #2b374a;
                       border-radius: 8px; padding: 10px; }
QListWidget::item { padding: 15px 9px; margin-bottom: 5px; border-radius: 5px; }
QListWidget::item:selected { background: #263c49; color: #8eefd5; }
QTextEdit { font: 13px 'DejaVu Sans Mono'; color: #c7d5e8; }
QPushButton { padding: 12px 23px; border: 1px solid #3a465a;
              border-radius: 6px; background: #202b3a; font-weight: 600; }
QPushButton#allow { background: #66dfbd; color: #08251c; border: none; }
QPushButton#block { color: #ffb3b9; border: 1px solid #a04b5b; background: #35212d; }
QPushButton:disabled, QPushButton#allow:disabled, QPushButton#block:disabled {
    background: #1b2330; color: #627087; border-color: #2b3545; }
QSplitter::handle { background: #10151f; width: 18px; }
"""


def label(text, name=None):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


class Events(QObject):
    snapshot = Signal(object)
    failed = Signal(str)
    ready = Signal()


class Service:
    def __init__(self, options, events):
        self.options = options
        self.events = events
        self.loop = asyncio.new_event_loop()
        self.queue = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="gateway")
        self.runners = []

    def snapshot(self):
        def pack(item):
            return {
                "key": item.key,
                "title": item.title,
                "message": item.message,
                "client": item.client,
                "server": item.server,
                "state": item.state,
                "reason": item.reason,
                "deadline": item.deadline,
            }

        self.events.snapshot.emit(
            {
                "pending": [pack(x) for x in self.queue.pending.values() if x.state == "pending"],
                "history": [pack(x) for x in self.queue.history],
            }
        )

    async def start(self):
        self.queue = ApprovalQueue(self.options.decision_timeout, changed=self.snapshot)
        gateway = Gateway(
            self.queue,
            self.options.control_token,
            self.options.remote_token,
            self.options.upstream,
            self.options.upstream_token,
            tuple(self.options.origin),
            self.options.gate_all,
        )
        for app, host, port in [
            (gateway.control_app(), "127.0.0.1", self.options.control_port),
            (gateway.remote_app(), self.options.host, self.options.port),
        ]:
            runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=2)
            self.runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
        self.events.ready.emit()

    def run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.start())
            self.loop.run_forever()
        except Exception as exc:
            self.events.failed.emit(f"Could not start gateway: {exc}")
        finally:
            self.loop.run_until_complete(self.cleanup())
            self.loop.close()

    def decide(self, key, allow):
        if self.queue and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.queue.decide, key, allow)

    async def cleanup(self):
        if self.queue:
            self.queue.close()
        for runner in reversed(self.runners):
            await runner.cleanup()

    def stop(self):
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=8)


class RequestDetail(QWidget):
    def __init__(self, decide):
        super().__init__()
        self.item = None
        self.decide = decide
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(15)
        self.eyebrow = label("REQUEST INSPECTOR", "eyebrow")
        self.title = label("You're in control", "title")
        self.source = label("Requests stay here until you decide.", "muted")
        self.state = label("No pending requests", "stats")
        self.payload = QTextEdit()
        self.payload.setReadOnly(True)
        self.payload.setPlainText("Start a client to see its exact MCP request here.")
        buttons = QHBoxLayout()
        self.block = QPushButton("Block request")
        self.block.setObjectName("block")
        self.block.setAutoDefault(False)
        self.allow = QPushButton("Allow once")
        self.allow.setObjectName("allow")
        self.allow.setAutoDefault(False)
        self.block.clicked.connect(lambda: self.choose(False))
        self.allow.clicked.connect(lambda: self.choose(True))
        buttons.addWidget(self.block)
        buttons.addWidget(self.allow)
        for widget in [self.eyebrow, self.title, self.source, self.state, self.payload]:
            layout.addWidget(widget)
        layout.addLayout(buttons)
        self.set_item(None)

    def choose(self, allow):
        if self.item:
            self.allow.setEnabled(False)
            self.block.setEnabled(False)
            self.decide(self.item["key"], allow)

    def set_item(self, item):
        self.item = item
        active = item is not None and item["state"] == "pending"
        self.block.setEnabled(active)
        self.allow.setEnabled(active)
        if item:
            self.title.setText(item["title"])
            self.source.setText(f"{item['client']}\nDestination: {item['server']}")
            self.payload.setPlainText(json.dumps(item["message"], indent=2, ensure_ascii=False))
            self.tick()

    def tick(self):
        if not self.item:
            return
        if self.item["state"] == "pending":
            left = max(0, int(self.item["deadline"] - time.monotonic()))
            self.state.setText(f"HELD  /  Not forwarded  /  {left}s remaining")
        else:
            self.state.setText(f"{self.item['state'].upper()}  /  {self.item['reason']}")


class ApprovalPopup(QDialog):
    def __init__(self, decide, parent):
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Sentinel - approval required")
        self.resize(640, 650)
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.addWidget(label("A client wants to run a request", "brand"))
        layout.addWidget(
            label("Review the arguments, then allow this request once or block it.", "muted")
        )
        self.detail = RequestDetail(decide)
        layout.addWidget(self.detail)

    def reject(self):
        # Dismissing the popup never grants approval. The dashboard keeps the queue.
        self.hide()


class Window(QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.items = {}
        self.known = set()
        self.setWindowTitle("Sentinel | MCP approval gateway")
        self.resize(1140, 770)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(30, 27, 30, 24)
        outer.setSpacing(18)
        heading = QHBoxLayout()
        heading.addWidget(label("Sentinel", "brand"))
        heading.addStretch()
        heading.addWidget(label("MCP REQUEST GATEWAY", "eyebrow"))
        outer.addLayout(heading)
        outer.addWidget(label("Review execution requests before they reach your server.", "muted"))
        self.connection = label("Starting local gateway...", "stats")
        outer.addWidget(self.connection)
        split = QSplitter()
        sidebar = QWidget()
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(0, 0, 0, 0)
        self.counts = label("PENDING  0", "eyebrow")
        left.addWidget(self.counts)
        self.requests = QListWidget()
        left.addWidget(self.requests)
        left.addWidget(label("RECENT DECISIONS  /  This session only", "eyebrow"))
        self.history = QListWidget()
        left.addWidget(self.history)
        split.addWidget(sidebar)
        self.detail = RequestDetail(service.decide)
        split.addWidget(self.detail)
        split.setSizes([340, 740])
        outer.addWidget(split, 1)
        outer.addWidget(
            label(
                (
                    "All requests need approval. Protocol notifications pass through. "
                    if service.options.gate_all
                    else "Discovery and protocol notifications pass through. Decisions apply once. "
                )
                + "Closing Sentinel blocks requests still waiting for approval.",
                "muted",
            )
        )
        self.popup = ApprovalPopup(service.decide, self)
        self.requests.currentItemChanged.connect(self.selected)
        self.history.currentItemChanged.connect(self.selected)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)

    def tick(self):
        self.detail.tick()
        self.popup.detail.tick()

    def selected(self, current, previous):
        if current:
            self.detail.set_item(self.items.get(current.data(Qt.ItemDataRole.UserRole)))

    def update_snapshot(self, snapshot):
        selected = self.detail.item["key"] if self.detail.item else None
        pending = snapshot["pending"]
        self.items = {x["key"]: x for x in [*pending, *snapshot["history"]]}
        self.counts.setText(f"PENDING  {len(pending)}   /   Independent decisions")
        self.requests.clear()
        self.history.clear()
        for collection, widget in [(pending, self.requests), (snapshot["history"], self.history)]:
            for item in collection:
                row = QListWidgetItem(
                    f"{item['title']}  /  #{item['message']['id']}\n"
                    f"{item['client']}  |  {item['state']}"
                )
                row.setData(Qt.ItemDataRole.UserRole, item["key"])
                widget.addItem(row)
        if selected in self.items:
            self.detail.set_item(self.items[selected])
        elif pending:
            self.detail.set_item(pending[0])
        elif snapshot["history"]:
            self.detail.set_item(snapshot["history"][0])
        new = [x for x in pending if x["key"] not in self.known]
        self.known = {x["key"] for x in pending}
        popup_key = self.popup.detail.item["key"] if self.popup.detail.item else None
        if pending and (
            (new and not self.popup.isVisible())
            or (self.popup.isVisible() and popup_key not in self.known)
        ):
            # Never swap the request underneath a person who is reviewing it.
            current = next((x for x in pending if x["key"] == popup_key), pending[0])
            self.popup.detail.set_item(current)
            self.popup.show()
            self.popup.raise_()
            self.popup.activateWindow()
        elif not pending:
            self.popup.hide()

    def closeEvent(self, event):
        self.popup.hide()
        self.service.stop()
        event.accept()


def run_gui(options):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    events = Events()
    service = Service(options, events)
    window = Window(service)
    events.snapshot.connect(window.update_snapshot)
    events.ready.connect(
        lambda: window.connection.setText(
            f"LISTENING  /  Desktop :{options.control_port}   /   "
            f"HTTP :{options.port}/mcp   /   Fail closed"
        )
    )
    events.failed.connect(lambda message: window.connection.setText(message))
    window.show()
    service.thread.start()
    app.exec()
