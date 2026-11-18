"""Connectivity matrix: load and validate the expected results, compare observations.

The matrix (tests/matrix.yaml) is the lab's test contract. This module checks
that the file is consistent, runs it against a deployed lab, and compares the
observed results with it.

Each source host gets one SSM Run Command invocation with a small Python
program that attempts every TCP connection in its rows and prints the results
as JSON. A connection that completes is "allow"; a timeout, a refusal or an
unreachable network is "deny", with the reason kept as detail.

Usage:
    python scripts/matrix.py validate [path]
    python scripts/matrix.py run --outputs lab-outputs.json [--save docs/results]

`run` reads `terraform output -json` from the lab root (scripts/lab.sh matrix
writes it) and exits 1 on any difference from the expected results.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    hosts: dict[str, str] = field(default_factory=dict)  # name -> VPC
    targets: dict[str, str] = field(default_factory=dict)  # name -> address

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
    return Matrix(
        stages=list(stages),
        checks=checks,
        hosts={n: e.get("vpc", "") for n, e in endpoints.items() if e["kind"] == "hosts"},
        targets={n: e.get("address", "") for n, e in endpoints.items() if e["kind"] == "targets"},
    )


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
    detail: str = ""  # how it was observed: "connected in 2 ms", "timeout after 5 s"

    @property
    def passed(self) -> bool:
        return self.observed == self.check.expect


def compare(
    checks: list[Check], observed: dict[str, str], details: dict[str, str] | None = None
) -> list[Outcome]:
    """Pair each expected row with its observed result. A missing result fails."""
    details = details or {}
    return [Outcome(c, observed.get(c.id), details.get(c.id, "")) for c in checks]


def report(outcomes: list[Outcome]) -> str:
    """Markdown table of the outcomes, failures first, then a one-line summary."""
    rows = sorted(outcomes, key=lambda o: o.passed)
    lines = [
        "| Result | Check | From | To | Port | Family | Expected | Observed | Detail |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for o in rows:
        c = o.check
        expected = c.expect if c.layer is None else f"{c.expect} ({c.layer})"
        lines.append(
            f"| {'pass' if o.passed else '**FAIL**'} | {c.id} | {c.source} | {c.destination} "
            f"| {c.port} | {c.family} | {expected} | {o.observed or 'no result'} | {o.detail} |"
        )
    failed = sum(not o.passed for o in outcomes)
    lines += ["", f"{len(outcomes) - failed} of {len(outcomes)} checks passed."]
    return "\n".join(lines)


# --- running the matrix against a deployed lab ---------------------------------

PROBE_TIMEOUT_SECONDS = 5

# Sent to each source host and run with its python3 (Amazon Linux 2023).
PROBE_PATH = Path(__file__).resolve().parent / "matrix_probe.py"


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # looked up at call time, so tests can replace it


class RunnerError(RuntimeError):
    """The matrix could not be run: a host is missing, offline, or a command failed."""


@dataclass(frozen=True)
class Probe:
    check_id: str
    address: str
    port: int
    family: str


def read_outputs(path: Path) -> dict[str, Any]:
    """`terraform output -json` as {name: value}."""
    with path.open(encoding="utf-8") as f:
        return {name: o["value"] for name, o in json.load(f).items()}


def resolve(name: str, family: str, matrix: Matrix, outputs: dict[str, Any]) -> str:
    """The address a probe connects to: a host's address, or a target's name."""
    if name in matrix.hosts:
        host = outputs.get("hosts", {}).get(name)
        if not host or not host.get(family):
            raise RunnerError(f"{name} has no {family} address in the lab outputs")
        return host[family]
    address = matrix.targets[name]
    if address.startswith("output:"):
        key = address.removeprefix("output:")
        if not outputs.get(key):
            raise RunnerError(f"{name}: lab output {key!r} is missing")
        return outputs[key]
    return address


def plan_probes(
    checks: list[Check], matrix: Matrix, outputs: dict[str, Any]
) -> dict[str, list[Probe]]:
    """Group the probes by the source host's instance ID."""
    plan: dict[str, list[Probe]] = {}
    for c in checks:
        source = outputs.get("hosts", {}).get(c.source)
        if not source:
            raise RunnerError(f"{c.id}: source host {c.source} is not in the lab outputs")
        address = resolve(c.destination, c.family, matrix, outputs)
        plan.setdefault(source["instance_id"], []).append(Probe(c.id, address, c.port, c.family))
    return plan


def shell_command(probes: list[Probe]) -> str:
    """One shell line: write the probe program and run it with the probe list."""
    program = base64.b64encode(PROBE_PATH.read_bytes()).decode()
    payload = {
        "timeout": PROBE_TIMEOUT_SECONDS,
        "probes": [
            {"id": p.check_id, "address": p.address, "port": p.port, "family": p.family}
            for p in probes
        ],
    }
    probes_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    return (
        f"echo {program} | base64 -d > /tmp/nwlab-probe.py && "
        f"python3 /tmp/nwlab-probe.py {probes_b64}"
    )


