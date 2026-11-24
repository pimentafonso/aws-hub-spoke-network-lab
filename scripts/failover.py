"""VPN tunnel failover: cut the preferred tunnel and measure how long BGP takes.

The tunnels are active/standby (docs/adr/0006). Each run:

1. starts scripts/failover_probe.py on the on-prem host, which opens a TCP
   connection to spoke A's web port every 0.2 s with a 0.5 s timeout;
2. after a baseline, cuts tunnel 1 on the router: iptables drops everything to
   and from that tunnel's AWS endpoint, so neither side says goodbye and only
   dead peer detection and the BGP hold timer can notice;
3. later restores it and has strongSwan re-initiate the tunnel at once, as a
   router would when its link comes back;
4. collects the probe's results, the router's BGP neighbour changes (FRR logs
   them with millisecond timestamps), its route to spoke A and the packet
   counters of both tunnel interfaces, and the tunnel status AWS reports;
5. keeps strongSwan's log for tunnel 1, so a slow failback can be explained
   (saved next to the report as <report>-strongswan.log).

The outage is the time from the last connection that succeeded before the cut
to the first that succeeded after it, on the on-prem host's clock. Detection
is the time from the cut to FRR's "Down" for tunnel 1, on the router's clock.

Usage:
    python scripts/failover.py wait --outputs lab-outputs.json
    python scripts/failover.py run --outputs lab-outputs.json [--runs 3] [--save docs/results]

`wait` returns once both tunnels and both BGP sessions are up.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matrix import RunnerError, read_outputs, wait_online

PROBE_PATH = Path(__file__).resolve().parent / "failover_probe.py"
PROBE_OUT = "/tmp/nwlab-failover.json"  # noqa: S108 - a lab host's scratch file, not ours

INTERVAL = 0.2  # seconds between connection attempts
TIMEOUT = 0.5  # connect timeout of each attempt
BASELINE = 20  # seconds of traffic before the cut
CUT = 90  # seconds tunnel 1 stays cut
AFTER = 90  # seconds of traffic after the restore
SETTLE = 30  # seconds between runs once both sessions are up again

ADJCHANGE = re.compile(r"%ADJCHANGE: neighbor (?P<peer>[0-9.]+)\S* .*?\b(?P<state>Up|Down)\b")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # looked up at call time, so tests can replace it


# --- analysis (pure) -----------------------------------------------------------


@dataclass(frozen=True)
class Event:
    t: float  # epoch seconds
    peer: str
    state: str  # "Up" or "Down"


def parse_adjchanges(journal: str) -> list[Event]:
    """FRR neighbour changes from `journalctl -o short-iso-precise` lines."""
    events = []
    for line in journal.splitlines():
        m = ADJCHANGE.search(line)
        if not m:
            continue
        stamp = line.split(" ", 1)[0]
        try:
            t = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        events.append(Event(t, m["peer"], m["state"]))
    return events


@dataclass(frozen=True)
class Outage:
    start: float  # last successful attempt before the failures
    end: float  # first successful attempt after them
    lost: int  # failed attempts

    @property
    def seconds(self) -> float:
        return self.end - self.start


def outage_after(segments: list[dict], t: float, slack: float = 1.0) -> Outage | None:
    """The first run of failed attempts that ends after `t`, with its bounds.

    `slack` covers an attempt already in flight when the cut happened, and the
    small offset between the router's and the on-prem host's clocks.
    """
    for i, s in enumerate(segments):
        if s["ok"] or s["last"] < t - slack:
            continue
        if i == 0 or i + 1 >= len(segments):
            return None  # no success before or after: the run did not recover
        return Outage(segments[i - 1]["last"], segments[i + 1]["first"], s["n"])
    return Outage(t, t, 0)  # nothing failed


def failures_after(segments: list[dict], t: float) -> int:
    return sum(s["n"] for s in segments if not s["ok"] and s["first"] >= t)


def first_event(events: list[Event], peer: str, state: str, after: float) -> float | None:
    return next((e.t for e in events if e.peer == peer and e.state == state and e.t >= after), None)


def packet_deltas(before: dict, after: dict) -> dict[str, int]:
    """Packets received on each tunnel interface between two samples."""
    return {i: after["rx"][i] - before["rx"][i] for i in before["rx"]}


@dataclass
class RunResult:
    number: int
    cut: float
    restore: float
    outage: Outage | None
    bgp_down: float | None
    bgp_up: float | None
    failback_lost: int
    path: dict[str, str]  # sample name -> interface carrying traffic to spoke A
    inbound: dict[str, dict[str, int]]  # phase -> interface -> packets received
    aws: list[tuple[float, str, str]] = field(default_factory=list)  # (t, tunnel, status)
    charon: str = ""  # strongSwan's log lines for tunnel 1 during the run

    @property
    def detection(self) -> float | None:
        return None if self.bgp_down is None else self.bgp_down - self.cut

    @property
    def reconverge(self) -> float | None:
        return None if self.bgp_up is None else self.bgp_up - self.restore


def analyse(
    number: int,
    samples: dict[str, dict],
    probe: dict,
    journal: str,
    tunnel1_peer: str,
    aws: list[tuple[float, str, str]] | None = None,
) -> RunResult:
    """Turn one run's raw data into its measurements."""
    cut, restore = samples["cut"]["t"], samples["restore"]["t"]
    segments = probe["segments"]
    events = parse_adjchanges(journal)
    return RunResult(
        number=number,
        cut=cut,
        restore=restore,
        outage=outage_after(segments, cut),
        bgp_down=first_event(events, tunnel1_peer, "Down", cut),
        bgp_up=first_event(events, tunnel1_peer, "Up", restore),
        failback_lost=failures_after(segments, restore),
        path={name: s["dev"] for name, s in samples.items()},
        inbound={
            "before the cut": packet_deltas(samples["start"], samples["cut"]),
            "while cut": packet_deltas(samples["cut"], samples["restore"]),
            "after the restore": packet_deltas(samples["restore"], samples["end"]),
        },
        aws=aws or [],
    )


