import copy

import pytest

import matrix

BASE = {
    "version": 1,
    "stages": ["m2", "m3"],
    "hosts": {
        "a": {"vpc": "spoke-a", "families": ["ipv4", "ipv6"], "stage": "m2"},
        "b": {"vpc": "spoke-b", "families": ["ipv4", "ipv6"], "stage": "m2"},
        "onprem": {"vpc": "onprem", "families": ["ipv4"], "stage": "m3"},
    },
    "targets": {"internet": {"address": "example", "families": ["ipv4"], "stage": "m2"}},
    "checks": [
        {
            "id": "a-to-b",
            "from": "a",
            "to": "b",
            "port": 80,
            "family": "ipv4",
            "expect": "deny",
            "layer": "routing",
            "stage": "m2",
            "reason": "blackhole",
        },
        {
            "id": "onprem-to-a",
            "from": "onprem",
            "to": "a",
            "port": 80,
            "family": "ipv4",
            "expect": "allow",
            "stage": "m3",
            "reason": "propagated",
        },
    ],
}


def doc(**row_changes):
    """BASE with the first check changed; a value of None deletes the key."""
    d = copy.deepcopy(BASE)
    for key, value in row_changes.items():
        if value is None:
            d["checks"][0].pop(key, None)
        else:
            d["checks"][0][key] = value
    return d


def errors_of(d) -> str:
    with pytest.raises(matrix.MatrixError) as e:
        matrix.parse(d)
    return str(e.value)


def test_repository_matrix_is_valid():
    m = matrix.load()
    assert len(m.checks) >= 15
    assert {c.expect for c in m.checks} == {"allow", "deny"}


def test_stages_are_cumulative():
    m = matrix.parse(copy.deepcopy(BASE))
    assert [c.id for c in m.for_stage("m2")] == ["a-to-b"]
    assert [c.id for c in m.for_stage("m3")] == ["a-to-b", "onprem-to-a"]
    with pytest.raises(matrix.MatrixError):
        m.for_stage("m9")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"layer": None}, "a deny needs layer"),
        ({"expect": "allow"}, "layer only applies to deny rows"),
        ({"expect": "maybe"}, "expect must be one of"),
        ({"port": 0}, "port must be 1-65535"),
        ({"port": "80"}, "port must be 1-65535"),
        ({"port": True}, "port must be 1-65535"),
        ({"from": "internet"}, "from must be a host"),
        ({"to": "nowhere"}, "unknown destination"),
        ({"to": "a"}, "from and to are the same"),
        ({"family": "ipv5"}, "family must be one of"),
        ({"to": "onprem"}, "onprem is only deployed from stage m3"),
        ({"to": "internet", "family": "ipv6"}, "internet has no ipv6 address"),
        ({"reason": None}, "missing reason"),
        ({"colour": "red"}, "unknown key 'colour'"),
    ],
)
def test_invalid_rows_are_reported(changes, message):
    assert message in errors_of(doc(**changes))


def test_duplicate_ids_are_reported():
    d = copy.deepcopy(BASE)
    d["checks"][1]["id"] = "a-to-b"
    assert "a-to-b: duplicate id" in errors_of(d)


def test_every_problem_is_reported_at_once():
    d = doc(port=0, family="ipv5")
    d["version"] = 2
    message = errors_of(d)
    assert "version must be 1" in message
    assert "port must be 1-65535" in message
    assert "family must be one of" in message


def test_host_definitions_are_checked():
    d = copy.deepcopy(BASE)
    d["hosts"]["b"]["families"] = ["ipx"]
    d["targets"]["a"] = {"families": ["ipv4"], "stage": "m2"}
    message = errors_of(d)
    assert "b: families must be a subset" in message
    assert "a: defined as both a host and a target" in message


def test_compare_and_report():
    m = matrix.parse(copy.deepcopy(BASE))
    outcomes = matrix.compare(m.checks, {"a-to-b": "allow"})
    assert [o.passed for o in outcomes] == [False, False]
    text = matrix.report(outcomes)
    assert "| **FAIL** | a-to-b |" in text
    assert "deny (routing)" in text
    assert "no result" in text
    assert text.endswith("0 of 2 checks passed.")

    outcomes = matrix.compare(m.checks, {"a-to-b": "deny", "onprem-to-a": "allow"})
    assert all(o.passed for o in outcomes)
    assert matrix.report(outcomes).endswith("2 of 2 checks passed.")


def test_cli_validate(tmp_path, capsys):
    assert matrix.main(["validate"]) == 0
    assert "checks valid" in capsys.readouterr().out

    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nstages: [m2]\nchecks: []\n", encoding="utf-8")
    assert matrix.main(["validate", str(bad)]) == 1
    assert "the matrix has no checks" in capsys.readouterr().err
