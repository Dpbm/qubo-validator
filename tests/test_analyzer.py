"""Unit tests for QUBO parsing, evaluation, validation, graphing, and ZIP export."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

from qubo_validator.analyzer import (
    analyze_qubo,
    build_connectivity_graph,
    create_connectivity_figure,
    create_heatmap_figure,
    default_zip_filename,
    export_project_zip,
)


def test_qubit_counting_with_explicit_and_inequality_slacks() -> None:
    """Explicit s* vars, inequality slacks, and extra penalty vars count cleanly."""
    res = analyze_qubo(
        qubo_text="2*x0 - 3*x1 + x0*x1 + s0",
        penalties_text="x0 + x1 <= 1\nx0 + x2 == 1",
        lagrange_multiplier=10.0,
        qubo_type="binary",
        optimizer_name="ExactSolver",
    )

    stats = res.qubo_stats
    assert stats.individual_variables == 2  # x0, x1
    assert stats.for_penalties == 1  # x2 introduced only in penalties
    assert stats.total_slack >= 2  # s0 + generated slack from x0 + x1 <= 1
    assert stats.total == stats.total_slack + stats.individual_variables + stats.for_penalties
    assert stats.total == len(res.combined_bqm.variables)

    summary = stats.format_summary()
    assert "----------Amount of qubits----------" in summary
    assert f"total: {stats.total}" in summary
    assert f"total slack: {stats.total_slack}" in summary
    assert f"individual variables: {stats.individual_variables}" in summary
    assert f"for penalties: {stats.for_penalties}" in summary


def test_energy_landscape_matrix_shape_and_ascending_ties() -> None:
    """Heatmap matrix uses floor(n/2) row bits and ceil(n/2) col bits; ties stay sorted."""
    # 3 variables: (x0 + x1 + x2 - 1)^2 has three tied ground states (100, 010, 001).
    res = analyze_qubo(
        qubo_text="(x0 + x1 + x2 - 1)^2",
        penalties_text="",
        qubo_type="binary",
        optimizer_name="ExactSolver",
    )

    assert res.row_bits == 1
    assert res.col_bits == 2
    assert res.energy_matrix is not None
    assert res.energy_matrix.shape == (2, 4)

    energies = res.results_df["energy"].to_numpy()
    assert np.all(energies[:-1] <= energies[1:])
    assert int(res.results_df["is_lowest"].sum()) == 3
    assert np.isclose(energies[0], 0.0)


@pytest.mark.parametrize(
    ("optimizer", "params"),
    [
        ("ExactSolver", {}),
        ("SimulatedAnnealingSampler", {"num_reads": 25, "num_sweeps": 100, "seed": 7}),
        ("RandomSampler", {"num_reads": 25, "max_num_samples": 25, "seed": 7}),
    ],
)
def test_all_optimizers_and_ising_mode(optimizer: str, params: dict) -> None:
    """All supported optimizers work on both binary and ising formulations."""
    res = analyze_qubo(
        qubo_text="-x0*x1 + 0.5*x0",
        penalties_text="x0 + x1 >= 0",
        lagrange_multiplier=5.0,
        qubo_type="ising",
        optimizer_name=optimizer,
        optimizer_params=params,
    )

    assert res.vartype == "ising"
    assert not res.results_df.empty
    energies = res.results_df["energy"].to_numpy()
    assert np.all(energies[:-1] <= energies[1:])
    assert bool(res.results_df["is_lowest"].iloc[0])


def test_connectivity_graph_excludes_penalties() -> None:
    """Pure QUBO graph must contain only pure QUBO variables and interactions."""
    res = analyze_qubo(
        qubo_text="x0 + x1 - 2*x0*x1",
        penalties_text="x0 + x2 == 1",
        lagrange_multiplier=4.0,
        qubo_type="binary",
        optimizer_name="ExactSolver",
    )

    graph = build_connectivity_graph(res.pure_qubo_bqm)
    assert len(res.pure_qubo_bqm.variables) == 2
    assert "x2" not in res.pure_qubo_bqm.variables
    assert graph.num_nodes() == 2
    assert graph.num_edges() == 1


def test_penalty_validation_sufficient_insufficient_and_unsat() -> None:
    """Penalty validation distinguishes sufficient, insufficient, and unsat cases."""
    # Pure QUBO: -3*x0 - 3*x1 has range [-6, 0] (spread 6). Penalty: x0 + x1 <= 1.
    insufficient = analyze_qubo(
        qubo_text="-3*x0 - 3*x1",
        penalties_text="x0 + x1 <= 1",
        lagrange_multiplier=1.0,
        optimizer_name="ExactSolver",
    )
    assert insufficient.penalty_validation.pure_min == pytest.approx(-6.0)
    assert insufficient.penalty_validation.pure_max == pytest.approx(0.0)
    assert insufficient.penalty_validation.pure_range == pytest.approx(6.0)
    assert insufficient.penalty_validation.is_sufficient is False

    sufficient = analyze_qubo(
        qubo_text="-3*x0 - 3*x1",
        penalties_text="x0 + x1 <= 1",
        lagrange_multiplier=8.0,
        optimizer_name="ExactSolver",
    )
    assert sufficient.penalty_validation.is_sufficient is True
    assert sufficient.penalty_validation.satisfiable is True

    unsat = analyze_qubo(
        qubo_text="x0 + x1",
        penalties_text="x0 + x1 == 1\nx0 + x1 == 2",
        lagrange_multiplier=5.0,
        optimizer_name="ExactSolver",
    )
    assert unsat.penalty_validation.satisfiable is False
    assert unsat.penalty_validation.is_sufficient is False
    assert unsat.penalty_validation.status == "Unsatisfiable"


def test_zip_export_contents(tmp_path) -> None:
    """Exported ZIP holds pandas CSV, PNG plots, and JSON referencing filenames."""
    res = analyze_qubo(
        qubo_text="2*x0 + 3*x1 - 4*x0*x1",
        penalties_text="(x0 + x1 - 1)**2",
        lagrange_multiplier=5.0,
        optimizer_name="ExactSolver",
    )
    hm_fig = create_heatmap_figure(res)
    gr_fig = create_connectivity_figure(res.pure_qubo_bqm)

    zip_name = default_zip_filename(date(2026, 11, 25))
    assert zip_name == "project_QUBO-2026-11-25.zip"
    out_zip = export_project_zip(res, tmp_path / zip_name, hm_fig, gr_fig)

    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert {"energies.csv", "energy_landscape.png", "connectivity_graph.png", "metadata.json"} <= names

        meta = json.loads(zf.read("metadata.json").decode("utf-8"))
        assert meta["files"]["csv"] == "energies.csv"
        assert meta["files"]["plots"] == ["energy_landscape.png", "connectivity_graph.png"]
        assert meta["parameters"]["qubo_type"] == "binary"

        csv_df = pd.read_csv(io.BytesIO(zf.read("energies.csv")))
        assert list(csv_df.columns) == ["bit_string", "energy", "num_occurrences", "is_lowest"]
        assert len(csv_df) == len(res.results_df)

        assert zf.read("energy_landscape.png").startswith(b"\x89PNG")
        assert zf.read("connectivity_graph.png").startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("qubo", "penalties", "optimizer"),
    [
        ("", "", "ExactSolver"),
        ("x0 * x1 * x2", "", "ExactSolver"),
        ("x0 / 0", "", "ExactSolver"),
        ("2*x0 +", "", "ExactSolver"),
        ("x0 + x1", "", "UnknownSolver"),
    ],
)
def test_invalid_inputs_raise_value_error(qubo: str, penalties: str, optimizer: str) -> None:
    """Malformed expressions and invalid optimizers raise clear ValueErrors."""
    with pytest.raises(ValueError):
        analyze_qubo(qubo_text=qubo, penalties_text=penalties, optimizer_name=optimizer)
