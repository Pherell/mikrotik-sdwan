# A software SD-WAN hub: strongSwan terminates IPsec, FRR runs BGP.
#
# Two jobs:
#   1. A real hub and route reflector in the cloud, so a fabric can be built
#      without buying a CHR licence.
#   2. A CI target -- FRR's route-reflector implementation is mature, which
#      makes it a reliable reference when RouterOS v7's is not.
FROM alpine:3.20

RUN apk add --no-cache \
        strongswan \
        frr \
        frr-pythontools \
        iproute2 \
        iptables \
        tini \
        curl \
        bash \
        python3 \
    && rm -rf /var/cache/apk/*

# Forwarding and policy routing are the whole point of this container.
RUN mkdir -p /etc/frr /etc/swanctl/conf.d /var/run/frr \
    && printf 'zebra=yes\nbgpd=yes\nstaticd=yes\n' > /etc/frr/daemons

COPY entrypoint.sh /entrypoint.sh
COPY frr.conf.template /etc/frr/frr.conf.template
RUN chmod +x /entrypoint.sh

# 500/4500 for IKE and NAT-T, 179 for BGP, 8443 for the controller's API.
EXPOSE 500/udp 4500/udp 179/tcp 8443/tcp

ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
