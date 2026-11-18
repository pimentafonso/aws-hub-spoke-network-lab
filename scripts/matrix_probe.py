"""Connection probe that runs on a lab test host (sent by scripts/matrix.py run).

argv[1] is base64 JSON: {"timeout": seconds, "probes": [{id, address, port,
family}]}. Prints one JSON object, {check id: {result, detail, address}}.
A completed TCP handshake is "allow"; anything else is "deny".
Standard library only: it runs with the host's python3.
"""

import base64
import json
import socket
import sys
import time


def probe(address: str, port: int, family: str, timeout: float) -> dict[str, str]:
    af = socket.AF_INET6 if family == "ipv6" else socket.AF_INET
    try:
        sockaddr = socket.getaddrinfo(address, port, af, socket.SOCK_STREAM)[0][4]
    except OSError as e:
        return {"result": "deny", "detail": f"no {family} address: {e}"}
    s = socket.socket(af, socket.SOCK_STREAM)
    s.settimeout(timeout)
    start = time.monotonic()
    try:
        s.connect(sockaddr)
        result, detail = "allow", f"connected in {(time.monotonic() - start) * 1000:.0f} ms"
    except TimeoutError:
        result, detail = "deny", f"timeout after {timeout:g} s"
    except ConnectionRefusedError:
        result, detail = "deny", "refused"
    except OSError as e:
        result, detail = "deny", e.strerror or str(e)
    finally:
        s.close()
    return {"result": result, "detail": detail, "address": str(sockaddr[0])}


def main(encoded: str) -> dict[str, dict[str, str]]:
    spec = json.loads(base64.b64decode(encoded))
    return {
        p["id"]: probe(p["address"], p["port"], p["family"], spec["timeout"])
        for p in spec["probes"]
    }


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1])))