def _fmt(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds:.1f} s"


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%H:%M:%S.%f")[:-3]


def report(results: list[RunResult]) -> str:
    lines = [
        "| Run | Cut (UTC) | BGP down after | Outage | Probes lost | Path while cut "
        "| BGP up after restore | Lost on failback |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.number} | {_stamp(r.cut)} | {_fmt(r.detection)} "
            f"| {_fmt(r.outage.seconds if r.outage else None)} "
            f"| {r.outage.lost if r.outage else 'n/a'} | {r.path['restore']} "
            f"| {_fmt(r.reconverge)} | {r.failback_lost} |"
        )
    outages = [r.outage.seconds for r in results if r.outage]
    detections = [r.detection for r in results if r.detection is not None]
    lines.append("")
    for name, values in (("Outage", outages), ("BGP detection", detections)):
        if values:
            lines.append(
                f"- {name}: min {min(values):.1f} s, median {statistics.median(values):.1f} s, "
                f"max {max(values):.1f} s over {len(values)} run(s)."
            )
    lines += ["", "Packets received on each tunnel interface (traffic from AWS to on-prem):", ""]
    lines += ["| Run | Phase | xfrm1 (tunnel 1) | xfrm2 (tunnel 2) |", "|---|---|---|---|"]
    for r in results:
        for phase, counts in r.inbound.items():
            lines.append(f"| {r.number} | {phase} | {counts['xfrm1']} | {counts['xfrm2']} |")
    aws_lines = [
        f"- Run {r.number}: {tunnel} {status} at +{t - r.cut:.0f} s after the cut"
        for r in results
        for t, tunnel, status in r.aws
    ]
    if aws_lines:
        lines += ["", "Tunnel status changes reported by AWS (polled every 5 s):", "", *aws_lines]
    return "\n".join(lines)


# --- commands on the hosts ------------------------------------------------------


