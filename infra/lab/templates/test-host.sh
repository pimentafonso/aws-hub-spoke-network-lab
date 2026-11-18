#!/bin/bash
# Test host on Amazon Linux 2023: answer HTTP on TCP 80 (web) and 8443 (admin),
# on IPv4 and IPv6. Nothing is installed, so the host does not depend on the
# lab's egress to boot. The matrix runner probes it through SSM Run Command.
set -euo pipefail

mkdir -p /srv/www
echo "${name}" >/srv/www/index.html

for port in 80 8443; do
  cat >"/etc/systemd/system/nwlab-http-$port.service" <<EOF
[Unit]
Description=nwlab test listener on TCP $port
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 -m http.server $port --bind :: --directory /srv/www
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  systemctl enable --now "nwlab-http-$port.service"
done
