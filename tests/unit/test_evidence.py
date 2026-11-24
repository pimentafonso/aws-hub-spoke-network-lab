from datetime import UTC, datetime

import pytest

import evidence
import failover
from matrix import load

SINCE = 1_795_000_000.0  # 2026-11-17T08:26:40Z
UNTIL = SINCE + 60

HOSTS = {
    "hub-svc": ("10.0.0.140", "2a05:d018:0:7b00::10", "eni-hub"),
    "spoke-a": ("10.1.0.10", "2a05:d018:0:a00::10", "eni-a"),
    "spoke-b": ("10.2.0.70", "2a05:d018:0:b00::70", "eni-b"),
    "onprem": ("172.16.0.165", None, "eni-onprem"),
    "consumer": ("10.1.0.20", None, "eni-consumer"),
}
VPCS = {
    "hub": "vpc-hub",
    "spoke-a": "vpc-a",
    "spoke-b": "vpc-b",
    "onprem": "vpc-onprem",
    "consumer": "vpc-consumer",
}


@pytest.fixture
def outputs():
    return {
        "stage": "m4",
        "hosts": {
            name: {"instance_id": f"i-{name}", "ipv4": v4, "ipv6": v6, "eni": eni}
            for name, (v4, v6, eni) in HOSTS.items()
        },
        "vpcs": {name: {"id": vid} for name, vid in VPCS.items()},
        "privatelink_endpoint_dns": "vpce-1.example.amazonaws.com",
        "privatelink": {"endpoint_enis": ["eni-vpce"], "nlb_ips": ["10.1.0.30", "10.1.1.30"]},
        "flow_logs": {"bucket": "b", "database": "db", "table": "t", "workgroup": "wg"},
        "reachability_paths": {
            "spoke-a-to-spoke-b-web": {
                "from": "spoke-a",
                "to": "spoke-b",
                "port": 80,
                "id": "nip-1",
            },
        },
    }


@pytest.fixture
def exps(outputs):
    return {e.check.id: e for e in evidence.expectations(load(), outputs)}


def record(src, dst, port, action, eni="eni-x", vpc="vpc-x", start=SINCE + 10):
    return {
        "interface_id": eni,
        "vpc_id": vpc,
        "srcaddr": src,
        "dstaddr": dst,
        "dstport": str(port),
        "action": action,
        "start": str(int(start)),
        "end": str(int(start) + 60),
    }


# --- what each row should leave ------------------------------------------------


def test_each_row_gets_the_evidence_its_layer_implies(exps):
    assert len(exps) == 19
    assert exps["spoke-a-to-hub-web"].want == "ACCEPT"
    assert exps["spoke-a-to-hub-web"].enis == ("eni-hub",)
    assert exps["spoke-a-to-hub-admin"].want == "REJECT"
    assert exps["spoke-a-to-spoke-b"].want == "none"
    assert exps["spoke-a-to-spoke-b"].vpc == "vpc-b"
    assert exps["spoke-a-to-internet"].want == "skip"
    assert exps["consumer-to-internet"].want == "skip"


def test_privatelink_is_checked_at_the_endpoint(exps):
    e = exps["consumer-to-privatelink"]
    assert (e.want, e.enis, e.destination, e.source) == ("ACCEPT", ("eni-vpce",), "", "10.1.0.20")


def test_consumer_direct_probe_must_not_reach_spoke_a(exps):
    e = exps["consumer-to-spoke-a-direct"]
    assert (e.want, e.vpc, e.source, e.destination) == ("none", "vpc-a", "10.1.0.20", "10.1.0.10")


# --- judging the records -------------------------------------------------------


def judge_one(e, records, latest=None):
    return evidence.judge([e], records, latest or {}, UNTIL)[0]


def test_allow_passes_on_an_accept_at_the_destination(exps):
    e = exps["spoke-a-to-hub-web"]
    assert judge_one(e, [record("10.1.0.10", "10.0.0.140", 80, "ACCEPT", eni="eni-hub")]).passed
    # The same flow seen elsewhere (the source's own interface) does not count.
    assert not judge_one(e, [record("10.1.0.10", "10.0.0.140", 80, "ACCEPT", eni="eni-a")]).passed


