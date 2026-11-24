#!/usr/bin/env bash
# The host firewall for the crawler's network (deploy/prod/tools.yml; deploy/README.md, "The crawler's network"; PRODUCTION_REVIEW.md R20).
#
# The crawler is a browser that follows whatever a page tells it to load, so a hostile page can send it to the droplet's metadata service (169.254.169.254: instance credentials), the VPC,
# or another container. A bridge network has internet egress and Docker has no option to take that away; what stops it is a rule in the host's DOCKER-USER chain, scoped to the
# crawler's subnet. This script installs it, and the same rules for traffic to the host itself (the INPUT chain, which DOCKER-USER never sees).
#
#   apply     create the rules (idempotent: running it twice leaves one set)
#   remove    delete them
#   status    exit 0 only if every rule is in place (compose.sh runs this before it starts anything with the crawler enabled)
#   check     prove it from inside the running crawler: probe an address in each blocked range and read the rules' PACKET COUNTERS. A timeout alone cannot tell "dropped" from
#             "nothing there"; a counter that moved can. Also confirms the internet still works.
#   install   a systemd unit that applies the rules after Docker starts (and again when Docker restarts), so a reboot cannot leave the crawler unprotected
#
# Needs root, or passwordless sudo (the deploy user has it). CRAWLER_SUBNET must equal the subnet in tools.yml; IPTABLES, CRAWLER_CONTAINER and SYSTEMD_UNIT_DIR exist for tests.
set -euo pipefail

SUBNET="${CRAWLER_SUBNET:-172.30.50.0/24}"
IPT="${IPTABLES:-iptables}"
EGRESS=CRAWLER-EGRESS
INGRESS=CRAWLER-INPUT
# What the crawler must not reach, each with an address that is in the range and will not be a real host: link-local (the metadata service), the VPC and private ranges, the other
# Docker networks (172.16/12), and carrier-grade NAT.
RANGES=(169.254.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10)
PROBES=(169.254.169.254 10.255.255.1 172.31.255.1 192.168.255.1 100.64.0.1)

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo -n"
ipt() { $SUDO "$IPT" "$@"; }

apply() {
  ipt -N "$EGRESS" 2>/dev/null || ipt -F "$EGRESS"
  ipt -A "$EGRESS" -d "$SUBNET" -j RETURN   # its own network: the app roles reach it, it reaches them
  for range in "${RANGES[@]}"; do ipt -A "$EGRESS" -d "$range" -j DROP; done
  ipt -A "$EGRESS" -j RETURN                # everything else is the internet
  ipt -C DOCKER-USER -s "$SUBNET" -j "$EGRESS" 2>/dev/null || ipt -I DOCKER-USER 1 -s "$SUBNET" -j "$EGRESS"

  ipt -N "$INGRESS" 2>/dev/null || ipt -F "$INGRESS"
  ipt -A "$INGRESS" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT   # replies to what the host started
  ipt -A "$INGRESS" -j DROP                                                # the host's own services (sshd, ...) are not the crawler's to reach
  ipt -C INPUT -s "$SUBNET" -j "$INGRESS" 2>/dev/null || ipt -I INPUT 1 -s "$SUBNET" -j "$INGRESS"
  echo "crawler firewall applied for $SUBNET"
}

remove() {
  ipt -D DOCKER-USER -s "$SUBNET" -j "$EGRESS" 2>/dev/null || true
  ipt -D INPUT -s "$SUBNET" -j "$INGRESS" 2>/dev/null || true
  for chain in "$EGRESS" "$INGRESS"; do ipt -F "$chain" 2>/dev/null || true; ipt -X "$chain" 2>/dev/null || true; done
  echo "crawler firewall removed for $SUBNET"
}

status() {
  local missing=0
  ipt -C DOCKER-USER -s "$SUBNET" -j "$EGRESS" 2>/dev/null || { echo "missing: DOCKER-USER jump for $SUBNET" >&2; missing=1; }
  ipt -C INPUT -s "$SUBNET" -j "$INGRESS" 2>/dev/null || { echo "missing: INPUT jump for $SUBNET" >&2; missing=1; }
  for range in "${RANGES[@]}"; do
    ipt -C "$EGRESS" -d "$range" -j DROP 2>/dev/null || { echo "missing: drop for $range" >&2; missing=1; }
  done
  [ "$missing" -eq 0 ] || { echo "the crawler firewall is not installed: run $0 install" >&2; return 1; }
  echo "crawler firewall in place for $SUBNET"
}

check() {
  status
  local container="${CRAWLER_CONTAINER:-$(docker ps -q --filter label=com.docker.compose.service=crawl4ai | head -1)}"
  [ -n "$container" ] || { echo "the crawler is not running" >&2; return 2; }
  ipt -Z "$EGRESS"
  for probe in "${PROBES[@]}"; do docker exec "$container" curl -m 3 -s -o /dev/null "http://$probe/" || true; done
  local failed=0 i pkts
  for i in "${!RANGES[@]}"; do
    pkts=$(ipt -nvx -L "$EGRESS" | awk -v r="${RANGES[$i]}" '$3 == "DROP" && $NF == r {print $1}' | head -1)
    if [ "${pkts:-0}" -gt 0 ]; then echo "dropped  ${RANGES[$i]}  ($pkts packets)"; else echo "NOT DROPPED  ${RANGES[$i]}"; failed=1; fi
  done
  if docker exec "$container" curl -m 8 -s -o /dev/null https://example.com; then echo "internet reachable"; else echo "the internet is NOT reachable from the crawler" >&2; failed=1; fi
  return "$failed"
}

install() {
  local self unit="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}/crawler-firewall.service"
  self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  $SUDO tee "$unit" >/dev/null <<EOF
[Unit]
Description=Egress firewall for the crawler's Docker network
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$self apply
ExecStop=$self remove

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now crawler-firewall.service
  echo "installed $unit (starts after Docker, and again when Docker restarts)"
}

case "${1:-}" in
  apply) apply ;;
  remove) remove ;;
  status) status ;;
  check) check ;;
  install) install ;;
  *) echo "usage: $0 apply|remove|status|check|install" >&2; exit 2 ;;
esac
