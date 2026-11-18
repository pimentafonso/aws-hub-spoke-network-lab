"""Connectivity matrix: load and validate the expected results, compare observations.

The matrix (tests/matrix.yaml) is the lab's test contract. This module checks
that the file is consistent and compares observed results with it. Running the
probes on the test instances through SSM Run Command is added in M2.

Usage:
    python scripts/matrix.py validate [path]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "tests" / "matrix.yaml"
FAMILIES = {"ipv4", "ipv6"}
EXPECTS = {"allow", "deny"}
LAYERS = {"security-group", "routing"}
CHECK_KEYS = {"id", "from", "to", "port", "family", "expect", "layer", "stage", "reason"}


class MatrixError(ValueError):
    """The matrix file is inconsistent. The message lists every problem."""


@dataclass(frozen=True)
class Check:
    id: str
    source: str
    destination: str
    port: int
    family: str
    expect: str
    stage: str
    reason: str
    layer: str | None = None


@dataclass(frozen=True)
class Matrix:
    stages: list[str]
    checks: list[Check]

    def for_stage(self, stage: str) -> list[Check]:
        """Rows to run once `stage` is deployed. Stages are cumulative."""
        if stage not in self.stages:
            raise MatrixError(f"unknown stage {stage!r}; expected one of {self.stages}")
        upto = self.stages[: self.stages.index(stage) + 1]
        return [c for c in self.checks if c.stage in upto]


def load(path: Path = DEFAULT_PATH) -> Matrix:
    with path.open(encoding="utf-8") as f:
        return parse(yaml.safe_load(f))


def parse(doc: Any) -> Matrix:
    """Build a Matrix from the parsed YAML, collecting every problem before failing."""
    if not isinstance(doc, dict):
        raise MatrixError("the matrix must be a mapping")
    errors: list[str] = []
    if doc.get("version") != 1:
        errors.append("version must be 1")

    stages = doc.get("stages") or []
    if not stages or len(set(stages)) != len(stages):
        errors.append("stages must be a non-empty list without duplicates")
    order = {s: i for i, s in enumerate(stages)}

    endpoints: dict[str, dict[str, Any]] = {}
    for kind in ("hosts", "targets"):
        for name, spec in (doc.get(kind) or {}).items():
            if name in endpoints:
                errors.append(f"{name}: defined as both a host and a target")
            spec = spec or {}
            families = set(spec.get("families") or [])
            if not families or not families <= FAMILIES:
                errors.append(f"{name}: families must be a subset of {sorted(FAMILIES)}")
            if spec.get("stage") not in order:
                errors.append(f"{name}: unknown stage {spec.get('stage')!r}")
            endpoints[name] = {**spec, "kind": kind, "families": families}

    checks: list[Check] = []
    seen: set[str] = set()
    for i, row in enumerate(doc.get("checks") or []):
        cid = row.get("id") or f"row {i + 1}"
        problems = _check_row(row, endpoints, order)
        if cid in seen:
            problems.append("duplicate id")
        seen.add(cid)
        errors.extend(f"{cid}: {p}" for p in problems)
        if not problems:
            checks.append(
                Check(
                    id=row["id"],
                    source=row["from"],
                    destination=row["to"],
                    port=row["port"],
                    family=row["family"],
                    expect=row["expect"],
                    stage=row["stage"],
                    reason=row["reason"],
                    layer=row.get("layer"),
                )
            )
    if not checks and not errors:
        errors.append("the matrix has no checks")
    if errors:
        raise MatrixError("\n".join(errors))
    return Matrix(stages=list(stages), checks=checks)


def _check_row(row: dict, endpoints: dict, order: dict) -> list[str]:
    problems = [f"unknown key {k!r}" for k in sorted(set(row) - CHECK_KEYS)]
    for key in ("id", "from", "to", "port", "family", "expect", "stage", "reason"):
        if row.get(key) in (None, ""):
            problems.append(f"missing {key}")
    if problems:
        return problems

    src, dst = endpoints.get(row["from"]), endpoints.get(row["to"])
    stage, family, port = row["stage"], row["family"], row["port"]
    if src is None or src["kind"] != "hosts":
        problems.append(f"from must be a host, got {row['from']!r}")
    if dst is None:
        problems.append(f"unknown destination {row['to']!r}")
    if row["from"] == row["to"]:
        problems.append("from and to are the same")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append(f"port must be 1-65535, got {port!r}")
    if family not in FAMILIES:
        problems.append(f"family must be one of {sorted(FAMILIES)}")
    if stage not in order:
        problems.append(f"unknown stage {stage!r}")

    if row["expect"] == "deny":
        if row.get("layer") not in LAYERS:
            problems.append(f"a deny needs layer, one of {sorted(LAYERS)}")
    elif row["expect"] == "allow":
        if "layer" in row:
            problems.append("layer only applies to deny rows")
    else:
        problems.append(f"expect must be one of {sorted(EXPECTS)}")

    for name, end in (("from", src), ("to", dst)):
        if end is None or family not in FAMILIES or stage not in order:
            continue
        if family not in end["families"]:
            problems.append(f"{row[name]} has no {family} address")
        if end["stage"] in order and order[end["stage"]] > order[stage]:
            problems.append(f"{row[name]} is only deployed from stage {end['stage']}")
    return problems


@dataclass(frozen=True)
class Outcome:
    check: Check
    observed: str | None  # "allow", "deny", or None when the probe did not run

    @property
    def passed(self) -> bool:
        return self.observed == self.check.expect


def compare(checks: list[Check], observed: dict[str, str]) -> list[Outcome]:
    """Pair each expected row with its observed result. A missing result fails."""
    return [Outcome(c, observed.get(c.id)) for c in checks]


def report(outcomes: list[Outcome]) -> str:
    """Markdown table of the outcomes, failures first, then a one-line summary."""
    rows = sorted(outcomes, key=lambda o: o.passed)
    lines = [
        "| Result | Check | From | To | Port | Family | Expected | Observed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for o in rows:
        c = o.check
        expected = c.expect if c.layer is None else f"{c.expect} ({c.layer})"
        lines.append(
            f"| {'pass' if o.passed else '**FAIL**'} | {c.id} | {c.source} | {c.destination} "
            f"| {c.port} | {c.family} | {expected} | {o.observed or 'no result'} |"
        )
    failed = sum(not o.passed for o in outcomes)
    lines += ["", f"{len(outcomes) - failed} of {len(outcomes)} checks passed."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="check the matrix file for consistency")
    validate.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    try:
        matrix = load(args.path)
    except MatrixError as e:
        print(f"{args.path}: invalid matrix\n{e}", file=sys.stderr)
        return 1
    counts = ", ".join(f"{s}: {len(matrix.for_stage(s))}" for s in matrix.stages)
    print(f"{args.path.name}: {len(matrix.checks)} checks valid (cumulative per stage: {counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
