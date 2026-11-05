#!/usr/bin/env bash
set -euo pipefail

# Installs the Jetson-side WSS bridge for a dashboard hosted on GitHub Pages.
# DAC output intentionally remains DISARMED after boot.

if [[ "$EUID" -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

SERVICE_USER="${SUDO_USER:-}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
  SERVICE_USER="$(logname 2>/dev/null || true)"
fi
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
  echo "Could not determine your normal Linux username." >&2
  echo "Run: sudo SUDO_USER=YOUR_LINUX_USERNAME bash $0" >&2
  exit 1
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/alley-wheelchair"
CONF_DIR="/etc/alley-wheelchair"
TLS_DIR="$CONF_DIR/tls"
TOKEN_FILE="$CONF_DIR/bridge.token"

HOST_SHORT="$(hostname -s | tr -cd 'A-Za-z0-9-')"
if [[ -z "$HOST_SHORT" ]]; then HOST_SHORT="jetson"; fi
HOST_LOCAL="${HOST_SHORT}.local"

# Restrict the bridge to the network interface used by the Jetson's default
# route (normally the active Wi-Fi interface on a Wi-Fi setup). This avoids
# accepting clients from unrelated private subnets on secondary interfaces.
LAN_INTERFACE="$(ip -4 route show default | awk 'NR==1 {for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
if [[ -z "$LAN_INTERFACE" ]]; then
  echo "No default-route network interface found. Connect the Jetson to Wi-Fi/Ethernet and rerun." >&2
  exit 1
fi
mapfile -t LAN_CIDRS < <(ip -o -4 addr show dev "$LAN_INTERFACE" scope global | awk '{print $4}' | sort -u)
if [[ ${#LAN_CIDRS[@]} -eq 0 ]]; then
  echo "No IPv4 address found on $LAN_INTERFACE. Connect the Jetson to Wi-Fi/Ethernet and rerun." >&2
  exit 1
fi
ALLOWED_SUBNET_ARGS=""
SAN_ENTRIES="DNS:${HOST_LOCAL},DNS:${HOST_SHORT}"
LAN_IPS=()
for cidr in "${LAN_CIDRS[@]}"; do
  ALLOWED_SUBNET_ARGS+=" --allowed-subnet ${cidr}"
  lan_ip="${cidr%/*}"
  LAN_IPS+=("$lan_ip")
  SAN_ENTRIES+=",IP:${lan_ip}"
done

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-websockets openssl avahi-daemon i2c-tools
systemctl enable --now avahi-daemon

install -d -m 0755 "$CONF_DIR" "$TLS_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 "$INSTALL_DIR"

# Create a local certificate authority once, then issue a server certificate
# for the Jetson's stable mDNS name (hostname.local).
if [[ ! -s "$TLS_DIR/ca.key" || ! -s "$TLS_DIR/ca.crt" ]]; then
  rm -f "$TLS_DIR/ca.key" "$TLS_DIR/ca.crt"
  openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
    -subj "/CN=ALLEY Wheelchair LAN Certificate Authority" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"
fi

# Reissue the leaf certificate on every installer run so it follows the
# current hostname. The CA certificate remains stable for client trust.
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$TLS_DIR/server.key"
openssl req -new -key "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" \
  -subj "/CN=${HOST_LOCAL}"
cat > "$TLS_DIR/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${SAN_ENTRIES}
EOF
openssl x509 -req -in "$TLS_DIR/server.csr" \
  -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
  -out "$TLS_DIR/server.crt" -days 825 -sha256 -extfile "$TLS_DIR/server.ext"
rm -f "$TLS_DIR/server.csr" "$TLS_DIR/server.ext" "$TLS_DIR/ca.srl"

if [[ ! -s "$TOKEN_FILE" ]]; then
  openssl rand -hex 32 > "$TOKEN_FILE"
fi

chown root:root "$TLS_DIR/ca.key"
chmod 0600 "$TLS_DIR/ca.key"
chown root:root "$TLS_DIR/ca.crt"
chmod 0644 "$TLS_DIR/ca.crt"
chown "$SERVICE_USER:$SERVICE_GROUP" "$TLS_DIR/server.crt" "$TLS_DIR/server.key"
chmod 0644 "$TLS_DIR/server.crt"
chmod 0640 "$TLS_DIR/server.key"
chown root:"$SERVICE_GROUP" "$TOKEN_FILE"
chmod 0640 "$TOKEN_FILE"

install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
  "$SCRIPT_DIR/ALLEY_Wheelchair_Jetson_Bridge.py" \
  "$INSTALL_DIR/ALLEY_Wheelchair_Jetson_Bridge.py"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0644 \
  "$SCRIPT_DIR/index.html" "$INSTALL_DIR/index.html"

cat > /etc/systemd/system/alley-wheelchair-bridge.service <<EOF
[Unit]
Description=ALLEY Wheelchair Secure LAN WebSocket Bridge
Wants=network-online.target avahi-daemon.service
After=network-online.target avahi-daemon.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/ALLEY_Wheelchair_Jetson_Bridge.py --no-terminal-keys --voltage-mode 3.3 --dac-vdd 5.0 --tls-cert $TLS_DIR/server.crt --tls-key $TLS_DIR/server.key --token-file $TOKEN_FILE$ALLOWED_SUBNET_ARGS
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now alley-wheelchair-bridge.service

CA_FINGERPRINT="$(openssl x509 -in "$TLS_DIR/ca.crt" -noout -fingerprint -sha256)"
JETSON_IPS="${LAN_IPS[*]}"

echo
echo "============================================================"
echo "ALLEY WHEELCHAIR — GITHUB PAGES / SAME-WI-FI SETUP COMPLETE"
echo "============================================================"
echo "Jetson hostname:      $HOST_LOCAL"
echo "Secure WebSocket:     wss://$HOST_LOCAL:8765"
echo "Bridge token:         $(cat "$TOKEN_FILE")"
echo "CA certificate:       $TLS_DIR/ca.crt"
echo "CA SHA-256:           $CA_FINGERPRINT"
echo "Network interface:   $LAN_INTERFACE"
echo "Jetson LAN IP(s):     ${JETSON_IPS:-check with hostname -I}"
echo "Allowed subnet(s):    ${LAN_CIDRS[*]}"
echo
echo "NEXT STEPS"
echo "1. Copy $TLS_DIR/ca.crt to each browser device and trust it as a certificate authority."
echo "2. Copy the bridge token above into the dashboard's Private LAN access token field."
echo "3. In the GitHub Pages dashboard, enter $HOST_LOCAL and press Connect."
echo "4. Keep the Jetson and browser device on the same trusted Wi-Fi network."
echo
echo "Service status: sudo systemctl status alley-wheelchair-bridge.service"
echo "Service logs:   sudo journalctl -u alley-wheelchair-bridge.service -f"
echo "Restart:        sudo systemctl restart alley-wheelchair-bridge.service"
echo "Stop:           sudo systemctl disable --now alley-wheelchair-bridge.service"
echo
echo "DAC OUTPUT: DISARMED. This installer does not enable MCP4725 motor-control outputs."
echo "Do not port-forward TCP 8765 or expose this bridge to the internet."
echo "============================================================"
