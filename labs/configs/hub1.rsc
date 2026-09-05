# Bootstrap only. Everything the controller manages is applied over the REST
# API afterwards, and carries an "sdwan:" comment; nothing below has one, so the
# reconciler must leave all of it alone. That is itself part of the test.

/system identity set name=hub1

# Uplink onto the shared "internet" segment.
/ip address add address=198.51.100.5/24 interface=ether1 comment="lab uplink"

# A LAN the site originates into BGP.
/interface bridge add name=lan comment="lab lan"
/ip address add address=10.1.0.1/24 interface=lan comment="lab lan"

# REST API. A self-signed certificate is enough for a lab; the controller is
# started with SDWAN_DEVICE_VERIFY_TLS=false.
/certificate add name=lab common-name=hub1 key-size=2048 days-valid=365
/certificate sign lab
/ip service set www-ssl certificate=lab disabled=no
/ip service set api disabled=yes
/ip service set www disabled=yes

# Controller account. Restricted to the management subnet.
/user add name=sdwan password=sdwan-lab group=full address=172.30.30.0/24