def sample_command(spoke_a: str) -> str:
    """Print one JSON line: the time, the tunnel counters and the route to spoke A."""
    program = (
        "import json,subprocess,time\n"
        "def ip(*a): return json.loads(subprocess.check_output(['ip','-j',*a]))\n"
        "rx={i: ip('-s','link','show',i)[0]['stats64']['rx']['packets']\n"
        "    for i in ('xfrm1','xfrm2')}\n"
        f"dev=ip('route','get','{spoke_a}')[0].get('dev')\n"
        "print(json.dumps({'t': time.time(), 'rx': rx, 'dev': dev}))\n"
    )
    return f"echo {base64.b64encode(program.encode()).decode()} | base64 -d | python3"


def cut_command(tunnel: dict, spoke_a: str) -> str:
    t = tunnel["outside"]
    return f"iptables -I INPUT -s {t} -j DROP && iptables -I OUTPUT -d {t} -j DROP && " + (
        sample_command(spoke_a)
    )


def restore_command(tunnel: dict, spoke_a: str) -> str:
    t, name = tunnel["outside"], tunnel["name"]
    return (
        f"{sample_command(spoke_a)} && "
        f"iptables -D INPUT -s {t} -j DROP && iptables -D OUTPUT -d {t} -j DROP && "
        f"(swanctl --terminate --ike {name} --force >/dev/null 2>&1 || true) && "
        f"(swanctl --initiate --child {name} --timeout 20 >/dev/null 2>&1 || true)"
    )


def probe_command(address: str, port: int, seconds: float) -> str:
    program = base64.b64encode(PROBE_PATH.read_bytes()).decode()
    spec = {
        "address": address,
        "port": port,
        "seconds": seconds,
        "interval": INTERVAL,
        "timeout": TIMEOUT,
        "out": PROBE_OUT,
    }
    spec_b64 = base64.b64encode(json.dumps(spec).encode()).decode()
    return (
        f"rm -f {PROBE_OUT} && echo {program} | base64 -d > /tmp/nwlab-failover-probe.py && "
        f"(nohup python3 /tmp/nwlab-failover-probe.py {spec_b64} >/dev/null 2>&1 &)"
    )


def read_probe_command() -> str:
    """Wait up to 60 s for the probe to finish writing, then print its results."""
    return f"for _ in $(seq 60); do [ -s {PROBE_OUT} ] && break; sleep 1; done; cat {PROBE_OUT}"


def journal_command(since: float) -> str:
    return (
        f"journalctl -o short-iso-precise --since @{int(since) - 1} --no-pager "
        "| grep ADJCHANGE || true"
    )


# SSM keeps at most 24,000 characters of output: the last 150 lines, cut short.
CHARON_LINES = 150
CHARON_WIDTH = 150


def charon_command(since: float, connection: str = "tunnel1") -> str:
    """strongSwan's log for one connection since `since`, from the journal.

    On 24 November 2026 a grep for the `<tunnel1|` prefix found nothing in any
    run. charon-systemd appears to keep the connection's name in the
    IKE_SA_NAME journal field rather than in the message, so this filters on
    the field; it is unconfirmed until the next session's failover run.
    """
    return (
        f"journalctl -u strongswan IKE_SA_NAME={connection} -o short-iso-precise "
        f"--since @{int(since) - 1} --no-pager "
        f"| tail -n {CHARON_LINES} | cut -c 1-{CHARON_WIDTH} || true"
    )


BGP_SUMMARY = "vtysh -c 'show bgp ipv4 unicast summary json'"


