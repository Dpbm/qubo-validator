"""GUI integration tests for ``qubo_validator.gui.GUI`` using offscreen Qt."""

from __future__ import annotations

import os
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog

from qubo_validator import GUI


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    """Provide a single QApplication instance across GUI tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _wait_until(app: QApplication, predicate, timeout_sec: float = 10.0) -> None:
    """Pump Qt events until ``predicate()`` returns True or timeout expires."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("Timed out waiting for GUI state transition.")


def test_initial_ui_state_and_conditional_parameters(qapp: QApplication) -> None:
    """Initial view shows 'waiting for input.', hidden download, and dynamic params."""
    gui = GUI()
    gui.show()
    try:
        assert gui.left_stack.currentIndex() == GUI.PAGE_WAITING
        assert gui.waiting_label.text() == "waiting for input."
        assert gui.download_button.isVisible() is False
        assert gui.binary_radio.isChecked() is True
        assert gui.optimizer_combo.currentText() == "ExactSolver"
        assert gui.params_stack.currentIndex() == 0

        gui.optimizer_combo.setCurrentText("SimulatedAnnealingSampler")
        assert gui.params_stack.currentIndex() == 1

        gui.optimizer_combo.setCurrentText("RandomSampler")
        assert gui.params_stack.currentIndex() == 2
    finally:
        gui.close()


def test_submit_updates_results_table_and_reveals_download(
    qapp: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting a QUBO transitions to results, highlights min energy, and exports ZIP."""
    gui = GUI()
    gui.show()
    try:
        gui.qubo_input.setPlainText("2*x0 + 2*x1 - 4*x0*x1")
        gui.penalties_input.setPlainText("x0 + x1 >= 1")
        gui.lagrange_input.setValue(5.0)
        gui.binary_radio.setChecked(True)
        gui.optimizer_combo.setCurrentText("ExactSolver")

        gui.submit()
        assert gui.left_stack.currentIndex() == GUI.PAGE_LOADING
        assert gui.submit_button.isEnabled() is False

        _wait_until(qapp, lambda: gui.left_stack.currentIndex() == GUI.PAGE_RESULTS)

        assert gui.submit_button.isEnabled() is True
        assert gui.download_button.isVisible() is True
        assert "----------Amount of qubits----------" in gui.qubit_summary_label.text()
        assert "individual variables: 2" in gui.qubit_summary_label.text()
        assert gui.energy_table.rowCount() > 0

        # Verify ascending energy sort in table and green highlight on row 0.
        energies = [
            float(gui.energy_table.item(r, 1).text())
            for r in range(gui.energy_table.rowCount())
        ]
        assert energies == sorted(energies)
        assert gui.energy_table.item(0, 0).background().color().name() == "#d1fae5"

        target_zip = tmp_path / "exported.zip"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            lambda *args, **kwargs: (str(target_zip), "ZIP Archives (*.zip)"),
        )
        gui.download_project()
        assert target_zip.exists()
        with zipfile.ZipFile(target_zip, "r") as zf:
            assert "metadata.json" in zf.namelist()
            assert "energies.csv" in zf.namelist()
    finally:
        gui.close()


def test_invalid_submission_shows_error_without_download(qapp: QApplication) -> None:
    """Invalid input restores waiting page and surfaces an inline error message."""
    gui = GUI()
    gui.show()
    try:
        gui.qubo_input.setPlainText("")
        gui.submit()
        _wait_until(qapp, lambda: gui.submit_button.isEnabled())

        assert gui.left_stack.currentIndex() == GUI.PAGE_WAITING
        assert gui.download_button.isVisible() is False
        assert gui.error_label.isVisible() is True
        assert "empty" in gui.error_label.text().lower()
    finally:
        gui.close()


def test_ising_simulated_annealing_flow(qapp: QApplication) -> None:
    """Ising selection and SimulatedAnnealingSampler parameters execute end-to-end."""
    gui = GUI()
    gui.show()
    try:
        gui.qubo_input.setPlainText("-x0*x1 + x0")
        gui.ising_radio.setChecked(True)
        gui.optimizer_combo.setCurrentText("SimulatedAnnealingSampler")
        gui.sa_num_reads.setValue(20)
        gui.sa_num_sweeps.setValue(50)
        gui.sa_seed.setText("123")

        gui.submit()
        _wait_until(qapp, lambda: gui.left_stack.currentIndex() == GUI.PAGE_RESULTS)

        assert gui.download_button.isVisible() is True
        assert "No penalties provided" in gui.penalty_details_label.text()
    finally:
        gui.close()