def test_security_group_deny_needs_a_reject_and_no_accept(exps):
    e = exps["spoke-a-to-hub-admin"]
    reject = record("10.1.0.10", "10.0.0.140", 8443, "REJECT", eni="eni-hub")
    accept = record("10.1.0.10", "10.0.0.140", 8443, "ACCEPT", eni="eni-hub")
    assert judge_one(e, [reject]).passed
    assert not judge_one(e, [reject, accept]).passed
    assert judge_one(e, []).observed == "no record yet"


def test_routing_deny_needs_silence_and_proof_the_logs_arrived(exps):
    e = exps["spoke-a-to-spoke-b"]
    assert judge_one(e, [], {"vpc-b": int(UNTIL) + 30}).passed
    late = judge_one(e, [], {"vpc-b": int(UNTIL) - 30})
    assert not late.passed
    assert "not in yet" in late.observed
    leaked = record("10.1.0.10", "10.2.0.70", 80, "ACCEPT", eni="eni-b", vpc="vpc-b")
    assert not judge_one(e, [leaked], {"vpc-b": int(UNTIL) + 30}).passed


def test_ipv6_addresses_match_whatever_their_spelling(exps):
    e = exps["spoke-a-to-hub-web-v6"]
    r = record(
        "2a05:d018:0000:0a00:0000:0000:0000:0010",
        "2a05:d018:0:7b00:0:0:0:10",
        80,
        "ACCEPT",
        eni="eni-hub",
    )
    assert judge_one(e, [r]).passed


def test_partition_filter_covers_every_hour_of_the_window():
    since = datetime(2026, 11, 23, 14, 58, tzinfo=UTC).timestamp()
    until = datetime(2026, 11, 23, 15, 3, tzinfo=UTC).timestamp()
    sql = evidence.partition_filter(since, until)
    for hour in ("14", "15", "16"):
        assert f"hour = '{hour}'" in sql
    assert "day = '23'" in sql
    assert "month = '11'" in sql


# --- polling Athena ------------------------------------------------------------


