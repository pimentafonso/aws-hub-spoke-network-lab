"""Evidence for the matrix: VPC Flow Logs through Athena, and Reachability Analyzer.

The matrix says whether a connection worked. This says which layer decided:

- an allow must show an ACCEPT at the destination's network interface;
- a security-group deny must show a REJECT there, and no ACCEPT;
- a routing deny must leave no record at all in the destination VPC, because
  the packet never got there. That only means something once the destination
  VPC's logs for the probe window have arrived, so each such row also needs a
  later record from that VPC, of any kind.

Rows to a destination outside the lab (the internet) have nothing to check.
The PrivateLink row is checked at the consumer's interface endpoint, where the
connection is accepted; spoke A sees it arrive from the NLB's addresses.

Usage:
    python scripts/evidence.py flowlogs --outputs lab-outputs.json --since EPOCH
        [--until EPOCH] [--wait 1200] [--save docs/results]
    python scripts/evidence.py reachability --outputs lab-outputs.json [--save docs/results]

`flowlogs` polls Athena every minute until every row has its evidence or
`--wait` seconds have passed, and reports how long delivery took. Each query
scans at least 10 MB, about $0.00005. `reachability` runs one Reachability
Analyzer analysis per path in the lab outputs, $0.10 each.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from matrix import Check, Matrix, MatrixError, RunnerError, load, read_outputs, resolve

SLACK = 120  # seconds either side of the probe window that still count
POLL = 60  # seconds between Athena polls
ANALYSIS_PRICE = 0.10  # USD per Reachability Analyzer analysis, eu-west-1


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # looked up at call time, so tests can replace it


def _ip(address: str) -> str:
    """One spelling per address, whatever form the logs or the API use."""
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return address


# --- what each row should leave in the logs ------------------------------------


@dataclass(frozen=True)
class Expectation:
    check: Check
    want: str  # "ACCEPT", "REJECT", "none", or "skip"
    source: str = ""  # source address
    destination: str = ""  # destination address, "" when only the interface matters
    enis: tuple[str, ...] = ()  # where an ACCEPT or REJECT must appear
    vpc: str = ""  # VPC ID where a routing deny must leave nothing
    vpc_name: str = ""
    note: str = ""


def expectations(matrix: Matrix, outputs: dict[str, Any]) -> list[Expectation]:
    """What the flow logs should show for each row of the deployed stage."""
    hosts = outputs["hosts"]
    result = []
    for c in matrix.for_stage(outputs["stage"]):
        source = _ip(hosts[c.source][c.family])
        if c.destination in matrix.hosts:
            dest = hosts[c.destination]
            address = _ip(resolve(c.destination, c.family, matrix, outputs))
            if c.expect == "deny" and c.layer == "routing":
                vpc_name = matrix.hosts[c.destination]
                result.append(
                    Expectation(
                        c,
                        "none",
                        source,
                        address,
                        vpc=outputs["vpcs"][vpc_name]["id"],
                        vpc_name=vpc_name,
                    )
                )
            else:
                want = "ACCEPT" if c.expect == "allow" else "REJECT"
                result.append(Expectation(c, want, source, address, enis=(dest["eni"],)))
        elif c.destination == "privatelink":
            enis = tuple(outputs["privatelink"]["endpoint_enis"])
            result.append(
                Expectation(
                    c,
                    "ACCEPT" if c.expect == "allow" else "REJECT",
                    source,
                    enis=enis,
                    note="at the consumer's interface endpoint",
                )
            )
        else:
            result.append(Expectation(c, "skip", note="destination outside the lab"))
    return result


# --- Athena --------------------------------------------------------------------


def partition_filter(since: float, until: float) -> str:
    """The hourly partitions that can hold records from the window, as SQL."""
    start = datetime.fromtimestamp(since - SLACK, UTC).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(until + SLACK, UTC) + timedelta(hours=1)
    terms = []
    hour = start
    while hour <= end:
        terms.append(
            f"(year = '{hour:%Y}' AND month = '{hour:%m}' AND day = '{hour:%d}' "
            f"AND hour = '{hour:%H}')"
        )
        hour += timedelta(hours=1)
    return "(" + " OR ".join(terms) + ")"


def records_query(table: str, since: float, until: float, ports: set[int]) -> str:
    port_list = ", ".join(str(p) for p in sorted(ports))
    # Every value is a number or a name from the lab's own outputs; nothing
    # comes from outside.
    return (
        'SELECT interface_id, vpc_id, srcaddr, dstaddr, dstport, action, start, "end" '  # noqa: S608
        f"FROM {table} WHERE {partition_filter(since, until)} "
        f"AND dstport IN ({port_list}) "
        f"AND start BETWEEN {int(since) - SLACK} AND {int(until) + SLACK}"
    )


def coverage_query(table: str, since: float, until: float) -> str:
    """The latest record from each VPC: proof its logs for the window arrived."""
    # Numbers and the lab's own table name only.
    return (
        f'SELECT vpc_id, max("end") AS latest FROM {table} '  # noqa: S608
        f"WHERE {partition_filter(since, until)} GROUP BY vpc_id"
    )


def athena(
    client: Any,
    query: str,
    database: str,
    workgroup: str,
    sleep: Callable[[float], None] = _sleep,
) -> list[dict[str, str]]:
    """Run one query and return its rows as dicts of strings."""
    qid = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        WorkGroup=workgroup,
    )["QueryExecutionId"]
    while True:
        status = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if status["State"] == "SUCCEEDED":
            break
        if status["State"] in ("FAILED", "CANCELLED"):
            raise RunnerError(f"Athena query {status['State']}: {status.get('StateChangeReason')}")
        sleep(2)
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for page in client.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for row in page["ResultSet"]["Rows"]:
            values = [d.get("VarCharValue", "") for d in row["Data"]]
            if header is None:
                header = values
            else:
                rows.append(dict(zip(header, values, strict=True)))
    return rows


# --- judging -------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    expectation: Expectation
    passed: bool | None  # None: nothing to check
    observed: str


def judge(
    exps: list[Expectation], records: list[dict[str, str]], latest: dict[str, int], until: float
) -> list[Finding]:
    """Compare the records with what each row should have left."""
    findings = []
    for e in exps:
        if e.want == "skip":
            findings.append(Finding(e, None, e.note))
            continue
        port = str(e.check.port)
        matching = [
            r
            for r in records
            if _ip(r["srcaddr"]) == e.source
            and r["dstport"] == port
            and (not e.destination or _ip(r["dstaddr"]) == e.destination)
        ]
        if e.want == "none":
            here = [r for r in matching if r["vpc_id"] == e.vpc]
            covered = latest.get(e.vpc, 0) >= until
            if here:
                actions = sorted({r["action"] for r in here})
                findings.append(
                    Finding(
                        e, False, f"{len(here)} record(s) in {e.vpc_name}: {', '.join(actions)}"
                    )
                )
            elif not covered:
                findings.append(Finding(e, False, f"{e.vpc_name}'s logs for the window not in yet"))
            else:
                findings.append(Finding(e, True, f"no record in {e.vpc_name}"))
            continue
        at_eni = [r for r in matching if r["interface_id"] in e.enis]
        accepts = sum(r["action"] == "ACCEPT" for r in at_eni)
        rejects = sum(r["action"] == "REJECT" for r in at_eni)
        # A security-group deny must be rejected every time it was tried.
        passed = accepts > 0 if e.want == "ACCEPT" else rejects > 0 and accepts == 0
        observed = f"{accepts} ACCEPT, {rejects} REJECT" if at_eni else "no record yet"
        if e.note:
            observed += f" ({e.note})"
        findings.append(Finding(e, passed, observed))
    return findings


def flowlog_report(findings: list[Finding], delay: float | None) -> str:
    lines = [
        "| Result | Check | Expected | Layer | Flow logs should show | Observed |",
        "|---|---|---|---|---|---|",
    ]
    wants = {
        "ACCEPT": "ACCEPT at the destination",
        "REJECT": "REJECT at the destination",
        "none": "no record in the destination VPC",
        "skip": "nothing to check",
    }
    for f in sorted(findings, key=lambda f: (f.passed is not False, f.passed is None)):
        e = f.expectation
        result = {True: "pass", False: "**FAIL**", None: "n/a"}[f.passed]
        lines.append(
            f"| {result} | {e.check.id} | {e.check.expect} | {e.check.layer or ''} "
            f"| {wants[e.want]} | {f.observed} |"
        )
    checked = [f for f in findings if f.passed is not None]
    passed = sum(bool(f.passed) for f in checked)
    lines += ["", f"{passed} of {len(checked)} rows confirmed by the flow logs."]
    if delay is not None:
        lines.append(
            f"Every row's evidence was in Athena {delay / 60:.1f} min after the probes ended."
        )
    return "\n".join(lines)


def collect(
    client: Any,
    outputs: dict[str, Any],
    exps: list[Expectation],
    since: float,
    until: float,
    wait: float,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.time,
) -> tuple[list[Finding], float | None]:
    """Poll until every row has its evidence or `wait` runs out."""
    logs = outputs["flow_logs"]
    table = f"{logs['database']}.{logs['table']}"
    ports = {e.check.port for e in exps if e.want != "skip"}
    deadline = clock() + wait
    while True:
        records = athena(
            client,
            records_query(table, since, until, ports),
            logs["database"],
            logs["workgroup"],
            sleep,
        )
        latest = {
            r["vpc_id"]: int(r["latest"] or 0)
            for r in athena(
                client,
                coverage_query(table, since, until),
                logs["database"],
                logs["workgroup"],
                sleep,
            )
        }
        findings = judge(exps, records, latest, until)
        if all(f.passed is not False for f in findings):
            return findings, clock() - until
        if clock() > deadline:
            return findings, None
        waiting = sum(f.passed is False for f in findings)
        print(f"  {waiting} row(s) without evidence yet; next poll in {POLL} s", flush=True)
        sleep(POLL)


# --- Reachability Analyzer -----------------------------------------------------

ROUTING_COMPONENTS = {
    "RouteTable",
    "RouteTableRoute",
    "TransitGatewayRouteTable",
    "TransitGatewayRouteTableRoute",
}
SG_COMPONENTS = {"SecurityGroup", "SecurityGroups", "SecurityGroupRule"}


@dataclass(frozen=True)
class Analysis:
    name: str
    check: Check | None
    found: bool
    codes: tuple[str, ...]
    layers: tuple[str, ...]
    components: tuple[str, ...]

    @property
    def passed(self) -> bool | None:
        if self.check is None:
            return None
        if self.check.expect == "allow":
            return self.found
        return not self.found and (self.check.layer in self.layers)


def matching_check(matrix: Matrix, outputs: dict[str, Any], path: dict) -> Check | None:
    """The IPv4 matrix row a path repeats."""
    for c in matrix.for_stage(outputs["stage"]):
        if (c.source, c.destination, c.port, c.family) == (
            path["from"],
            path["to"],
            path["port"],
            "ipv4",
        ):
            return c
    return None


def summarise(name: str, check: Check | None, analysis: dict) -> Analysis:
    """Reduce one finished analysis to its verdict, codes and blamed layers."""
    explanations = analysis.get("Explanations", [])
    layers, components = set(), []
    for x in explanations:
        for key, value in x.items():
            if key in ROUTING_COMPONENTS:
                layers.add("routing")
            elif key in SG_COMPONENTS:
                layers.add("security-group")
            else:
                continue
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict) and item.get("Id"):
                    components.append(f"{key} {item['Id']}")
    return Analysis(
        name,
        check,
        bool(analysis.get("NetworkPathFound")),
        tuple(sorted({x["ExplanationCode"] for x in explanations if x.get("ExplanationCode")})),
        tuple(sorted(layers)),
        tuple(dict.fromkeys(components)),
    )


def analyse_paths(
    ec2: Any,
    matrix: Matrix,
    outputs: dict[str, Any],
    sleep: Callable[[float], None] = _sleep,
    timeout: float = 600,
    clock: Callable[[], float] = time.monotonic,
) -> list[Analysis]:
    paths = outputs["reachability_paths"]
    started = {
        name: ec2.start_network_insights_analysis(NetworkInsightsPathId=p["id"])[
            "NetworkInsightsAnalysis"
        ]["NetworkInsightsAnalysisId"]
        for name, p in paths.items()
    }
    deadline = clock() + timeout
    done: dict[str, dict] = {}
    while len(done) < len(started):
        sleep(10)
        pending = [aid for name, aid in started.items() if name not in done]
        found = ec2.describe_network_insights_analyses(NetworkInsightsAnalysisIds=pending)
        for a in found["NetworkInsightsAnalyses"]:
            if a["Status"] == "failed":
                raise RunnerError(
                    f"analysis {a['NetworkInsightsAnalysisId']} failed: {a.get('StatusMessage')}"
                )
            if a["Status"] == "succeeded":
                name = next(
                    n for n, aid in started.items() if aid == a["NetworkInsightsAnalysisId"]
                )
                done[name] = a
        if len(done) < len(started) and clock() > deadline:
            raise RunnerError(f"analyses not finished after {timeout:.0f} s")
    return [summarise(n, matching_check(matrix, outputs, paths[n]), done[n]) for n in paths]


def reachability_report(analyses: list[Analysis]) -> str:
    lines = [
        "| Result | Path | Matrix row | Expected | Path found | Layer blamed | Explanation codes |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in analyses:
        expected = (
            ""
            if a.check is None
            else (
                a.check.expect if a.check.layer is None else f"{a.check.expect} ({a.check.layer})"
            )
        )
        result = {True: "pass", False: "**FAIL**", None: "n/a"}[a.passed]
        lines.append(
            f"| {result} | {a.name} | {a.check.id if a.check else ''} | {expected} "
            f"| {'yes' if a.found else 'no'} | {', '.join(a.layers) or '-'} "
            f"| {', '.join(a.codes) or '-'} |"
        )
    blamed = [f"- {a.name}: {'; '.join(a.components)}" for a in analyses if a.components]
    if blamed:
        lines += ["", "Components named in the explanations:", "", *blamed]
    return "\n".join(lines)


# --- command line --------------------------------------------------------------


def save(
    kind: str, body: str, intro: str, outputs: dict[str, Any], directory: Path, now: datetime
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{outputs['stage']}-{kind}-{now.strftime('%Y-%m-%dT%H%MZ')}.md"
    title = {"flowlogs": "Flow log evidence", "reachability": "Reachability Analyzer"}[kind]
    header = (
        f"# {title}: {outputs['stage']}\n\n"
        f"Run at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC by `scripts/evidence.py {kind}`. "
        f"{intro}\n\n"
    )
    path.write_text(header + body + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("flowlogs", "reachability"):
        p = sub.add_parser(name)
        p.add_argument("--outputs", type=Path, required=True, help="terraform output -json")
        p.add_argument("--save", type=Path, help="directory for the Markdown report")
        if name == "flowlogs":
            p.add_argument("--since", type=float, required=True, help="probe window start, epoch s")
            p.add_argument("--until", type=float, help="probe window end, epoch s (default: now)")
            p.add_argument("--wait", type=float, default=1200, help="seconds to wait for delivery")
    args = parser.parse_args(argv)

    import boto3  # only talking to AWS needs it

    outputs = read_outputs(args.outputs)
    try:
        matrix = load()
    except MatrixError as e:
        print(f"invalid matrix\n{e}", file=sys.stderr)
        return 1
    missing = "flow_logs" if args.command == "flowlogs" else "reachability_paths"
    if missing not in outputs:
        print(
            f"The deployed lab has no {missing} output (stage m4 or later needed).", file=sys.stderr
        )
        return 1
    try:
        if args.command == "flowlogs":
            until = args.until or time.time()
            exps = expectations(matrix, outputs)
            print(f"Waiting for flow logs (up to {args.wait / 60:.0f} min)...", flush=True)
            findings, delay = collect(
                boto3.client("athena"), outputs, exps, args.since, until, args.wait
            )
            body = flowlog_report(findings, delay)
            intro = (
                f"Probe window {datetime.fromtimestamp(args.since, UTC):%H:%M:%S} to "
                f"{datetime.fromtimestamp(until, UTC):%H:%M:%S} UTC; flow logs with 1-minute "
                "aggregation, Parquet in S3, queried with Athena."
            )
            ok = all(f.passed is not False for f in findings)
        else:
            n = len(outputs["reachability_paths"])
            print(
                f"Running {n} Reachability Analyzer analyses (${n * ANALYSIS_PRICE:.2f})...",
                flush=True,
            )
            analyses = analyse_paths(boto3.client("ec2"), matrix, outputs)
            body = reachability_report(analyses)
            intro = "One analysis per path, each repeating an IPv4 matrix row."
            ok = all(a.passed is not False for a in analyses)
    except RunnerError as e:
        print(f"evidence failed: {e}", file=sys.stderr)
        return 1
    print(body)
    if args.save:
        print(f"Saved to {save(args.command, body, intro, outputs, args.save, datetime.now(UTC))}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
