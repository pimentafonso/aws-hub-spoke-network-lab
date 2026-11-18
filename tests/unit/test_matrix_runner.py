import base64
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime

import matrix_probe
import pytest

import matrix

OUTPUTS = {
    "stage": "m2",
    "egress": "nat-gateway",
    "hosts": {
        name: {
            "instance_id": f"i-{n}",
            "vpc": vpc,
            "ipv4": f"10.{n}.0.10",
            "ipv6": f"2001:db8:{n}::10",
        }
        for n, (name, vpc) in enumerate(
            [("hub-svc", "hub"), ("spoke-a", "spoke-a"), ("spoke-b", "spoke-b")]
        )
    },
}


def encode(probes, timeout=1):
    return base64.b64encode(json.dumps({"timeout": timeout, "probes": probes}).encode()).decode()


# --- the probe that runs on the hosts -------------------------------------------


@pytest.fixture
def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen()
    yield s.getsockname()[1]
    s.close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_reports_allow_and_refused(listener):
    results = matrix_probe.main(
        encode(
            [
                {"id": "open", "address": "127.0.0.1", "port": listener, "family": "ipv4"},
                {"id": "closed", "address": "127.0.0.1", "port": closed_port(), "family": "ipv4"},
                {"id": "no-v6", "address": "127.0.0.1", "port": listener, "family": "ipv6"},
            ]
        )
    )
    assert results["open"]["result"] == "allow"
    assert results["open"]["detail"].startswith("connected in")
    assert results["open"]["address"] == "127.0.0.1"
    assert results["closed"]["result"] == "deny"
    if sys.platform != "win32":  # Windows retries a refused localhost connect until timeout
        assert results["closed"]["detail"] == "refused"
    assert results["no-v6"]["result"] == "deny"
    assert results["no-v6"]["detail"].startswith("no ipv6 address")


def test_shell_command_carries_the_probe_and_its_list(listener, tmp_path):
    """Run the generated command's two halves locally, as a host would."""
    probes = [matrix.Probe("open", "127.0.0.1", listener, "ipv4")]
    command = matrix.shell_command(probes)
    program_b64 = command.split()[1]
    probes_b64 = command.split()[-1]
    program = tmp_path / "probe.py"
    program.write_bytes(base64.b64decode(program_b64))
    assert program.read_bytes() == matrix.PROBE_PATH.read_bytes()
    out = subprocess.run(  # noqa: S603 - our own probe, run with this interpreter
        [sys.executable, "-I", str(program), probes_b64],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out)["open"]["result"] == "allow"


# --- resolving and planning -------------------------------------------------------


def test_resolve_hosts_targets_and_outputs():
    m = matrix.load()
    assert matrix.resolve("spoke-a", "ipv6", m, OUTPUTS) == "2001:db8:1::10"
    assert matrix.resolve("internet", "ipv4", m, OUTPUTS) == "s3.dualstack.eu-west-1.amazonaws.com"
    outputs = {**OUTPUTS, "privatelink_endpoint_dns": "vpce-1.example"}
    assert matrix.resolve("privatelink", "ipv4", m, outputs) == "vpce-1.example"
    with pytest.raises(matrix.RunnerError, match="privatelink_endpoint_dns"):
        matrix.resolve("privatelink", "ipv4", m, OUTPUTS)
    with pytest.raises(matrix.RunnerError, match="onprem has no ipv4"):
        matrix.resolve("onprem", "ipv4", m, OUTPUTS)


def test_plan_groups_probes_by_source_instance():
    m = matrix.load()
    plan = matrix.plan_probes(m.for_stage("m2"), m, OUTPUTS)
    assert set(plan) == {"i-0", "i-1", "i-2"}
    assert sum(len(p) for p in plan.values()) == len(m.for_stage("m2"))
    assert matrix.Probe("spoke-a-to-spoke-b", "10.2.0.10", 80, "ipv4") in plan["i-1"]


# --- running through a fake SSM ----------------------------------------------------