class FakeAthena:
    """Answers queries in turn: first nothing, then the records and coverage."""

    def __init__(self, rounds):
        self.rounds = rounds  # list of (records, coverage) per poll
        self.calls = 0
        self.queries = []

    def start_query_execution(self, QueryString, QueryExecutionContext, WorkGroup):
        self.queries.append(QueryString)
        return {"QueryExecutionId": str(len(self.queries))}

    def get_query_execution(self, QueryExecutionId):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def get_paginator(self, name):
        fake = self

        class Paginator:
            def paginate(self, QueryExecutionId):
                n = int(QueryExecutionId) - 1
                records, coverage = fake.rounds[min(n // 2, len(fake.rounds) - 1)]
                rows = records if n % 2 == 0 else coverage
                header = list(rows[0]) if rows else ["vpc_id"]
                data = [{"Data": [{"VarCharValue": h} for h in header]}]
                data += [{"Data": [{"VarCharValue": r[h]} for h in header]} for r in rows]
                return [{"ResultSet": {"Rows": data}}]

        return Paginator()


def test_collect_polls_until_every_row_has_evidence(outputs, exps):
    rows = [exps["spoke-a-to-hub-web"], exps["spoke-a-to-spoke-b"]]
    good = (
        [record("10.1.0.10", "10.0.0.140", 80, "ACCEPT", eni="eni-hub", vpc="vpc-hub")],
        [{"vpc_id": "vpc-b", "latest": str(int(UNTIL) + 60)}],
    )
    client = FakeAthena([([], []), good])
    now = [UNTIL + 100]

    def sleep(seconds):
        now[0] += seconds

    findings, delay = evidence.collect(
        client, outputs, rows, SINCE, UNTIL, wait=600, sleep=sleep, clock=lambda: now[0]
    )
    assert all(f.passed for f in findings)
    assert delay == pytest.approx(100 + evidence.POLL)
    assert "db.t" in client.queries[0]


def test_collect_gives_up_after_the_wait(outputs, exps):
    client = FakeAthena([([], [])])
    now = [UNTIL]

    def sleep(seconds):
        now[0] += seconds

    findings, delay = evidence.collect(
        client,
        outputs,
        [exps["spoke-a-to-hub-web"]],
        SINCE,
        UNTIL,
        wait=120,
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert delay is None
    assert findings[0].passed is False
    assert "FAIL" in evidence.flowlog_report(findings, delay)


# --- Reachability Analyzer -----------------------------------------------------


def test_reachability_blames_the_layer_the_matrix_names(outputs):
    matrix = load()
    path = outputs["reachability_paths"]["spoke-a-to-spoke-b-web"]
    check = evidence.matching_check(matrix, outputs, path)
    assert check.id == "spoke-a-to-spoke-b"
    analysis = {
        "NetworkPathFound": False,
        "Explanations": [
            {
                "ExplanationCode": "TGW_RTB_BLACKHOLE_ROUTE",
                "TransitGatewayRouteTable": {"Id": "tgw-rtb-1"},
            }
        ],
    }
    a = evidence.summarise("spoke-a-to-spoke-b-web", check, analysis)
    assert a.layers == ("routing",)
    assert a.passed
    assert "TransitGatewayRouteTable tgw-rtb-1" in a.components


def test_reachability_fails_when_another_layer_is_blamed(outputs):
    check = evidence.matching_check(
        load(), outputs, outputs["reachability_paths"]["spoke-a-to-spoke-b-web"]
    )
    analysis = {
        "NetworkPathFound": False,
        "Explanations": [
            {"ExplanationCode": "ENI_SG_RULES_MISMATCH", "SecurityGroups": [{"Id": "sg-1"}]}
        ],
    }
    a = evidence.summarise("p", check, analysis)
    assert a.layers == ("security-group",)
    assert a.passed is False
    assert "FAIL" in evidence.reachability_report([a])


def test_reachability_allow_needs_a_path(outputs):
    check = next(c for c in load().for_stage("m4") if c.id == "hub-to-spoke-a-admin")
    assert evidence.summarise("p", check, {"NetworkPathFound": True}).passed
    assert not evidence.summarise("p", check, {"NetworkPathFound": False}).passed


# --- strongSwan log capture in the failover test -------------------------------


def test_charon_command_filters_one_connection():
    cmd = failover.charon_command(SINCE, "tunnel1")
    assert "-u strongswan" in cmd
    assert "IKE_SA_NAME=tunnel1 " in cmd
    assert "grep" not in cmd
    assert f"tail -n {failover.CHARON_LINES}" in cmd


def test_failover_save_writes_the_strongswan_log_beside_the_report(tmp_path):
    result = failover.RunResult(
        number=1,
        cut=SINCE,
        restore=SINCE + 90,
        outage=None,
        bgp_down=None,
        bgp_up=None,
        failback_lost=0,
        path={"restore": "xfrm2"},
        inbound={},
        charon="09[IKE] <tunnel1|3> sending DPD request",
    )
    path = failover.save([result], tmp_path, datetime(2026, 11, 23, 15, 0, tzinfo=UTC), "m4")
    assert path.name == "m4-failover-2026-11-23T1500Z.md"
    log = (tmp_path / "m4-failover-2026-11-23T1500Z-strongswan.log").read_text(encoding="utf-8")
    assert log.startswith("# run 1: cut ")
    assert "sending DPD request" in log


class FakeEC2:
    def __init__(self, status="succeeded"):
        self.status = status
        self.deleted = []

    def start_network_insights_analysis(self, NetworkInsightsPathId):
        return {"NetworkInsightsAnalysis": {"NetworkInsightsAnalysisId": "nia-1"}}

    def describe_network_insights_analyses(self, NetworkInsightsAnalysisIds):
        return {
            "NetworkInsightsAnalyses": [
                {
                    "NetworkInsightsAnalysisId": "nia-1",
                    "Status": self.status,
                    "NetworkPathFound": False,
                    "Explanations": [
                        {
                            "ExplanationCode": "TGW_RTB_NO_ROUTE_TO_TGW_ATTACHMENT",
                            "TransitGatewayRouteTable": {"Id": "tgw-rtb-1"},
                        }
                    ],
                }
            ]
        }

    def delete_network_insights_analysis(self, NetworkInsightsAnalysisId):
        self.deleted.append(NetworkInsightsAnalysisId)


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_analyses_are_deleted_so_destroy_can_remove_the_paths(outputs, status):
    ec2 = FakeEC2(status)
    if status == "failed":
        with pytest.raises(evidence.RunnerError):
            evidence.analyse_paths(ec2, load(), outputs, sleep=lambda s: None)
    else:
        [a] = evidence.analyse_paths(ec2, load(), outputs, sleep=lambda s: None)
        assert a.passed
    assert ec2.deleted == ["nia-1"]
