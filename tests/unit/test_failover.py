import base64
import contextlib
import json
import socket
import threading
from datetime import UTC, datetime

import failover_probe
import pytest

import failover

T0 = 1_795_000_000.0  # an epoch time in November 2026


def seg(ok, first, last, n):
    return {"ok": ok, "first": T0 + first, "last": T0 + last, "n": n}


# --- the probe that runs on the on-prem host -----------------------------------


def test_probe_run_length_encodes_attempts():
    segments = []
    for t, ok in [(0.0, True), (0.2, True), (0.4, False), (0.6, False), (0.8, True)]:
        failover_probe.add(segments, t, ok)
    assert segments == [
        {"ok": True, "first": 0.0, "last": 0.2, "n": 2},
        {"ok": False, "first": 0.4, "last": 0.6, "n": 2},
        {"ok": True, "first": 0.8, "last": 0.8, "n": 1},
    ]


@pytest.fixture
def listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    stop = threading.Event()

    def accept():
        server.settimeout(0.1)
        while not stop.is_set():
            with contextlib.suppress(OSError):
                server.accept()[0].close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join()
    server.close()


def test_probe_writes_its_results(listener, tmp_path):
    out = tmp_path / "probe.json"
    spec = {
        "address": "127.0.0.1",
        "port": listener,
        "seconds": 0.3,
        "interval": 0.05,
        "timeout": 0.5,
        "out": str(out),
    }
    failover_probe.main(base64.b64encode(json.dumps(spec).encode()).decode())
    result = json.loads(out.read_text())
    assert result["interval"] == 0.05
    assert [s["ok"] for s in result["segments"]] == [True]
    assert result["segments"][0]["n"] >= 3


# --- analysis -------------------------------------------------------------------

JOURNAL = """\
2026-11-21T10:00:05.100000+00:00 router bgpd[812]: [M59KS-A3ZXZ] %ADJCHANGE: neighbor 169.254.10.1(tunnel1) in vrf default Up
2026-11-21T10:00:51.250000+00:00 router bgpd[812]: [M59KS-A3ZXZ] %ADJCHANGE: neighbor 169.254.10.1(tunnel1) in vrf default Down BGP Notification send
2026-11-21T10:00:51.300000+00:00 router bgpd[812]: some other line
2026-11-21T10:02:13.900000+00:00 router bgpd[812]: [M59KS-A3ZXZ] %ADJCHANGE: neighbor 169.254.10.1(tunnel1) in vrf default Up
"""  # noqa: E501


def test_parse_adjchanges_reads_peer_state_and_time():
    events = failover.parse_adjchanges(JOURNAL)
    assert [(e.peer, e.state) for e in events] == [
        ("169.254.10.1", "Up"),
        ("169.254.10.1", "Down"),
        ("169.254.10.1", "Up"),
    ]
    assert events[1].t == datetime(2026, 11, 21, 10, 0, 51, 250000, tzinfo=UTC).timestamp()


def test_outage_is_last_success_to_first_success():
    segments = [seg(True, 0, 20.0, 101), seg(False, 20.2, 49.0, 45), seg(True, 49.7, 90, 200)]
    outage = failover.outage_after(segments, T0 + 20.1)
    assert outage.lost == 45
    assert outage.seconds == pytest.approx(29.7)


def test_failure_in_flight_at_the_cut_counts():
    segments = [seg(True, 0, 19.8, 100), seg(False, 20.0, 40.0, 30), seg(True, 40.5, 60, 50)]
    outage = failover.outage_after(segments, T0 + 20.4)
    assert outage.seconds == pytest.approx(20.7)


def test_no_failures_is_a_zero_outage():
    outage = failover.outage_after([seg(True, 0, 90, 450)], T0 + 20)
    assert outage.seconds == 0
    assert outage.lost == 0


def test_no_recovery_is_no_outage_value():
    segments = [seg(True, 0, 20, 100), seg(False, 20.2, 90, 100)]
    assert failover.outage_after(segments, T0 + 20) is None


def test_failures_before_the_cut_are_ignored():
    segments = [
        seg(True, 0, 4, 20),
        seg(False, 4.2, 4.2, 1),
        seg(True, 4.4, 20, 80),
        seg(False, 20.2, 50, 40),
        seg(True, 50.2, 90, 200),
    ]
    assert failover.outage_after(segments, T0 + 20).lost == 40