class FakeSSM:
    class exceptions:
        class InvocationDoesNotExist(Exception):
            pass

    def __init__(self, results, status="Success", online_after=1):
        self.results = results  # check id -> result dict
        self.status = status
        self.online_after = online_after
        self.describe_calls = 0
        self.sent = {}
        self.polls = {}

    def describe_instance_information(self, Filters):
        self.describe_calls += 1
        ids = Filters[0]["Values"]
        ping = "Online" if self.describe_calls >= self.online_after else "ConnectionLost"
        return {"InstanceInformationList": [{"InstanceId": i, "PingStatus": ping} for i in ids]}

    def send_command(self, InstanceIds, Parameters, **kwargs):
        command_id = f"cmd-{InstanceIds[0]}"
        encoded = Parameters["commands"][0].split()[-1]
        self.sent[InstanceIds[0]] = json.loads(base64.b64decode(encoded))["probes"]
        return {"Command": {"CommandId": command_id}}

    def get_command_invocation(self, CommandId, InstanceId):
        n = self.polls[InstanceId] = self.polls.get(InstanceId, 0) + 1
        if n == 1:
            raise self.exceptions.InvocationDoesNotExist()
        if n == 2:
            return {"Status": "InProgress"}
        ids = [p["id"] for p in self.sent[InstanceId]]
        return {
            "Status": self.status,
            "StandardOutputContent": json.dumps({i: self.results[i] for i in ids}),
            "StandardErrorContent": "python3: not found",
        }


def expected_results(m, flip=None):
    out = {}
    for c in m.for_stage("m2"):
        result = c.expect if c.id != flip else ("deny" if c.expect == "allow" else "allow")
        out[c.id] = {"result": result, "detail": "connected in 1 ms", "address": "10.0.0.1"}
    return out


def no_wait(_seconds):
    pass


def test_run_passes_when_every_row_matches(tmp_path):
    m = matrix.load()
    ssm = FakeSSM(expected_results(m), online_after=2)
    outcomes = matrix.run(m, OUTPUTS, ssm, sleep=no_wait)
    assert all(o.passed for o in outcomes)
    assert ssm.describe_calls == 2
    assert outcomes[0].detail == "connected in 1 ms (10.0.0.1)"

    path = matrix.save(outcomes, OUTPUTS, tmp_path, datetime(2026, 11, 19, 10, 5, tzinfo=UTC))
    assert path.name == "m2-nat-gateway-2026-11-19T1005Z.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Connectivity matrix: m2, nat-gateway")
    assert text.rstrip().endswith(f"{len(outcomes)} of {len(outcomes)} checks passed.")


def test_run_reports_a_difference():
    m = matrix.load()
    ssm = FakeSSM(expected_results(m, flip="spoke-a-to-spoke-b"))
    outcomes = matrix.run(m, OUTPUTS, ssm, sleep=no_wait)
    failed = [o.check.id for o in outcomes if not o.passed]
    assert failed == ["spoke-a-to-spoke-b"]


def test_failed_command_raises():
    m = matrix.load()
    with pytest.raises(matrix.RunnerError, match="command Failed: python3: not found"):
        matrix.run(m, OUTPUTS, FakeSSM(expected_results(m), status="Failed"), sleep=no_wait)


def test_offline_hosts_time_out():
    ticks = iter(range(0, 10_000, 100))
    ssm = FakeSSM({}, online_after=10**6)
    with pytest.raises(matrix.RunnerError, match="not online in SSM after 600 s: i-1"):
        matrix.wait_online(ssm, ["i-1"], sleep=no_wait, clock=lambda: next(ticks))


def test_cli_run_reads_terraform_outputs(tmp_path, monkeypatch, capsys):
    m = matrix.load()
    outputs = tmp_path / "outputs.json"
    outputs.write_text(
        json.dumps({k: {"value": v, "type": "x", "sensitive": False} for k, v in OUTPUTS.items()}),
        encoding="utf-8",
    )
    assert matrix.read_outputs(outputs) == OUTPUTS

    import boto3

    monkeypatch.setattr(boto3, "client", lambda name: FakeSSM(expected_results(m)))
    monkeypatch.setattr(matrix.time, "sleep", no_wait)
    assert matrix.main(["run", "--outputs", str(outputs), "--save", str(tmp_path / "r")]) == 0
    out = capsys.readouterr().out
    assert "11 of 11 checks passed." in out
    assert len(list((tmp_path / "r").glob("m2-nat-gateway-*.md"))) == 1