def run_command(
    ssm: Any,
    instance_id: str,
    command: str,
    timeout: float = 90,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Run one shell command through SSM and return its standard output."""
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment="nwlab VPN failover",
        TimeoutSeconds=max(30, int(timeout)),
        Parameters={"commands": [command]},
    )["Command"]["CommandId"]
    deadline = clock() + timeout
    while True:
        sleep(1)
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            inv = {"Status": "Pending"}
        if inv["Status"] == "Success":
            return inv["StandardOutputContent"]
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            stderr = inv.get("StandardErrorContent", "").strip()
            raise RunnerError(f"{instance_id}: command {inv['Status']}: {stderr[:500]}")
        if clock() > deadline:
            raise RunnerError(f"{instance_id}: no result after {timeout:.0f} s")


def established(summary_json: str, with_routes: bool = False) -> set[str]:
    """Peers in state Established, from `show bgp ipv4 unicast summary json`.

    With with_routes, only peers that have also sent at least one prefix.
    """
    peers = json.loads(summary_json or "{}").get("peers", {})
    return {
        p
        for p, s in peers.items()
        if s.get("state") == "Established" and (not with_routes or s.get("pfxRcd", 0) > 0)
    }


# On 24 November 2026 both sessions came up while Terraform was still creating
# the Transit Gateway route table association and propagations, and AWS sent
# no prefixes on either session until they were reset. A soft refresh did not
# help; a hard reset brought both routes within seconds.
BGP_RESET = "vtysh -c 'clear ip bgp *'"
SILENT_AFTER = 120.0


def tunnel_status(ec2: Any, connection_id: str) -> dict[str, str]:
    """AWS's view: tunnel outside address -> UP or DOWN."""
    vpn = ec2.describe_vpn_connections(VpnConnectionIds=[connection_id])["VpnConnections"][0]
    return {t["OutsideIpAddress"]: t["Status"] for t in vpn.get("VgwTelemetry", [])}


def wait_ready(
    ssm: Any,
    ec2: Any,
    vpn: dict,
    timeout: float = 900,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until AWS reports both tunnels UP and both sessions carry routes.

    If both sessions are up but AWS has sent no routes for SILENT_AFTER
    seconds, reset them once.
    """
    router = vpn["router_instance_id"]
    wait_online(ssm, [router], timeout=timeout, sleep=sleep, clock=clock)
    peers = {t["aws"] for t in vpn["tunnels"]}
    deadline = clock() + timeout
    silent_since: float | None = None
    reset = False
    while True:
        status = tunnel_status(ec2, vpn["connection_id"])
        up = set(status.values()) == {"UP"} and len(status) == len(vpn["tunnels"])
        if up:
            summary = run_command(ssm, router, BGP_SUMMARY, sleep=sleep)
            if peers <= established(summary, with_routes=True):
                return
            if peers <= established(summary):
                silent_since = silent_since if silent_since is not None else clock()
                if not reset and clock() - silent_since >= SILENT_AFTER:
                    print(
                        f"Both BGP sessions up but no routes from AWS for "
                        f"{SILENT_AFTER:.0f} s; resetting them."
                    )
                    run_command(ssm, router, BGP_RESET, sleep=sleep)
                    reset = True
            else:
                silent_since = None
        if clock() > deadline:
            raise RunnerError(f"VPN not ready after {timeout:.0f} s: AWS reports {status}")
        sleep(15)


def poll_aws(
    ec2: Any,
    vpn: dict,
    seconds: float,
    seen: dict[str, str],
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[tuple[float, str, str]]:
    """Poll AWS's tunnel status every 5 s for `seconds`; return the changes."""
    names = {t["outside"]: t["name"] for t in vpn["tunnels"]}
    changes = []
    end = clock() + seconds
    while clock() < end:
        for address, status in tunnel_status(ec2, vpn["connection_id"]).items():
            if seen.get(address) != status:
                if address in seen:
                    changes.append((time.time(), names.get(address, address), status))
                seen[address] = status
        sleep(5)
    return changes


def one_run(number: int, ssm: Any, ec2: Any, outputs: dict[str, Any]) -> RunResult:
    vpn = outputs["vpn"]
    router = vpn["router_instance_id"]
    onprem = outputs["hosts"]["onprem"]["instance_id"]
    spoke_a = outputs["hosts"]["spoke-a"]["ipv4"]
    tunnel1 = vpn["tunnels"][0]

    def sample(command: str) -> dict:
        return json.loads(run_command(ssm, router, command).strip().splitlines()[0])

    samples = {"start": sample(sample_command(spoke_a))}
    run_command(ssm, onprem, probe_command(spoke_a, 80, BASELINE + CUT + AFTER))
    seen: dict[str, str] = {}
    aws = poll_aws(ec2, vpn, BASELINE, seen)
    print(f"  run {number}: cutting {tunnel1['name']}", flush=True)
    samples["cut"] = sample(cut_command(tunnel1, spoke_a))
    aws += poll_aws(ec2, vpn, CUT, seen)
    print(f"  run {number}: restoring {tunnel1['name']}", flush=True)
    samples["restore"] = sample(restore_command(tunnel1, spoke_a))
    aws += poll_aws(ec2, vpn, AFTER + 5, seen)
    samples["end"] = sample(sample_command(spoke_a))
    probe = json.loads(run_command(ssm, onprem, read_probe_command(), timeout=120))
    journal = run_command(ssm, router, journal_command(samples["start"]["t"]))
    result = analyse(number, samples, probe, journal, tunnel1["aws"], aws)
    result.charon = run_command(ssm, router, charon_command(samples["start"]["t"], tunnel1["name"]))
    return result


def save(results: list[RunResult], directory: Path, now: datetime, stage: str = "m3") -> Path:
    """Write the report, and strongSwan's log for each run beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stage}-failover-{now.strftime('%Y-%m-%dT%H%MZ')}.md"
    log = "".join(
        f"# run {r.number}: cut {_stamp(r.cut)} UTC, restored {_stamp(r.restore)} UTC\n"
        f"{r.charon.rstrip()}\n\n"
        for r in results
        if r.charon.strip()
    )
    if log:
        path.with_name(path.stem + "-strongswan.log").write_text(
            log, encoding="utf-8", newline="\n"
        )
    else:
        print("No strongSwan log lines for tunnel 1 in any run; none saved.", file=sys.stderr)
    header = (
        f"# VPN tunnel failover: {stage}\n\n"
        f"Run at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC by `scripts/failover.py run`. "
        f"The on-prem host opened a TCP connection to spoke A's port 80 every {INTERVAL} s "
        f"with a {TIMEOUT} s timeout. Tunnel 1 was cut after {BASELINE} s by dropping all "
        f"traffic to and from its AWS endpoint on the router, restored {CUT} s later, and "
        "re-initiated by strongSwan at once. BGP timers: keepalive 10 s, hold 30 s; AWS dead "
        "peer detection: 30 s.\n\n"
    )
    path.write_text(header + report(results) + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("wait", "run"):
        p = sub.add_parser(name)
        p.add_argument("--outputs", type=Path, required=True, help="terraform output -json")
        if name == "run":
            p.add_argument("--runs", type=int, default=3)
            p.add_argument("--save", type=Path, help="directory for the Markdown report")
    args = parser.parse_args(argv)

    import boto3  # only talking to AWS needs it

    outputs = read_outputs(args.outputs)
    if "vpn" not in outputs:
        if args.command == "wait":
            print("The deployed lab has no VPN; nothing to wait for.")
            return 0
        print("The deployed lab has no VPN (stage m3 or later needed).", file=sys.stderr)
        return 1
    ssm, ec2 = boto3.client("ssm"), boto3.client("ec2")
    try:
        print("Waiting for both tunnels and BGP sessions...", flush=True)
        wait_ready(ssm, ec2, outputs["vpn"])
        if args.command == "wait":
            print("Both tunnels and BGP sessions are up.")
            return 0
        results = []
        for n in range(1, args.runs + 1):
            if n > 1:
                wait_ready(ssm, ec2, outputs["vpn"])
                _sleep(SETTLE)
            results.append(one_run(n, ssm, ec2, outputs))
    except RunnerError as e:
        print(f"failover failed: {e}", file=sys.stderr)
        return 1
    print(report(results))
    if args.save:
        print(f"Saved to {save(results, args.save, datetime.now(UTC), outputs['stage'])}")
    recovered = all(r.outage is not None and r.bgp_down is not None for r in results)
    return 0 if recovered else 1


if __name__ == "__main__":
    sys.exit(main())