def test_analyse_and_report():
    cut = datetime(2026, 11, 21, 10, 0, 21, tzinfo=UTC).timestamp()
    samples = {
        "start": {"t": cut - 20, "rx": {"xfrm1": 0, "xfrm2": 0}, "dev": "xfrm1"},
        "cut": {"t": cut, "rx": {"xfrm1": 100, "xfrm2": 0}, "dev": "xfrm1"},
        "restore": {"t": cut + 90, "rx": {"xfrm1": 110, "xfrm2": 300}, "dev": "xfrm2"},
        "end": {"t": cut + 180, "rx": {"xfrm1": 500, "xfrm2": 310}, "dev": "xfrm1"},
    }
    probe = {
        "segments": [
            {"ok": True, "first": cut - 20, "last": cut - 0.1, "n": 100},
            {"ok": False, "first": cut + 0.1, "last": cut + 30, "n": 40},
            {"ok": True, "first": cut + 30.5, "last": cut + 180, "n": 700},
        ]
    }
    r = failover.analyse(1, samples, probe, JOURNAL, "169.254.10.1")
    assert r.detection == pytest.approx(30.25)
    assert r.outage.seconds == pytest.approx(30.6)
    assert r.reconverge == pytest.approx(22.9)
    assert r.failback_lost == 0
    assert r.inbound["while cut"] == {"xfrm1": 10, "xfrm2": 300}
    text = failover.report([r])
    assert "| 1 | 10:00:21.000 | 30.2 s | 30.6 s | 40 | xfrm2 | 22.9 s | 0 |" in text
    assert "Outage: min 30.6 s, median 30.6 s, max 30.6 s over 1 run(s)." in text


# --- commands -----------------------------------------------------------------

TUNNEL = {"name": "tunnel1", "outside": "203.0.113.1", "cgw": "169.254.10.2", "aws": "169.254.10.1"}


def test_cut_and_restore_commands_target_tunnel_one_only():
    cut = failover.cut_command(TUNNEL, "10.1.0.10")
    assert "iptables -I INPUT -s 203.0.113.1 -j DROP" in cut
    assert "iptables -I OUTPUT -d 203.0.113.1 -j DROP" in cut
    restore = failover.restore_command(TUNNEL, "10.1.0.10")
    assert "iptables -D INPUT -s 203.0.113.1 -j DROP" in restore
    assert "swanctl --initiate --child tunnel1" in restore
    # The sample is taken before the drops are removed.
    assert restore.index("base64 -d | python3") < restore.index("iptables -D")


def test_established_reads_frr_summary():
    summary = {
        "routerId": "172.16.128.10",
        "peers": {"169.254.10.1": {"state": "Established"}, "169.254.11.1": {"state": "Active"}},
    }
    assert failover.established(json.dumps(summary)) == {"169.254.10.1"}
    assert failover.established("") == set()


class FakeSSM:
    class exceptions:
        class InvocationDoesNotExist(Exception):
            pass

    def __init__(self, status="Success", output="ok"):
        self.status, self.output, self.polls = status, output, 0

    def send_command(self, **kwargs):
        return {"Command": {"CommandId": "c1"}}

    def get_command_invocation(self, CommandId, InstanceId):
        self.polls += 1
        if self.polls == 1:
            raise self.exceptions.InvocationDoesNotExist()
        return {"Status": self.status, "StandardOutputContent": self.output}


def test_run_command_returns_output_and_raises_on_failure():
    assert failover.run_command(FakeSSM(), "i-1", "true", sleep=lambda _: None) == "ok"
    with pytest.raises(failover.RunnerError):
        failover.run_command(FakeSSM(status="Failed"), "i-1", "false", sleep=lambda _: None)


def test_wait_without_a_vpn_is_a_no_op(tmp_path, monkeypatch, capsys):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda _: None))
    outputs = tmp_path / "outputs.json"
    outputs.write_text(json.dumps({"stage": {"value": "m2"}}))
    assert failover.main(["wait", "--outputs", str(outputs)]) == 0
    assert "no VPN" in capsys.readouterr().out
    assert failover.main(["run", "--outputs", str(outputs)]) == 1
