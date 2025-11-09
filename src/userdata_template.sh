#!/bin/bash

# Detect the primary network interface name
INTERFACE=$(ip route | grep default | awk '{print $5}' | head -n1)

# Configure secondary IP and routing
sudo tee /etc/rc.local <<EOF
#!/bin/bash

ip addr add {{ ROUTE_DESTINATION.split('/')[0] }}/24 dev $INTERFACE

mkdir -p /etc/iproute2
echo "200 secondary" | sudo tee -a /etc/iproute2/rt_tables
ip rule add to 10.0.0.0/24 table secondary
ip route add default via {{ AZ_SUBNET_DEF_ROUTE }} src {{ ROUTE_DESTINATION.split('/')[0] }} dev $INTERFACE table secondary
EOF

sudo chmod +x /etc/rc.local

# Create systemd service for rc.local
sudo tee /etc/systemd/system/rc-local.service <<'EOF'
[Unit]
Description=/etc/rc.local Compatibility
ConditionPathExists=/etc/rc.local
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
ExecStart=/etc/rc.local start
TimeoutSec=0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable rc-local
sudo systemctl start rc-local