def wait_online(
    ssm: Any,
    instance_ids: list[str],
    timeout: float = 600,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until every instance's SSM agent reports Online."""
    deadline = clock() + timeout
    while True:
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": instance_ids}]
        )["InstanceInformationList"]
        online = {i["InstanceId"] for i in info if i.get("PingStatus") == "Online"}
        missing = sorted(set(instance_ids) - online)
        if not missing:
            return
        if clock() > deadline:
            raise RunnerError(f"not online in SSM after {timeout:.0f} s: {', '.join(missing)}")
        sleep(10)


def run_probes(
    ssm: Any,
    plan: dict[str, list[Probe]],
    timeout: float = 300,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, dict[str, str]]:
    """Send one command per host, wait for all of them, and merge the results."""
    commands = {}
    for instance_id, probes in plan.items():
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Comment="nwlab connectivity matrix",
            TimeoutSeconds=60 + PROBE_TIMEOUT_SECONDS * len(probes),
            Parameters={"commands": [shell_command(probes)]},
        )
        commands[instance_id] = response["Command"]["CommandId"]

    results: dict[str, dict[str, str]] = {}
    pending = dict(commands)
    deadline = clock() + timeout
    while pending:
        sleep(3)
        for instance_id, command_id in list(pending.items()):
            try:
                inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            except ssm.exceptions.InvocationDoesNotExist:
                continue  # not registered yet
            status = inv["Status"]
            if status in ("Pending", "InProgress", "Delayed"):
                continue
            del pending[instance_id]
            if status != "Success":
                stderr = inv.get("StandardErrorContent", "").strip()
                raise RunnerError(f"{instance_id}: command {status}: {stderr[:500]}")
            results.update(json.loads(inv["StandardOutputContent"]))
        if pending and clock() > deadline:
            raise RunnerError(f"no result after {timeout:.0f} s from {', '.join(pending)}")
    return results


def run(
    matrix: Matrix,
    outputs: dict[str, Any],
    ssm: Any,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[Outcome]:
    """Run the deployed stage's rows on the lab's hosts and compare."""
    checks = matrix.for_stage(outputs["stage"])
    plan = plan_probes(checks, matrix, outputs)
    wait_online(ssm, sorted(plan), sleep=sleep, clock=clock)
    raw = run_probes(ssm, plan, sleep=sleep, clock=clock)
    observed = {cid: r["result"] for cid, r in raw.items()}
    details = {
        cid: f"{r['detail']} ({r['address']})" if r.get("address") else r["detail"]
        for cid, r in raw.items()
    }
    return compare(checks, observed, details)


def save(outcomes: list[Outcome], outputs: dict[str, Any], directory: Path, now: datetime) -> Path:
    """Write the report as Markdown, named after the stage, the egress and the time."""
    directory.mkdir(parents=True, exist_ok=True)
    egress = outputs.get("egress", "lab")
    path = directory / f"{outputs['stage']}-{egress}-{now.strftime('%Y-%m-%dT%H%MZ')}.md"
    header = (
        f"# Connectivity matrix: {outputs['stage']}, {egress}\n\n"
        f"Run at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC by `scripts/matrix.py run` "
        f"through SSM Run Command, {PROBE_TIMEOUT_SECONDS} s connect timeout.\n\n"
    )
    path.write_text(header + report(outcomes) + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="check the matrix file for consistency")
    validate.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    run_cmd = sub.add_parser("run", help="run the deployed stage's rows through SSM")
    run_cmd.add_argument("--outputs", type=Path, required=True, help="terraform output -json")
    run_cmd.add_argument("--save", type=Path, help="directory for a Markdown copy of the report")
    run_cmd.add_argument("--matrix", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    path = args.path if args.command == "validate" else args.matrix
    try:
        matrix = load(path)
    except MatrixError as e:
        print(f"{path}: invalid matrix\n{e}", file=sys.stderr)
        return 1
    if args.command == "run":
        return _run_cli(matrix, args.outputs, args.save)
    counts = ", ".join(f"{s}: {len(matrix.for_stage(s))}" for s in matrix.stages)
    print(f"{args.path.name}: {len(matrix.checks)} checks valid (cumulative per stage: {counts})")
    return 0


def _run_cli(matrix: Matrix, outputs_path: Path, save_dir: Path | None) -> int:
    import boto3  # only the run command needs AWS

    outputs = read_outputs(outputs_path)
    print(f"Running stage {outputs['stage']} ({outputs.get('egress')}) through SSM...")
    try:
        outcomes = run(matrix, outputs, boto3.client("ssm"))
    except RunnerError as e:
        print(f"matrix run failed: {e}", file=sys.stderr)
        return 1
    print(report(outcomes))
    if save_dir:
        print(f"Saved to {save(outcomes, outputs, save_dir, datetime.now(UTC))}")
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
