"""QUBO parsing, evaluation, validation, plotting, and export utilities.

Provides the pure-Python and D-Wave Ocean SDK backend consumed by
``qubo_validator.gui.GUI`` without any direct Qt dependency.
"""

from __future__ import annotations

import ast
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import dimod
import numpy as np
import pandas as pd
import z3
from dimod.vartypes import Vartype
from dwave.samplers import RandomSampler, SimulatedAnnealingSampler
from matplotlib.figure import Figure

try:
    import rustworkx as rx
    from rustworkx.visualization import mpl_draw as rx_mpl_draw
except ImportError:  # pragma: no cover - fallback when rustworkx is absent
    rx = None
    rx_mpl_draw = None

import networkx as nx

# Exhaustive enumeration above 14 qubits (16,384 states) slows down heatmaps.
MAX_EXACT_QUBITS = 14
MAX_EXACT_SOLVER_QUBITS = 18

OPTIMIZER_NAMES = (
    "ExactSolver",
    "SimulatedAnnealingSampler",
    "RandomSampler",
)


@dataclass(frozen=True)
class QubitStats:
    """Breakdown of qubits required by the QUBO and penalty formulation."""

    total: int
    total_slack: int
    individual_variables: int
    for_penalties: int
    qubo_vars: tuple[str, ...] = ()
    slack_vars: tuple[str, ...] = ()
    penalty_extra_vars: tuple[str, ...] = ()
    all_vars: tuple[str, ...] = ()

    def format_summary(self) -> str:
        """Return the qubit summary matching the specification template."""
        lines = [
            "----------Amount of qubits----------",
            f"total: {self.total}",
            f"total slack: {self.total_slack}",
            f"individual variables: {self.individual_variables}",
            f"for penalties: {self.for_penalties}",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class PenaltyValidation:
    """Result of validating the Lagrangian multiplier against pure QUBO bounds."""

    pure_min: float
    pure_max: float
    pure_range: float
    lagrange_multiplier: float
    min_required_lagrange: float | None
    is_sufficient: bool | None
    satisfiable: bool | None
    has_penalties: bool
    status: str
    message: str


@dataclass
class AnalysisResult:
    """Complete evaluation output passed from worker thread to the GUI."""

    qubo_stats: QubitStats
    pure_qubo_bqm: dimod.BinaryQuadraticModel
    combined_bqm: dimod.BinaryQuadraticModel
    penalty_validation: PenaltyValidation
    energy_matrix: np.ndarray | None
    row_bits: int
    col_bits: int
    ordered_vars: tuple[str, ...]
    results_df: pd.DataFrame
    parameters: dict[str, Any] = field(default_factory=dict)
    vartype: str = "binary"


def _is_slack_name(name: str) -> bool:
    """Return True if a variable name represents an explicit or generated slack."""
    return bool(re.fullmatch(r"s(_?\d+)?", name)) or name.startswith("slack_")


def _var_sort_key(name: str) -> tuple[int, str, int, str]:
    """Sort decision variables naturally before slack variables."""
    match = re.fullmatch(r"([a-zA-Z_]+?)_?(\d+)", name)
    prefix = match.group(1) if match else name
    index = int(match.group(2)) if match else -1
    return (1 if _is_slack_name(name) else 0, prefix, index, name)


def _normalize_statement(stmt: str) -> str:
    """Normalize math shorthand (`^` for power, single `=` for equality)."""
    normalized = stmt.strip().replace("^", "**")
    return re.sub(r"(?<![<>=!])=(?!=)", "==", normalized)


def _split_statements(text: str) -> list[str]:
    """Split multiline or semicolon-delimited formulas into non-empty strings."""
    if not text or not text.strip():
        return []
    parts = re.split(r"[\n;]+", text)
    return [_normalize_statement(p) for p in parts if p.strip()]


def _as_scalar(value: Any) -> float:
    """Extract a numeric scalar from a number or constant-only BQM."""
    if isinstance(value, (int, float, np.number)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dimod.BinaryQuadraticModel) and value.num_variables == 0:
        return float(value.offset)
    raise ValueError("Expected a numeric scalar value.")


def _eval_expr_ast(
    node: ast.AST,
    vartype: Vartype,
    seen_vars: set[str],
) -> dimod.BinaryQuadraticModel | float:
    """Safely evaluate an arithmetic AST node into a BQM or scalar."""
    if isinstance(node, ast.Expression):
        return _eval_expr_ast(node.body, vartype, seen_vars)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError(f"Unsupported constant: {node.value!r}")

    if isinstance(node, ast.Name):
        seen_vars.add(node.id)
        return dimod.Binary(node.id) if vartype is Vartype.BINARY else dimod.Spin(node.id)

    if isinstance(node, ast.UnaryOp):
        operand = _eval_expr_ast(node.operand, vartype, seen_vars)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        left = _eval_expr_ast(node.left, vartype, seen_vars)
        right = _eval_expr_ast(node.right, vartype, seen_vars)

        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            try:
                return left * right
            except TypeError as exc:
                raise ValueError(
                    "Expression contains terms above degree 2 (not a valid QUBO)."
                ) from exc
        if isinstance(node.op, ast.Div):
            divisor = _as_scalar(right)
            if divisor == 0:
                raise ValueError("Division by zero in formula.")
            return left / divisor
        if isinstance(node.op, ast.Pow):
            exponent = _as_scalar(right)
            if isinstance(left, (int, float)):
                return float(left**exponent)
            if int(exponent) != exponent or exponent < 0:
                raise ValueError("Variable expressions only support non-negative integer powers.")
            exp_int = int(exponent)
            if exp_int == 0:
                return 1.0
            if exp_int == 1:
                return left
            if exp_int == 2:
                try:
                    return left**2
                except ValueError as exc:
                    raise ValueError(
                        "Only linear subexpressions can be squared in a QUBO."
                    ) from exc
            raise ValueError("QUBO expressions cannot exceed degree 2.")

        raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")

    raise ValueError(f"Unsupported syntax element: {type(node).__name__}")


def _to_bqm(value: dimod.BinaryQuadraticModel | float, vartype: Vartype) -> dimod.BinaryQuadraticModel:
    """Coerce a scalar or BQM to a BQM of the requested vartype."""
    if isinstance(value, dimod.BinaryQuadraticModel):
        return value.change_vartype(vartype, inplace=False)
    return dimod.BinaryQuadraticModel({}, {}, float(value), vartype)


def _scale_to_integers(terms: list[tuple[str, float]], constant: float, bound: float) -> tuple[list[tuple[str, int]], int, int]:
    """Scale a linear inequality by the LCM of denominators for exact slack encoding."""
    values = [bias for _, bias in terms] + [constant, bound]
    fracs = [Fraction(v).limit_denominator(10_000) for v in values]
    lcm = 1
    for frac in fracs:
        lcm = math.lcm(lcm, frac.denominator)
    int_terms = [(var, round(bias * lcm)) for var, bias in terms]
    return int_terms, round(constant * lcm), round(bound * lcm)


def _apply_linear_constraint(
    bqm: dimod.BinaryQuadraticModel,
    lhs_expr: dimod.BinaryQuadraticModel,
    op: ast.cmpop,
    rhs_expr: dimod.BinaryQuadraticModel,
    label: str,
) -> list[str]:
    """Convert a linear ==, <=, or >= constraint into a binary penalty BQM."""
    diff = lhs_expr - rhs_expr
    if not diff.is_linear():
        raise ValueError("Constraints must be linear in the variables.")

    # Convert spin variables to binary {0, 1} so dimod's slack encoding stays exact.
    if diff.vartype is Vartype.SPIN:
        diff = diff.change_vartype(Vartype.BINARY, inplace=False)

    terms = [(str(v), float(bias)) for v, bias in diff.linear.items() if bias != 0]
    constant = float(diff.offset)

    if isinstance(op, ast.Eq):
        bqm.add_linear_equality_constraint(terms, 1.0, constant)
        return []

    if isinstance(op, (ast.LtE, ast.GtE)):
        sign = -1.0 if isinstance(op, ast.GtE) else 1.0
        signed_terms = [(v, sign * bias) for v, bias in terms]
        int_terms, int_const, int_ub = _scale_to_integers(signed_terms, sign * constant, 0.0)
        slack_terms = bqm.add_linear_inequality_constraint(
            int_terms,
            1.0,
            label=label,
            constant=int_const,
            ub=int_ub,
        )
        return [str(v) for v, _ in slack_terms]

    raise ValueError(f"Unsupported comparison operator: {type(op).__name__}. Use ==, <=, or >=.")


def _eval_z3_ast(node: ast.AST, z3_vars: dict[str, Any]) -> Any:
    """Translate an expression AST into a Z3 arithmetic expression."""
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in z3_vars:
            z3_vars[node.id] = z3.Int(node.id)
        return z3_vars[node.id]
    if isinstance(node, ast.UnaryOp):
        val = _eval_z3_ast(node.operand, z3_vars)
        return val if isinstance(node.op, ast.UAdd) else -val
    if isinstance(node, ast.BinOp):
        left = _eval_z3_ast(node.left, z3_vars)
        right = _eval_z3_ast(node.right, z3_vars)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            return left**int(right)
    raise ValueError(f"Unsupported Z3 AST element: {type(node).__name__}")


def _check_constraints_satisfiable(
    constraint_asts: list[ast.Compare],
    vartype: Vartype,
) -> bool | None:
    """Use Z3 to check whether parsed comparison constraints admit a solution."""
    if not constraint_asts:
        return None

    solver = z3.Solver()
    z3_vars: dict[str, Any] = {}

    for cmp_node in constraint_asts:
        nodes = [cmp_node.left, *cmp_node.comparators]
        for op, lhs_node, rhs_node in zip(cmp_node.ops, nodes[:-1], nodes[1:], strict=True):
            lhs = _eval_z3_ast(lhs_node, z3_vars)
            rhs = _eval_z3_ast(rhs_node, z3_vars)
            if isinstance(op, ast.Eq):
                solver.add(lhs == rhs)
            elif isinstance(op, ast.LtE):
                solver.add(lhs <= rhs)
            elif isinstance(op, ast.GtE):
                solver.add(lhs >= rhs)

    for var in z3_vars.values():
        if vartype is Vartype.BINARY:
            solver.add(z3.Or(var == 0, var == 1))
        else:
            solver.add(z3.Or(var == -1, var == 1))

    return solver.check() == z3.sat


def parse_formula_block(
    text: str,
    vartype: Vartype,
    label_prefix: str = "c",
) -> tuple[dimod.BinaryQuadraticModel, set[str], list[str], list[ast.Compare]]:
    """Parse expressions and/or constraints into a unit-weighted BQM.

    Returns ``(bqm, referenced_vars, generated_slacks, constraint_asts)``.
    Constraints are compiled in BINARY space and converted to ``vartype`` at the end.
    """
    statements = _split_statements(text)
    acc_bqm = dimod.BinaryQuadraticModel(vartype)
    seen_vars: set[str] = set()
    generated_slacks: list[str] = []
    constraint_asts: list[ast.Compare] = []

    for idx, stmt in enumerate(statements):
        try:
            tree = ast.parse(stmt, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"Invalid formula syntax in '{stmt}': {exc.msg}") from exc

        body = tree.body
        if isinstance(body, ast.Compare):
            constraint_asts.append(body)
            nodes = [body.left, *body.comparators]
            constraint_bqm = dimod.BinaryQuadraticModel(Vartype.BINARY)
            for sub_idx, (op, lhs_node, rhs_node) in enumerate(
                zip(body.ops, nodes[:-1], nodes[1:], strict=True)
            ):
                lhs_bqm = _to_bqm(_eval_expr_ast(lhs_node, vartype, seen_vars), vartype)
                rhs_bqm = _to_bqm(_eval_expr_ast(rhs_node, vartype, seen_vars), vartype)
                slacks = _apply_linear_constraint(
                    constraint_bqm,
                    lhs_bqm,
                    op,
                    rhs_bqm,
                    label=f"{label_prefix}_{idx}_{sub_idx}",
                )
                generated_slacks.extend(slacks)
            if vartype is Vartype.SPIN:
                constraint_bqm = constraint_bqm.change_vartype(Vartype.SPIN, inplace=False)
            acc_bqm += constraint_bqm
        else:
            expr_bqm = _to_bqm(_eval_expr_ast(body, vartype, seen_vars), vartype)
            acc_bqm += expr_bqm

    return acc_bqm, seen_vars, generated_slacks, constraint_asts


def _all_bitstrings_energies(
    bqm: dimod.BinaryQuadraticModel,
    variables: list[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(samples_matrix, energies_1d)`` for all 2^n assignments."""
    n = len(variables)
    if n == 0:
        return np.empty((1, 0), dtype=np.int8), np.array([float(bqm.offset)], dtype=np.float64)

    indices = np.arange(1 << n, dtype=np.uint32)[:, None]
    shifts = np.arange(n - 1, -1, -1, dtype=np.uint32)
    bits = ((indices >> shifts) & 1).astype(np.int8)
    samples = 2 * bits - 1 if bqm.vartype is Vartype.SPIN else bits
    energies = bqm.energies((samples, variables))
    return samples, np.asarray(energies, dtype=np.float64)


def _validate_penalties(
    pure_qubo_bqm: dimod.BinaryQuadraticModel,
    unit_penalty_bqm: dimod.BinaryQuadraticModel,
    combined_bqm: dimod.BinaryQuadraticModel,
    lagrange_multiplier: float,
    has_penalties: bool,
    constraint_asts: list[ast.Compare],
    vartype: Vartype,
) -> PenaltyValidation:
    """Validate whether the Lagrangian multiplier suffices over the pure QUBO range."""
    pure_vars = sorted(pure_qubo_bqm.variables, key=_var_sort_key)
    all_vars = sorted(combined_bqm.variables, key=_var_sort_key)

    if len(pure_vars) <= MAX_EXACT_QUBITS:
        _, pure_energies = _all_bitstrings_energies(pure_qubo_bqm, pure_vars)
        pure_min = float(np.min(pure_energies))
        pure_max = float(np.max(pure_energies))
    else:
        radius = sum(abs(b) for b in pure_qubo_bqm.linear.values()) + sum(
            abs(b) for b in pure_qubo_bqm.quadratic.values()
        )
        pure_min = float(pure_qubo_bqm.offset - radius)
        pure_max = float(pure_qubo_bqm.offset + radius)

    pure_range = pure_max - pure_min
    satisfiable = _check_constraints_satisfiable(constraint_asts, vartype)

    if not has_penalties:
        return PenaltyValidation(
            pure_min=pure_min,
            pure_max=pure_max,
            pure_range=pure_range,
            lagrange_multiplier=lagrange_multiplier,
            min_required_lagrange=None,
            is_sufficient=None,
            satisfiable=None,
            has_penalties=False,
            status="N/A",
            message=(
                f"No penalties provided. Pure QUBO energy range: "
                f"[{pure_min:.4g}, {pure_max:.4g}] (spread = {pure_range:.4g})."
            ),
        )

    if satisfiable is False:
        return PenaltyValidation(
            pure_min=pure_min,
            pure_max=pure_max,
            pure_range=pure_range,
            lagrange_multiplier=lagrange_multiplier,
            min_required_lagrange=None,
            is_sufficient=False,
            satisfiable=False,
            has_penalties=True,
            status="Unsatisfiable",
            message=(
                "Penalty constraints are unsatisfiable (verified by Z3); "
                "no assignment satisfies all constraints."
            ),
        )

    if len(all_vars) <= MAX_EXACT_QUBITS:
        _, unit_p = _all_bitstrings_energies(unit_penalty_bqm, all_vars)
        _, total_e = _all_bitstrings_energies(combined_bqm, all_vars)
        min_p = float(np.min(unit_p))
        feasible_mask = np.isclose(unit_p, min_p)
        violated_mask = ~feasible_mask

        if not np.any(violated_mask):
            return PenaltyValidation(
                pure_min=pure_min,
                pure_max=pure_max,
                pure_range=pure_range,
                lagrange_multiplier=lagrange_multiplier,
                min_required_lagrange=0.0,
                is_sufficient=True,
                satisfiable=satisfiable,
                has_penalties=True,
                status="Sufficient",
                message="Penalty is constant across all states; any multiplier is valid.",
            )

        min_violation = float(np.min(unit_p[violated_mask] - min_p))
        min_req = pure_range / min_violation if min_violation > 0 else float("inf")
        best_feasible = float(np.min(total_e[feasible_mask]))
        best_violated = float(np.min(total_e[violated_mask]))
        empirically_valid = lagrange_multiplier > 0 and best_violated > best_feasible
        theoretically_sufficient = lagrange_multiplier > min_req

        if theoretically_sufficient:
            status = "Sufficient"
            msg = (
                f"Lagrangian λ={lagrange_multiplier:.4g} > required {min_req:.4g}: "
                f"guaranteed to penalize all infeasible states over pure QUBO spread "
                f"({pure_range:.4g})."
            )
        elif empirically_valid:
            status = "Sufficient (Instance)"
            msg = (
                f"Lagrangian λ={lagrange_multiplier:.4g} ≤ worst-case bound {min_req:.4g}, "
                f"but suffices on this landscape (min infeasible {best_violated:.4g} > "
                f"min feasible {best_feasible:.4g})."
            )
        else:
            status = "Insufficient"
            msg = (
                f"Lagrangian λ={lagrange_multiplier:.4g} is insufficient "
                f"(recommended > {min_req:.4g}; min infeasible {best_violated:.4g} ≤ "
                f"min feasible {best_feasible:.4g})."
            )

        return PenaltyValidation(
            pure_min=pure_min,
            pure_max=pure_max,
            pure_range=pure_range,
            lagrange_multiplier=lagrange_multiplier,
            min_required_lagrange=min_req,
            is_sufficient=theoretically_sufficient or empirically_valid,
            satisfiable=satisfiable,
            has_penalties=True,
            status=status,
            message=msg,
        )

    # Analytical fallback above MAX_EXACT_QUBITS assuming unit minimum constraint gap.
    min_req = pure_range
    sufficient = lagrange_multiplier > min_req
    return PenaltyValidation(
        pure_min=pure_min,
        pure_max=pure_max,
        pure_range=pure_range,
        lagrange_multiplier=lagrange_multiplier,
        min_required_lagrange=min_req,
        is_sufficient=sufficient,
        satisfiable=satisfiable,
        has_penalties=True,
        status="Sufficient" if sufficient else "Insufficient",
        message=(
            f"Analytical pure QUBO spread={pure_range:.4g}; "
            f"λ={lagrange_multiplier:.4g} {'exceeds' if sufficient else 'does not exceed'} "
            f"recommended bound {min_req:.4g}."
        ),
    )


def _run_optimizer(
    bqm: dimod.BinaryQuadraticModel,
    optimizer_name: str,
    optimizer_params: dict[str, Any],
) -> dimod.SampleSet:
    """Execute the requested D-Wave Ocean SDK sampler on ``bqm``."""
    if len(bqm.variables) == 0:
        return dimod.SampleSet.from_samples([], bqm.vartype, energy=[bqm.offset])

    if optimizer_name == "ExactSolver":
        if len(bqm.variables) > MAX_EXACT_SOLVER_QUBITS:
            raise ValueError(
                f"ExactSolver supports at most {MAX_EXACT_SOLVER_QUBITS} variables "
                f"(got {len(bqm.variables)}). Select SimulatedAnnealingSampler or RandomSampler."
            )
        return dimod.ExactSolver().sample(bqm)

    if optimizer_name == "SimulatedAnnealingSampler":
        kwargs: dict[str, Any] = {
            "num_reads": int(optimizer_params.get("num_reads", 100)),
            "num_sweeps": int(optimizer_params.get("num_sweeps", 1000)),
            "beta_schedule_type": str(optimizer_params.get("beta_schedule_type", "geometric")),
        }
        if optimizer_params.get("seed") is not None:
            kwargs["seed"] = int(optimizer_params["seed"])
        return SimulatedAnnealingSampler().sample(bqm, **kwargs)

    if optimizer_name == "RandomSampler":
        kwargs = {
            "num_reads": int(optimizer_params.get("num_reads", 100)),
            "max_num_samples": int(optimizer_params.get("max_num_samples", 1000)),
        }
        if optimizer_params.get("seed") is not None:
            kwargs["seed"] = int(optimizer_params["seed"])
        return RandomSampler().sample(bqm, **kwargs)

    raise ValueError(f"Unsupported optimizer: {optimizer_name}")


def _sampleset_to_dataframe(
    sampleset: dimod.SampleSet,
    ordered_vars: list[str],
    vartype: Vartype,
) -> pd.DataFrame:
    """Aggregate and sort samples ascending by energy into a DataFrame."""
    if len(ordered_vars) == 0:
        return pd.DataFrame(
            [{"bit_string": "∅", "energy": float(sampleset.first.energy) if len(sampleset) else 0.0, "num_occurrences": 1, "is_lowest": True}]
        )

    agg = sampleset.aggregate()
    rows: list[dict[str, Any]] = []
    for datum in agg.data(fields=["sample", "energy", "num_occurrences"]):
        if vartype is Vartype.BINARY:
            bit_str = "".join(str(int(datum.sample[v])) for v in ordered_vars)
        else:
            bit_str = " ".join(
                f"{int(datum.sample[v]):+d}" for v in ordered_vars
            )
        rows.append(
            {
                "bit_string": bit_str,
                "energy": float(datum.energy),
                "num_occurrences": int(datum.num_occurrences),
            }
        )

    df = pd.DataFrame(rows)
    df = df.sort_values(by=["energy", "bit_string"], ascending=[True, True], kind="mergesort")
    df = df.reset_index(drop=True)
    min_energy = float(df["energy"].iloc[0])
    df["is_lowest"] = np.isclose(df["energy"].to_numpy(dtype=float), min_energy)
    return df


def analyze_qubo(
    qubo_text: str,
    penalties_text: str = "",
    lagrange_multiplier: float = 1.0,
    qubo_type: str = "binary",
    optimizer_name: str = "ExactSolver",
    optimizer_params: dict[str, Any] | None = None,
) -> AnalysisResult:
    """Parse inputs, run D-Wave evaluation, and compute all GUI statistics."""
    if not qubo_text or not qubo_text.strip():
        raise ValueError("QUBO formula cannot be empty.")

    qubo_type_norm = qubo_type.strip().lower()
    if qubo_type_norm not in {"binary", "ising"}:
        raise ValueError(f"QUBO type must be 'binary' or 'ising', got {qubo_type!r}.")
    vartype = Vartype.BINARY if qubo_type_norm == "binary" else Vartype.SPIN

    optimizer_params = dict(optimizer_params or {})

    pure_qubo_bqm, qubo_seen, qubo_gen_slacks, qubo_constraints = parse_formula_block(
        qubo_text, vartype, label_prefix="q"
    )
    unit_penalty_bqm, pen_seen, pen_gen_slacks, pen_constraints = parse_formula_block(
        penalties_text, vartype, label_prefix="p"
    )

    has_penalties = bool(penalties_text and penalties_text.strip())
    scaled_penalty_bqm = (
        unit_penalty_bqm * float(lagrange_multiplier)
        if has_penalties
        else dimod.BinaryQuadraticModel(vartype)
    )
    combined_bqm = pure_qubo_bqm + scaled_penalty_bqm

    # Classify variables into individual QUBO vars, slack vars, and extra penalty vars.
    qubo_individual = sorted((v for v in qubo_seen if not _is_slack_name(v)), key=_var_sort_key)
    all_explicit_slacks = {v for v in (qubo_seen | pen_seen) if _is_slack_name(v)}
    all_slacks = sorted(all_explicit_slacks | set(qubo_gen_slacks) | set(pen_gen_slacks), key=_var_sort_key)
    penalty_extra = sorted(
        (v for v in pen_seen if not _is_slack_name(v) and v not in qubo_seen),
        key=_var_sort_key,
    )
    ordered_vars = sorted(combined_bqm.variables, key=_var_sort_key)

    qubo_stats = QubitStats(
        total=len(all_slacks) + len(qubo_individual) + len(penalty_extra),
        total_slack=len(all_slacks),
        individual_variables=len(qubo_individual),
        for_penalties=len(penalty_extra),
        qubo_vars=tuple(qubo_individual),
        slack_vars=tuple(all_slacks),
        penalty_extra_vars=tuple(penalty_extra),
        all_vars=tuple(ordered_vars),
    )

    n_total = len(ordered_vars)
    if n_total <= MAX_EXACT_QUBITS:
        row_bits = n_total // 2
        col_bits = n_total - row_bits
        _, flat_energies = _all_bitstrings_energies(combined_bqm, ordered_vars)
        energy_matrix = flat_energies.reshape((1 << row_bits, 1 << col_bits))
    else:
        row_bits = 0
        col_bits = 0
        energy_matrix = None

    penalty_validation = _validate_penalties(
        pure_qubo_bqm=pure_qubo_bqm,
        unit_penalty_bqm=unit_penalty_bqm,
        combined_bqm=combined_bqm,
        lagrange_multiplier=float(lagrange_multiplier),
        has_penalties=has_penalties,
        constraint_asts=qubo_constraints + pen_constraints,
        vartype=vartype,
    )

    sampleset = _run_optimizer(combined_bqm, optimizer_name, optimizer_params)
    results_df = _sampleset_to_dataframe(sampleset, ordered_vars, vartype)

    parameters = {
        "qubo": qubo_text.strip(),
        "penalties": penalties_text.strip(),
        "lagrange_multiplier": float(lagrange_multiplier),
        "qubo_type": qubo_type_norm,
        "optimizer": optimizer_name,
        "optimizer_params": optimizer_params,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }

    return AnalysisResult(
        qubo_stats=qubo_stats,
        pure_qubo_bqm=pure_qubo_bqm,
        combined_bqm=combined_bqm,
        penalty_validation=penalty_validation,
        energy_matrix=energy_matrix,
        row_bits=row_bits,
        col_bits=col_bits,
        ordered_vars=tuple(ordered_vars),
        results_df=results_df,
        parameters=parameters,
        vartype=qubo_type_norm,
    )


def build_connectivity_graph(pure_qubo_bqm: dimod.BinaryQuadraticModel) -> Any:
    """Build the pure-QUBO connectivity graph using rustworkx (or networkx fallback)."""
    nodes = sorted(pure_qubo_bqm.variables, key=_var_sort_key)

    if rx is not None:
        graph = rx.PyGraph()
        idx_map: dict[str, int] = {}
        for var in nodes:
            bias = float(pure_qubo_bqm.get_linear(var))
            idx_map[var] = graph.add_node({"label": var, "bias": bias})
        for (u, v), weight in pure_qubo_bqm.quadratic.items():
            graph.add_edge(idx_map[u], idx_map[v], float(weight))
        return graph

    nx_graph = nx.Graph()  # pragma: no cover
    for var in nodes:  # pragma: no cover
        nx_graph.add_node(var, bias=float(pure_qubo_bqm.get_linear(var)))
    for (u, v), weight in pure_qubo_bqm.quadratic.items():  # pragma: no cover
        nx_graph.add_edge(u, v, weight=float(weight))
    return nx_graph  # pragma: no cover


def create_heatmap_figure(result: AnalysisResult, figure: Figure | None = None) -> Figure:
    """Render the 2D bit-string energy landscape heatmap with min/max markers."""
    fig = figure if figure is not None else Figure(figsize=(6.5, 4.5), dpi=100)
    fig.clear()
    ax = fig.add_subplot(111)

    matrix = result.energy_matrix
    if matrix is None:
        ax.text(
            0.5,
            0.5,
            f"Energy landscape skipped for {result.qubo_stats.total} qubits\n"
            f"(exhaustive 2^n matrix capped at {MAX_EXACT_QUBITS} qubits).",
            ha="center",
            va="center",
            fontsize=10,
        )
        ax.set_axis_off()
        return fig

    im = ax.imshow(matrix, cmap="viridis", aspect="auto")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("Energy")

    min_val = float(np.min(matrix))
    max_val = float(np.max(matrix))
    min_coords = np.argwhere(np.isclose(matrix, min_val))
    max_coords = np.argwhere(np.isclose(matrix, max_val))

    ax.scatter(
        min_coords[:, 1],
        min_coords[:, 0],
        marker="*",
        s=130,
        color="#22d3ee",
        edgecolors="black",
        linewidths=0.8,
        zorder=5,
        label=f"Lowest (E={min_val:.4g})",
    )
    ax.scatter(
        max_coords[:, 1],
        max_coords[:, 0],
        marker="X",
        s=100,
        color="#ef4444",
        edgecolors="black",
        linewidths=0.8,
        zorder=5,
        label=f"Highest (E={max_val:.4g})",
    )

    # Mark lowest and highest values directly on the colorbar scale.
    cbar.ax.plot(
        [0.5],
        [min_val],
        marker="*",
        markersize=10,
        color="#22d3ee",
        markeredgecolor="black",
        clip_on=False,
    )
    cbar.ax.plot(
        [0.5],
        [max_val],
        marker="X",
        markersize=8,
        color="#ef4444",
        markeredgecolor="black",
        clip_on=False,
    )

    rows, cols = matrix.shape
    row_vars = result.ordered_vars[: result.row_bits]
    col_vars = result.ordered_vars[result.row_bits :]
    ax.set_ylabel(f"Row bits ({', '.join(row_vars) or 'const'})")
    ax.set_xlabel(f"Column bits ({', '.join(col_vars) or 'const'})")
    ax.set_title("Energy Landscape Heatmap")

    if rows <= 16 and result.row_bits > 0:
        ax.set_yticks(range(rows))
        ax.set_yticklabels([format(i, f"0{result.row_bits}b") for i in range(rows)], fontsize=8)
    if cols <= 16 and result.col_bits > 0:
        ax.set_xticks(range(cols))
        ax.set_xticklabels(
            [format(j, f"0{result.col_bits}b") for j in range(cols)],
            fontsize=8,
            rotation=45,
            ha="right",
        )

    ax.legend(loc="upper right", framealpha=0.9, fontsize=8)
    fig.tight_layout()
    return fig


def create_connectivity_figure(
    pure_qubo_bqm: dimod.BinaryQuadraticModel,
    figure: Figure | None = None,
) -> Figure:
    """Render the pure-QUBO connectivity graph using rustworkx (or networkx)."""
    fig = figure if figure is not None else Figure(figsize=(6.0, 4.2), dpi=100)
    fig.clear()
    ax = fig.add_subplot(111)

    if len(pure_qubo_bqm.variables) == 0:
        ax.text(0.5, 0.5, "No variables in pure QUBO.", ha="center", va="center")
        ax.set_axis_off()
        return fig

    graph = build_connectivity_graph(pure_qubo_bqm)
    if rx is not None and rx_mpl_draw is not None and isinstance(graph, rx.PyGraph):
        pos = rx.spring_layout(graph, seed=42)
        rx_mpl_draw(
            graph,
            pos=pos,
            ax=ax,
            with_labels=True,
            labels=lambda data: f"{data['label']}\n({data['bias']:.3g})",
            edge_labels=(lambda w: f"{w:.3g}") if graph.num_edges() > 0 else None,
            node_color="#93c5fd",
            node_size=900,
            font_size=9,
            edge_color="#475569",
        )
    else:  # pragma: no cover - fallback when rustworkx is absent
        pos = nx.spring_layout(graph, seed=42)
        node_labels = {n: f"{n}\n({d['bias']:.3g})" for n, d in graph.nodes(data=True)}
        edge_labels = {(u, v): f"{d['weight']:.3g}" for u, v, d in graph.edges(data=True)}
        nx.draw(
            graph,
            pos=pos,
            ax=ax,
            labels=node_labels,
            with_labels=True,
            node_color="#93c5fd",
            node_size=900,
            font_size=9,
            edge_color="#475569",
        )
        nx.draw_networkx_edge_labels(graph, pos=pos, edge_labels=edge_labels, ax=ax, font_size=8)

    ax.set_title("Pure QUBO Connectivity Graph (No Penalties)")
    ax.set_axis_off()
    fig.tight_layout()
    return fig


def default_zip_filename(on_date: date | None = None) -> str:
    """Return the default ZIP filename in ``project_QUBO-<date>.zip`` format."""
    target_date = on_date or date.today()
    return f"project_QUBO-{target_date.isoformat()}.zip"


def export_project_zip(
    result: AnalysisResult,
    output_path: str | Path,
    heatmap_fig: Figure | None = None,
    graph_fig: Figure | None = None,
) -> Path:
    """Export energies CSV, PNG plots, and metadata JSON into a single ZIP archive."""
    dest = Path(output_path)
    if dest.suffix.lower() != ".zip":
        dest = dest.with_suffix(".zip")

    hm_fig = heatmap_fig if heatmap_fig is not None else create_heatmap_figure(result)
    gr_fig = graph_fig if graph_fig is not None else create_connectivity_figure(result.pure_qubo_bqm)

    csv_name = "energies.csv"
    heatmap_name = "energy_landscape.png"
    graph_name = "connectivity_graph.png"
    json_name = "metadata.json"

    metadata = {
        "parameters": result.parameters,
        "qubit_counts": {
            "total": result.qubo_stats.total,
            "total_slack": result.qubo_stats.total_slack,
            "individual_variables": result.qubo_stats.individual_variables,
            "for_penalties": result.qubo_stats.for_penalties,
            "variables": list(result.qubo_stats.all_vars),
        },
        "penalty_validation": {
            "pure_qubo_min": result.penalty_validation.pure_min,
            "pure_qubo_max": result.penalty_validation.pure_max,
            "pure_qubo_range": result.penalty_validation.pure_range,
            "lagrange_multiplier": result.penalty_validation.lagrange_multiplier,
            "min_required_lagrange": result.penalty_validation.min_required_lagrange,
            "is_sufficient": result.penalty_validation.is_sufficient,
            "satisfiable": result.penalty_validation.satisfiable,
            "status": result.penalty_validation.status,
            "message": result.penalty_validation.message,
        },
        "files": {
            "csv": csv_name,
            "plots": [heatmap_name, graph_name],
        },
    }

    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(csv_name, result.results_df.to_csv(index=False))

        with zf.open(heatmap_name, "w") as hm_buf:
            hm_fig.savefig(hm_buf, format="png", dpi=150, bbox_inches="tight")

        with zf.open(graph_name, "w") as gr_buf:
            gr_fig.savefig(gr_buf, format="png", dpi=150, bbox_inches="tight")

        zf.writestr(json_name, json.dumps(metadata, indent=2))

    return dest
