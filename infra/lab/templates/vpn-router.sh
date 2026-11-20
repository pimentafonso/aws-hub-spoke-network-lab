#!/bin/bash
# On-prem router on Ubuntu 24.04: two route-based IPsec tunnels to the AWS VPN
# (strongSwan with XFRM interfaces) and one eBGP session over each (FRR).
#
#   xfrm1  tunnel 1, preferred both ways
#   xfrm2  tunnel 2, standby: lower local-pref in, AS path prepended out
#
# It also masquerades on-prem's internet traffic (the test host's SSM) behind
# its Elastic IP. Rendered by Terraform (vpn.tf); the pre-shared keys are read
# from SSM Parameter Store at boot and never pass through user data.
# Configuration is not persisted across a reboot: a lab router lives a session.
set -euo pipefail
exec > >(tee -a /var/log/nwlab-router.log) 2>&1
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get install -y -q --no-install-recommends \
  charon-systemd strongswan-swanctl frr python3-boto3 iptables

# --- forwarding -----------------------------------------------------------------
# rp_filter off: during a failover, replies can come back on the other tunnel.
cat >/etc/sysctl.d/90-nwlab-router.conf <<'EOF'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
EOF
sysctl --system >/dev/null

wan=$(ip route show default | awk '{print $5; exit}')

# --- XFRM interfaces: one per tunnel, matched to its SAs by if_id ---------------
%{ for i, t in tunnels ~}
ip link add xfrm${i + 1} type xfrm dev "$wan" if_id ${i + 1}
ip addr add ${t.cgw}/30 dev xfrm${i + 1}
# AES-256/SHA-256 ESP in UDP leaves about 1419 bytes of the 1500.
ip link set xfrm${i + 1} mtu 1419 up
sysctl -qw net.ipv4.conf.xfrm${i + 1}.rp_filter=0
%{ endfor ~}

# --- masquerade on-prem's internet traffic; clamp TCP MSS into the tunnels ------
iptables -t nat -A POSTROUTING -s ${router_ip} -j RETURN
iptables -t nat -A POSTROUTING -o "$wan" -s ${onprem_cidr} -j MASQUERADE
iptables -t mangle -A FORWARD -o xfrm+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

# --- strongSwan -----------------------------------------------------------------
# Routes come from BGP, not from strongSwan.
cat >/etc/strongswan.d/nwlab.conf <<'EOF'
charon {
  install_routes = no
}
charon-systemd {
  install_routes = no
}
EOF

psk() {
  python3 - "$1" <<'PY'
import sys
import boto3

name = sys.argv[1]
ssm = boto3.client("ssm", region_name="${region}")
print(ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"])
PY
}

# Plain assignments, so a failed read stops the script under set -e.
%{ for i, name in psk_names ~}
key${i + 1}=$(psk ${name})
[ -n "$key${i + 1}" ]
%{ endfor ~}

conf=/etc/swanctl/conf.d/nwlab-aws.conf
umask 077
{
  echo "connections {"
%{ for i, t in tunnels ~}
  cat <<'EOF'
  tunnel${i + 1} {
    version = 2
    remote_addrs = ${t.outside}
    proposals = aes256-sha256-modp2048
    mobike = no
    dpd_delay = 10s
    # Keep retrying after a cut, so the tunnel comes back on its own.
    keyingtries = 0
    local {
      auth = psk
      id = ${eip}
    }
    remote {
      auth = psk
      id = ${t.outside}
    }
    children {
      tunnel${i + 1} {
        local_ts = 0.0.0.0/0
        remote_ts = 0.0.0.0/0
        esp_proposals = aes256-sha256-modp2048
        if_id_in = ${i + 1}
        if_id_out = ${i + 1}
        rekey_time = 50m
        start_action = start
        dpd_action = restart
        close_action = start
      }
    }
  }
EOF
%{ endfor ~}
  echo "}"
  echo "secrets {"
%{ for i, t in tunnels ~}
  printf '  ike-tunnel%s {\n    id = %s\n    secret = "%s"\n  }\n' ${i + 1} "${t.outside}" "$key${i + 1}"
%{ endfor ~}
  echo "}"
} >"$conf"
umask 022

systemctl enable strongswan.service
systemctl restart strongswan.service
swanctl --load-all

# --- FRR: one eBGP session per tunnel -------------------------------------------
sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons
cat >/etc/frr/frr.conf <<'EOF'
frr defaults traditional
hostname nwlab-onprem-router
log syslog informational
log timestamp precision 3
!
! The VPC's range, so BGP has a route to advertise and replies reach on-prem's
! other subnets through the VPC router.
ip route ${onprem_cidr} ${vpc_router}
!
router bgp ${onprem_asn}
 bgp router-id ${router_ip}
 bgp log-neighbor-changes
 no bgp ebgp-requires-policy
%{ for i, t in tunnels ~}
 neighbor ${t.aws} remote-as ${aws_asn}
 neighbor ${t.aws} description tunnel${i + 1}
 neighbor ${t.aws} timers 10 30
%{ endfor ~}
 !
 address-family ipv4 unicast
  network ${onprem_cidr}
%{ for i, t in tunnels ~}
  neighbor ${t.aws} route-map TUNNEL${i + 1}-IN in
  neighbor ${t.aws} route-map TUNNEL${i + 1}-OUT out
%{ endfor ~}
 exit-address-family
!
route-map TUNNEL1-IN permit 10
 set local-preference 200
!
route-map TUNNEL1-OUT permit 10
!
route-map TUNNEL2-IN permit 10
 set local-preference 100
!
route-map TUNNEL2-OUT permit 10
 set as-path prepend%{ for n in range(prepend_times) } ${onprem_asn}%{ endfor }
!
EOF
chown frr:frr /etc/frr/frr.conf
systemctl enable frr
systemctl restart frr

touch /var/lib/nwlab-router-ready
echo "nwlab router configured"
