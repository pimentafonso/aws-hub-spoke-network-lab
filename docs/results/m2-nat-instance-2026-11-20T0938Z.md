# Connectivity matrix: m2, nat-instance

Run at 2026-11-20 09:38:48 UTC by `scripts/matrix.py run` through SSM Run Command, 5 s connect timeout.

| Result | Check | From | To | Port | Family | Expected | Observed | Detail |
|---|---|---|---|---|---|---|---|---|
| pass | spoke-a-to-hub-web | spoke-a | hub-svc | 80 | ipv4 | allow | allow | connected in 1 ms (10.0.0.29) |
| pass | spoke-b-to-hub-web | spoke-b | hub-svc | 80 | ipv4 | allow | allow | connected in 1 ms (10.0.0.29) |
| pass | spoke-a-to-hub-web-v6 | spoke-a | hub-svc | 80 | ipv6 | allow | allow | connected in 1 ms (2a05:d018:a18:1d00:2c00:3ddb:e9d6:dcd8) |
| pass | spoke-a-to-spoke-b | spoke-a | spoke-b | 80 | ipv4 | deny (routing) | deny | timed out (10.2.0.39) |
| pass | spoke-b-to-spoke-a | spoke-b | spoke-a | 80 | ipv4 | deny (routing) | deny | timed out (10.1.0.38) |
| pass | spoke-a-to-spoke-b-v6 | spoke-a | spoke-b | 80 | ipv6 | deny (routing) | deny | timed out (2a05:d018:15d5:a300:f20a:9a31:e731:5d4c) |
| pass | spoke-a-to-hub-admin | spoke-a | hub-svc | 8443 | ipv4 | deny (security-group) | deny | timed out (10.0.0.29) |
| pass | hub-to-spoke-a-admin | hub-svc | spoke-a | 8443 | ipv4 | allow | allow | connected in 1 ms (10.1.0.38) |
| pass | spoke-a-to-internet | spoke-a | internet | 443 | ipv4 | allow | allow | connected in 1 ms (52.218.61.219) |
| pass | spoke-b-to-internet | spoke-b | internet | 443 | ipv4 | allow | allow | connected in 1 ms (52.92.2.24) |
| pass | spoke-a-to-internet-v6 | spoke-a | internet | 443 | ipv6 | allow | allow | connected in 1 ms (2a05:d030:8000:40::305:43e3) |

11 of 11 checks passed.
