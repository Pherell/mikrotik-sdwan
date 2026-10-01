# Bootstrap only. Everything the controller manages is applied over the REST
# API afterwards, and carries an "sdwan:" comment; nothing below has one, so the
# reconciler must leave all of it alone. That is itself part of the test.

/system identity set name=spoke2

# Uplink onto the shared "internet" segment. ether2, not ether1: under
# vrnetlab ether1 is the VM's management port (172.31.255.30/30, NATed to the
# container's eth0), and the clab link "<node>:eth1" lands on ether2.
/ip address add address=198.51.100.12/24 interface=ether2 comment="lab uplink"

# A LAN the site originates into BGP.
/interface bridge add name=lan comment="lab lan"
/ip address add address=10.3.0.1/24 interface=lan comment="lab lan"

# REST API. A self-signed certificate is enough for a lab; the controller is
# started with SDWAN_DEVICE_VERIFY_TLS=false.
/certificate add name=lab common-name=spoke2 key-size=2048 days-valid=365
/certificate sign lab
/ip service set www-ssl certificate=lab disabled=no
/ip service set api disabled=yes
/ip service set www disabled=yes

# Controller account. Restricted to the management subnet -- and to
# 172.31.255.28/30, because vrnetlab DNATs the container's eth0 to the VM and
# masquerades, so the controller's requests arrive from 172.31.255.29.
/user add name=sdwan password=sdwan-lab group=full address=172.30.30.0/24,172.31.255.28/30
