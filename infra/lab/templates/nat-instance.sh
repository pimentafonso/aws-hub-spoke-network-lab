#!/bin/bash
# NAT instance on Amazon Linux 2023: forward IPv4 from the lab ranges and
# masquerade it behind the instance's public address.
set -euo pipefail

cat >/etc/sysctl.d/90-nat.conf <<'EOF'
net.ipv4.ip_forward = 1
EOF
sysctl --system >/dev/null

dnf install -y -q iptables-services
iface=$(ip route show default | awk '{print $5; exit}')
iptables -t nat -F POSTROUTING
iptables -t nat -A POSTROUTING -o "$iface" -s 10.0.0.0/8 -j MASQUERADE
iptables -F FORWARD
iptables -P FORWARD ACCEPT
service iptables save
systemctl enable --now iptables
