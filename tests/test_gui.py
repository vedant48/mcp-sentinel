import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mcp_sentinel.gui import Window


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    decisions = []
    service = SimpleNamespace(
        options=SimpleNamespace(gate_all=False),
        decide=lambda key, allow: decisions.append((key, allow)),
        stop=lambda: None,
    )
    widget = Window(service)
    widget.show()
    app.processEvents()
    yield widget, decisions
    widget.close()
    app.processEvents()


def item(key, request_id):
    return {
        "key": key,
        "title": "echo",
        "message": {"id": request_id},
        "client": "Desktop route / stdio",
        "server": "demo_server.py",
        "state": "pending",
        "reason": "Awaiting decision",
        "deadline": time.monotonic() + 60,
    }


def test_buttons_disabled_until_a_request_exists(window):
    widget, _ = window
    assert not widget.detail.allow.isEnabled()
    assert not widget.detail.block.isEnabled()


def test_popup_is_nonmodal_and_requests_topmost(window, app):
    widget, _ = window
    widget.update_snapshot({"pending": [item("first", 1)], "history": []})
    app.processEvents()
    assert widget.popup.isVisible()
    assert not widget.popup.isModal()
    assert widget.popup.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_new_arrival_does_not_replace_request_under_review(window):
    widget, decisions = window
    first, second = item("first", 1), item("second", 2)
    widget.update_snapshot({"pending": [first], "history": []})
    widget.update_snapshot({"pending": [first, second], "history": []})
    assert widget.popup.detail.item["key"] == "first"
    widget.popup.detail.allow.click()
    assert decisions == [("first", True)]
    assert not widget.popup.detail.allow.isEnabled()


def test_dismissing_popup_does_not_approve(window):
    widget, decisions = window
    widget.update_snapshot({"pending": [item("first", 1)], "history": []})
    widget.popup.reject()
    assert not decisions
    assert widget.requests.count() == 1


def test_resolved_popup_advances_to_next_request(window):
    widget, decisions = window
    first, second = item("first", 1), item("second", 2)
    widget.update_snapshot({"pending": [first, second], "history": []})
    widget.update_snapshot({"pending": [second], "history": [{**first, "state": "allowed"}]})
    assert widget.popup.detail.item["key"] == "second"
    widget.popup.detail.block.click()
    assert decisions == [("second", False)]


def test_new_request_reopens_dismissed_popup_without_swapping_its_payload(window):
    widget, _ = window
    first, second = item("first", 1), item("second", 2)
    widget.update_snapshot({"pending": [first], "history": []})
    widget.popup.reject()
    widget.update_snapshot({"pending": [first, second], "history": []})
    assert widget.popup.isVisible()
    assert widget.popup.detail.item["key"] == "first"
