"""PySide6 graphical interface for QUBO validation and analysis."""

from __future__ import annotations

from typing import Any

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qubo_validator.analyzer import (
    OPTIMIZER_NAMES,
    AnalysisResult,
    analyze_qubo,
    create_connectivity_figure,
    create_heatmap_figure,
    default_zip_filename,
    export_project_zip,
)


class LoadingSpinner(QWidget):
    """Lightweight painter-based rotating arc spinner requiring no image assets."""

    def __init__(self, parent: QWidget | None = None, size: int = 56) -> None:
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        """Begin spinner rotation."""
        self._timer.start(40)

    def stop(self) -> None:
        """Halt spinner rotation."""
        self._timer.stop()

    def _advance(self) -> None:
        self._angle = (self._angle + 18) % 360
        self.update()

    def paintEvent(self, event: Any) -> None:
        """Draw a smooth circular arc indicator."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen_bg = QPen(QColor("#cbd5e1"), 5)
        painter.setPen(pen_bg)
        rect = self.rect().adjusted(6, 6, -6, -6)
        painter.drawEllipse(rect)

        pen_fg = QPen(QColor("#2563eb"), 5)
        pen_fg.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_fg)
        painter.drawArc(rect, -self._angle * 16, 100 * 16)


class _AnalysisWorker(QObject):
    """Background worker running D-Wave evaluation off the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        qubo_text: str,
        penalties_text: str,
        lagrange_multiplier: float,
        qubo_type: str,
        optimizer_name: str,
        optimizer_params: dict[str, Any],
    ) -> None:
        super().__init__()
        self._args = (
            qubo_text,
            penalties_text,
            lagrange_multiplier,
            qubo_type,
            optimizer_name,
            optimizer_params,
        )

    @Slot()
    def run(self) -> None:
        """Execute QUBO analysis and emit either result or error message."""
        try:
            result = analyze_qubo(*self._args)
        except Exception as exc:  # noqa: BLE001 - surface any user/solver error cleanly
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class GUI(QMainWindow):
    """Main window implementing the QUBO Validator interface."""

    PAGE_WAITING = 0
    PAGE_LOADING = 1
    PAGE_RESULTS = 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("QUBO Validator")
        self.resize(1220, 800)

        self._latest_result: AnalysisResult | None = None
        self._worker_thread: QThread | None = None
        self._worker: _AnalysisWorker | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        """Construct the two-column layout: dynamic left view, static right inputs."""
        central = QWidget(self)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(14)

        self.left_stack = self._build_left_section()
        self.right_panel = self._build_right_section()

        root_layout.addWidget(self.left_stack, stretch=3)
        root_layout.addWidget(self.right_panel, stretch=1)
        self.setCentralWidget(central)

    def _build_left_section(self) -> QStackedWidget:
        """Create the dynamic left container: waiting -> spinner -> scrollable stats."""
        stack = QStackedWidget(self)

        # Page 0: Initial waiting state.
        waiting_page = QWidget(stack)
        waiting_layout = QVBoxLayout(waiting_page)
        self.waiting_label = QLabel("waiting for input.", waiting_page)
        self.waiting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.waiting_label.setStyleSheet("color: #64748b; font-size: 16px;")
        waiting_layout.addWidget(self.waiting_label)
        stack.addWidget(waiting_page)

        # Page 1: Loading spinner state.
        loading_page = QWidget(stack)
        loading_layout = QVBoxLayout(loading_page)
        loading_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = LoadingSpinner(loading_page)
        loading_text = QLabel("Evaluating QUBO formulation...", loading_page)
        loading_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(self.spinner, alignment=Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(loading_text, alignment=Qt.AlignmentFlag.AlignCenter)
        stack.addWidget(loading_page)

        # Page 2: Vertically scrollable results container.
        self.results_scroll = QScrollArea(stack)
        self.results_scroll.setWidgetResizable(True)
        results_container = QWidget()
        results_layout = QVBoxLayout(results_container)
        results_layout.setSpacing(12)

        # 1. Amount of qubits.
        qubit_group = QGroupBox("Amount of Qubits", results_container)
        qubit_layout = QVBoxLayout(qubit_group)
        self.qubit_summary_label = QLabel("", qubit_group)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self.qubit_summary_label.setFont(mono)
        self.qubit_summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        qubit_layout.addWidget(self.qubit_summary_label)
        results_layout.addWidget(qubit_group)

        # 2. Penalty validation.
        penalty_group = QGroupBox("Penalty Validation", results_container)
        penalty_layout = QVBoxLayout(penalty_group)
        self.penalty_status_label = QLabel("", penalty_group)
        self.penalty_status_label.setStyleSheet("font-weight: bold;")
        self.penalty_details_label = QLabel("", penalty_group)
        self.penalty_details_label.setWordWrap(True)
        penalty_layout.addWidget(self.penalty_status_label)
        penalty_layout.addWidget(self.penalty_details_label)
        results_layout.addWidget(penalty_group)

        # 3. Energy landscape heatmap.
        heatmap_group = QGroupBox("Energy Landscape", results_container)
        heatmap_layout = QVBoxLayout(heatmap_group)
        self.heatmap_figure = Figure(figsize=(6.5, 4.5), dpi=100)
        self.heatmap_canvas = FigureCanvasQTAgg(self.heatmap_figure)
        self.heatmap_canvas.setMinimumHeight(360)
        heatmap_layout.addWidget(self.heatmap_canvas)
        results_layout.addWidget(heatmap_group)

        # 4. Lowest energy table.
        table_group = QGroupBox("Lowest Energy (Ascending)", results_container)
        table_layout = QVBoxLayout(table_group)
        self.energy_table = QTableWidget(0, 3, table_group)
        self.energy_table.setHorizontalHeaderLabels(["Bit-String", "Energy Value", "Occurrences"])
        self.energy_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.energy_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.energy_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.energy_table.setMinimumHeight(220)
        self.energy_table.setMaximumHeight(320)
        table_layout.addWidget(self.energy_table)
        results_layout.addWidget(table_group)

        # 5. Pure QUBO connectivity graph.
        graph_group = QGroupBox("Connectivity Graph (Pure QUBO)", results_container)
        graph_layout = QVBoxLayout(graph_group)
        self.graph_figure = Figure(figsize=(6.0, 4.2), dpi=100)
        self.graph_canvas = FigureCanvasQTAgg(self.graph_figure)
        self.graph_canvas.setMinimumHeight(340)
        graph_layout.addWidget(self.graph_canvas)
        results_layout.addWidget(graph_group)

        results_layout.addStretch(1)
        self.results_scroll.setWidget(results_container)
        stack.addWidget(self.results_scroll)

        stack.setCurrentIndex(self.PAGE_WAITING)
        return stack

    def _build_right_section(self) -> QWidget:
        """Create the static, non-scrollable right panel for inputs and actions."""
        panel = QWidget(self)
        panel.setMinimumWidth(340)
        panel.setMaximumWidth(420)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)

        # Dynamic download button at the top of the panel, hidden until evaluation completes.
        self.download_button = QPushButton("Download Project (.zip)", panel)
        self.download_button.setVisible(False)
        self.download_button.clicked.connect(self.download_project)
        layout.addWidget(self.download_button)

        # Formula inputs.
        layout.addWidget(QLabel("QUBO Formula:", panel))
        self.qubo_input = QPlainTextEdit(panel)
        self.qubo_input.setPlaceholderText("e.g. 2*x0 + 3*x1 - 4*x0*x1")
        self.qubo_input.setFixedHeight(90)
        layout.addWidget(self.qubo_input)

        layout.addWidget(QLabel("Penalties:", panel))
        self.penalties_input = QPlainTextEdit(panel)
        self.penalties_input.setPlaceholderText("e.g. x0 + x1 <= 1  or  (x0 + x1 - 1)**2")
        self.penalties_input.setFixedHeight(85)
        layout.addWidget(self.penalties_input)

        # Lagrangian multiplier input.
        lagrange_row = QHBoxLayout()
        lagrange_row.addWidget(QLabel("Lagrangian Multiplier (λ):", panel))
        self.lagrange_input = QDoubleSpinBox(panel)
        self.lagrange_input.setRange(-1e9, 1e9)
        self.lagrange_input.setDecimals(4)
        self.lagrange_input.setValue(1.0)
        self.lagrange_input.setToolTip("Scalar multiplier applied to the Penalties expression.")
        lagrange_row.addWidget(self.lagrange_input)
        layout.addLayout(lagrange_row)

        # QUBO Type radio buttons.
        type_group = QGroupBox("QUBO Type", panel)
        type_layout = QHBoxLayout(type_group)
        self.binary_radio = QRadioButton("binary", type_group)
        self.ising_radio = QRadioButton("ising", type_group)
        self.binary_radio.setChecked(True)
        self.qubo_type_group = QButtonGroup(type_group)
        self.qubo_type_group.addButton(self.binary_radio)
        self.qubo_type_group.addButton(self.ising_radio)
        type_layout.addWidget(self.binary_radio)
        type_layout.addWidget(self.ising_radio)
        layout.addWidget(type_group)

        # Optimizer selector.
        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("Optimizer:", panel))
        self.optimizer_combo = QComboBox(panel)
        self.optimizer_combo.addItems(list(OPTIMIZER_NAMES))
        opt_row.addWidget(self.optimizer_combo)
        layout.addLayout(opt_row)

        # Conditional optimizer parameters.
        params_group = QGroupBox("Conditional Parameters", panel)
        params_layout = QVBoxLayout(params_group)
        self.params_stack = self._build_optimizer_params_stack(params_group)
        params_layout.addWidget(self.params_stack)
        layout.addWidget(params_group)
        self.optimizer_combo.currentIndexChanged.connect(self.params_stack.setCurrentIndex)

        # Inline validation error display.
        self.error_label = QLabel("", panel)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #dc2626; font-weight: bold;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        layout.addStretch(1)

        # Submit button.
        self.submit_button = QPushButton("Submit", panel)
        self.submit_button.clicked.connect(self.submit)
        layout.addWidget(self.submit_button)

        return panel

    def _build_optimizer_params_stack(self, parent: QWidget) -> QStackedWidget:
        """Create conditional parameter widgets synchronized with optimizer_combo."""
        stack = QStackedWidget(parent)

        # 0: ExactSolver
        exact_page = QWidget(stack)
        exact_layout = QVBoxLayout(exact_page)
        exact_layout.addWidget(QLabel("No additional parameters required.", exact_page))
        stack.addWidget(exact_page)

        # 1: SimulatedAnnealingSampler
        sa_page = QWidget(stack)
        sa_form = QFormLayout(sa_page)
        self.sa_num_reads = QSpinBox(sa_page)
        self.sa_num_reads.setRange(1, 100_000)
        self.sa_num_reads.setValue(100)
        self.sa_num_sweeps = QSpinBox(sa_page)
        self.sa_num_sweeps.setRange(1, 1_000_000)
        self.sa_num_sweeps.setValue(1000)
        self.sa_beta_schedule = QComboBox(sa_page)
        self.sa_beta_schedule.addItems(["geometric", "linear"])
        self.sa_seed = QLineEdit(sa_page)
        self.sa_seed.setPlaceholderText("Optional integer seed")
        sa_form.addRow("num_reads:", self.sa_num_reads)
        sa_form.addRow("num_sweeps:", self.sa_num_sweeps)
        sa_form.addRow("beta_schedule_type:", self.sa_beta_schedule)
        sa_form.addRow("seed:", self.sa_seed)
        stack.addWidget(sa_page)

        # 2: RandomSampler
        rand_page = QWidget(stack)
        rand_form = QFormLayout(rand_page)
        self.rand_num_reads = QSpinBox(rand_page)
        self.rand_num_reads.setRange(1, 100_000)
        self.rand_num_reads.setValue(100)
        self.rand_max_samples = QSpinBox(rand_page)
        self.rand_max_samples.setRange(1, 100_000)
        self.rand_max_samples.setValue(1000)
        self.rand_seed = QLineEdit(rand_page)
        self.rand_seed.setPlaceholderText("Optional integer seed")
        rand_form.addRow("num_reads:", self.rand_num_reads)
        rand_form.addRow("max_num_samples:", self.rand_max_samples)
        rand_form.addRow("seed:", self.rand_seed)
        stack.addWidget(rand_page)

        return stack

    def _parse_optional_seed(self, field: QLineEdit) -> int | None:
        """Parse an optional integer seed field or raise ValueError if malformed."""
        raw = field.text().strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"Seed must be an integer, got {raw!r}.") from exc

    def get_parameters(self) -> dict[str, Any]:
        """Collect all current UI inputs into a structured parameter dictionary."""
        optimizer = self.optimizer_combo.currentText()
        opt_params: dict[str, Any] = {}
        if optimizer == "SimulatedAnnealingSampler":
            opt_params = {
                "num_reads": self.sa_num_reads.value(),
                "num_sweeps": self.sa_num_sweeps.value(),
                "beta_schedule_type": self.sa_beta_schedule.currentText(),
                "seed": self._parse_optional_seed(self.sa_seed),
            }
        elif optimizer == "RandomSampler":
            opt_params = {
                "num_reads": self.rand_num_reads.value(),
                "max_num_samples": self.rand_max_samples.value(),
                "seed": self._parse_optional_seed(self.rand_seed),
            }

        return {
            "qubo_text": self.qubo_input.toPlainText(),
            "penalties_text": self.penalties_input.toPlainText(),
            "lagrange_multiplier": self.lagrange_input.value(),
            "qubo_type": "binary" if self.binary_radio.isChecked() else "ising",
            "optimizer_name": optimizer,
            "optimizer_params": opt_params,
        }

    @Slot()
    def submit(self) -> None:
        """Validate UI inputs and launch asynchronous QUBO evaluation."""
        if self._worker_thread is not None and self._worker_thread.isRunning():
            return

        self.error_label.setVisible(False)
        try:
            params = self.get_parameters()
        except ValueError as exc:
            self._show_error(str(exc))
            return

        self.download_button.setVisible(False)
        self.submit_button.setEnabled(False)
        self.left_stack.setCurrentIndex(self.PAGE_LOADING)
        self.spinner.start()

        thread = QThread(self)
        worker = _AnalysisWorker(**params)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_analysis_finished)
        worker.failed.connect(self._on_analysis_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        # Do NOT call deleteLater on worker or thread: Python owns them via
        # self._worker / self._worker_thread.  Letting Qt destroy the C++
        # side while Shiboken wrappers are still alive causes a segfault.
        # _clear_thread_refs drops the Python references, allowing normal GC.
        thread.finished.connect(self._clear_thread_refs)

        self._worker_thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def _clear_thread_refs(self) -> None:
        """Drop references once QThread has stopped."""
        self._worker = None
        self._worker_thread = None

    @Slot(object)
    def _on_analysis_finished(self, result: AnalysisResult) -> None:
        """Populate left results panel and reveal the dynamic download button."""
        self.spinner.stop()
        self.submit_button.setEnabled(True)
        self._latest_result = result
        self._render_results(result)
        self.download_button.setVisible(True)
        self.left_stack.setCurrentIndex(self.PAGE_RESULTS)

    @Slot(str)
    def _on_analysis_failed(self, error_message: str) -> None:
        """Restore waiting view and display the error message."""
        self.spinner.stop()
        self.submit_button.setEnabled(True)
        self.left_stack.setCurrentIndex(self.PAGE_WAITING)
        self._show_error(error_message)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def _render_results(self, result: AnalysisResult) -> None:
        """Update all widgets in the scrollable statistics container."""
        self.qubit_summary_label.setText(result.qubo_stats.format_summary())

        pv = result.penalty_validation
        self.penalty_status_label.setText(
            f"Status: {pv.status} | Pure QUBO Range: [{pv.pure_min:.4g}, {pv.pure_max:.4g}] "
            f"(Spread: {pv.pure_range:.4g})"
        )
        if pv.is_sufficient is True:
            self.penalty_status_label.setStyleSheet("color: #065f46; font-weight: bold;")
        elif pv.is_sufficient is False:
            self.penalty_status_label.setStyleSheet("color: #b91c1c; font-weight: bold;")
        else:
            self.penalty_status_label.setStyleSheet("color: #334155; font-weight: bold;")
        self.penalty_details_label.setText(pv.message)

        create_heatmap_figure(result, figure=self.heatmap_figure)
        self.heatmap_canvas.draw_idle()

        self._populate_energy_table(result)

        create_connectivity_figure(result.pure_qubo_bqm, figure=self.graph_figure)
        self.graph_canvas.draw_idle()

    def _populate_energy_table(self, result: AnalysisResult) -> None:
        """Fill the energy table sorted ascending, highlighting lowest-energy rows."""
        df = result.results_df
        var_header = ", ".join(result.ordered_vars) or "const"
        self.energy_table.setHorizontalHeaderLabels(
            [f"Bit-String ({var_header})", "Energy Value", "Occurrences"]
        )
        self.energy_table.setRowCount(len(df))

        low_bg = QColor("#d1fae5")
        low_fg = QColor("#065f46")

        for row_idx, row in df.iterrows():
            items = [
                QTableWidgetItem(str(row["bit_string"])),
                QTableWidgetItem(f"{row['energy']:.6g}"),
                QTableWidgetItem(str(int(row["num_occurrences"]))),
            ]
            is_low = bool(row["is_lowest"])
            for col_idx, item in enumerate(items):
                if is_low:
                    item.setBackground(low_bg)
                    item.setForeground(low_fg)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.energy_table.setItem(int(row_idx), col_idx, item)

    @Slot()
    def download_project(self) -> None:
        """Open a save dialog and write the experiment ZIP archive."""
        if self._latest_result is None:
            return
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save QUBO Project",
            default_zip_filename(),
            "ZIP Archives (*.zip)",
        )
        if not selected_path:
            return
        try:
            export_project_zip(
                self._latest_result,
                selected_path,
                heatmap_fig=self.heatmap_figure,
                graph_fig=self.graph_figure,
            )
        except OSError as exc:
            self._show_error(f"Failed to save ZIP archive: {exc}")

    def closeEvent(self, event: Any) -> None:
        """Ensure background QThread terminates cleanly before window destruction."""
        self.spinner.stop()
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        super().closeEvent(event)
