"""Application entrypoint for launching the QUBO Validator GUI."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from qubo_validator.gui import GUI


def create_application(argv: Sequence[str] | None = None) -> tuple[QApplication, GUI]:
    """Initialize (or reuse) the Qt application and construct the main GUI window."""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(list(argv) if argv is not None else sys.argv)
    window = GUI()
    return app, window


def main(argv: Sequence[str] | None = None) -> int:
    """Create the GUI, show it, and run the Qt application event loop."""
    app, window = create_application(argv)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
