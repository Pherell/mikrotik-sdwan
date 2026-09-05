#!/bin/bash
# Bring up the software hub.
#
# The controller configures peers through swanctl and vtysh rather than through
# a REST API, so this container is driven by files on a mounted volume. That is
# deliberate: giving a hub its own config API would be a second control plane to
# secure, and the controller already has one.
set -euo pipefail

: "${SOFTHUB_ASN:=65000}"
: "${SOFTHUB_ROUTER_ID:?SOFTHUB_ROUTER_ID must be set (the hub's loopback)}"
: "${SOFTHUB_LOOPBACK:=$SOFTHUB_ROUTER_ID}"

echo "softhub starting: AS${SOFTHUB_ASN} router-id ${SOFTHUB_ROUTER_ID}"

# Forwarding must be on or the hub silently drops everything it is meant to
# relay. Fail loudly rather than debugging a black hole later.
if ! sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1; then
    echo "FATAL: cannot enable ip_forward. Run with --privileged or" >&2
    echo "       --sysctl net.ipv4.ip_forward=1" >&2
    exit 1
fi
# IPsec transport mode needs these off, or the kernel drops decrypted packets
# whose source does not match the route back out.
for iface in all default; do
    sysctl -w "net.ipv4.conf.${iface}.rp_filter=0" >/dev/null 2>&1 || true
done

ip link show lo-sdwan >/dev/null 2>&1 || ip link add lo-sdwan type dummy
ip link set lo-sdwan up
ip addr replace "${SOFTHUB_LOOPBACK}/32" dev lo-sdwan

sed -e "s/{{ASN}}/${SOFTHUB_ASN}/g" \
    -e "s/{{ROUTER_ID}}/${SOFTHUB_ROUTER_ID}/g" \
    /etc/frr/frr.conf.template > /etc/frr/frr.conf
chown -R frr:frr /etc/frr /var/run/frr

/usr/lib/frr/frrinit.sh start
/usr/sbin/charon-systemd &
sleep 2
swanctl --load-all || echo "no swanctl config yet; waiting for the controller"

echo "softhub ready"
# Keep PID 1 alive and surface both daemons' logs.
tail -F /var/log/frr/frr.log /var/log/messages 2>/dev/null &
wait -n
