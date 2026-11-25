"""Tests for the application entrypoint in ``qubo_validator.qubo_validator``."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from qubo_validator.gui import GUI
from qubo_validator.qubo_validator import create_application, main


def test_create_application_returns_app_and_gui() -> None:
    """``create_application`` initializes Qt and constructs a ``GUI`` window."""
    app, window = create_application(["qubo-validator"])
    try:
        assert isinstance(app, QApplication)
        assert isinstance(window, GUI)
        assert window.windowTitle() == "QUBO Validator"
    finally:
        window.close()


def test_main_shows_gui_and_runs_event_loop() -> None:
    """``main`` shows the GUI and returns the Qt event loop exit code."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(["qubo-validator"])

    QTimer.singleShot(50, app.quit)
    exit_code = main(["qubo-validator"])
    assert exit_code == 0
