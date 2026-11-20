"""Timed TCP probe that runs on the on-prem test host (sent by scripts/failover.py).

argv[1] is base64 JSON: {"address", "port", "seconds", "interval", "timeout",
"out"}. Opens a new TCP connection every `interval` seconds for `seconds`
seconds, each with a `timeout`, and writes the results to `out` as JSON, run
length encoded so a long run stays small enough for SSM's 24,000-character
output limit:

    {"interval": 0.2, "timeout": 0.5,
     "segments": [{"ok": true, "first": t, "last": t, "n": attempts}, ...]}

`first` and `last` are the wall-clock start times (epoch seconds) of the first
and last attempt in the segment. Standard library only: it runs with the
host's python3, detached, so the SSM command that starts it returns at once.
"""

import base64
import json
import socket
import sys
import time


def attempt(address: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def add(segments: list[dict], t: float, ok: bool) -> None:
    """Append one attempt, extending the last segment when the result is the same."""
    if segments and segments[-1]["ok"] == ok:
        segments[-1]["last"] = t
        segments[-1]["n"] += 1
    else:
        segments.append({"ok": ok, "first": t, "last": t, "n": 1})


def main(encoded: str) -> None:
    spec = json.loads(base64.b64decode(encoded))
    segments: list[dict] = []
    start = time.monotonic()
    n = 0
    while time.monotonic() - start < spec["seconds"]:
        add(segments, time.time(), attempt(spec["address"], spec["port"], spec["timeout"]))
        n += 1
        # Fixed schedule: attempt n+1 starts n+1 intervals after the first,
        # or straight away when a timeout has already overrun it.
        delay = start + n * spec["interval"] - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    result = {"interval": spec["interval"], "timeout": spec["timeout"], "segments": segments}
    with open(spec["out"], "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main(sys.argv[1])
