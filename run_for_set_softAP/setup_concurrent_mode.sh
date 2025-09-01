#!/bin/bash

# setup_concurrent_mode.sh (v11 - Complete)
# Installs the critical 'libnss-mdns' package for .local name resolution.
# This is a cumulative script with all previous fixes included.

# stop on any error
set -e

# check if running as root
if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root. Please use sudo." >&2
  exit 1
fi

echo "--- 0. Disabling Conflicting Services ---"
# stop and disable common webservers that conflict on port 80
if systemctl list-units --type=service | grep -q 'nginx.service'; then
  echo "Disabling conflicting host nginx service..."
  systemctl stop nginx.service
  systemctl disable nginx.service
fi
# switch to systemd-networkd by disabling other network managers
systemctl stop NetworkManager 2>/dev/null || true
systemctl disable NetworkManager 2>/dev/null || true
systemctl stop dhcpcd 2>/dev/null || true
systemctl disable dhcpcd 2>/dev/null || true
echo "Conflicting services disabled."
echo ""

echo "--- 1. Installing All Necessary Packages ---"
apt update
# install all required packages, including the critical libnss-mdns
apt install -y hostapd dnsmasq python3-flask systemd-resolved iw libnss-mdns
echo "All necessary packages installed."
echo ""

echo "--- 2. Configuring Core Network & DNS Services ---"
# enable the services we need
systemctl enable systemd-networkd
systemctl enable systemd-resolved

# configure systemd-resolved to not conflict with dnsmasq
if grep -q "^DNSStubListener=" /etc/systemd/resolved.conf; then
    sed -i 's/^DNSStubListener=.*/DNSStubListener=no/' /etc/systemd/resolved.conf
else
    echo "DNSStubListener=no" >> /etc/systemd/resolved.conf
fi

# force the system to use DNS servers provided by systemd-resolved
ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf

# ensure nsswitch.conf is configured for mdns
NSS_CONFIG="/etc/nsswitch.conf"
HOSTS_LINE="hosts: files mdns4_minimal [NOTFOUND=return] dns mdns4"
if ! grep -q "^hosts:.*mdns4_minimal" "$NSS_CONFIG"; then
  echo "Fixing /etc/nsswitch.conf for .local resolution..."
  sed -i '/^hosts:/d' "$NSS_CONFIG"
  sed -i "1i ${HOSTS_LINE}" "$NSS_CONFIG"
fi
echo "Core network services enabled and configured for DNS."
echo ""

echo "--- 3. Configuring Avahi to Ignore Docker ---"
AVAHI_CONFIG="/etc/avahi/avahi-daemon.conf"
# configure avahi to only use the real wifi interfaces
if ! grep -q "^allow-interfaces=wlan0,uap0" "$AVAHI_CONFIG"; then
    echo "Configuring Avahi to use specific interfaces..."
    sed -i '/^#allow-interfaces=/c\allow-interfaces=wlan0,uap0' "$AVAHI_CONFIG"
fi
echo "Avahi configured."
echo ""

echo "--- 4. Setting up Hostname ---"
# get last 4 characters of the mac address
MAC_SUFFIX=$(cat /sys/class/net/wlan0/address | sed 's/://g' | cut -c 9-12)
HOSTNAME="ge-ccu-${MAC_SUFFIX}"
AP_PASSWORD="defaultPW"

# set the new hostname
hostnamectl set-hostname "${HOSTNAME}"
# robustly replace the 127.0.1.1 line in /etc/hosts
sed -i "/^127.0.1.1/d" /etc/hosts || true # delete existing line
echo "127.0.1.1	${HOSTNAME} ${HOSTNAME}-dashboard" >> /etc/hosts
echo "Hostname set to ${HOSTNAME} with alias ${HOSTNAME}-dashboard"
echo ""

# The rest of the script (sections 5 through 10) remains the same as v10
# It's included here for completeness.

echo "--- 5. Creating network configuration files ---"
cat > /etc/systemd/system/create_ap_interface.service << EOF
[Unit]
Description=Create AP virtual interface (uap0)
After=sys-subsystem-net-devices-wlan0.device
Before=systemd-networkd.service
[Service]
Type=oneshot
ExecStart=/sbin/iw dev wlan0 interface add uap0 type __ap
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/network/wlan0.network << EOF
[Match]
Name=wlan0
[Network]
DHCP=yes
EOF
cat > /etc/systemd/network/uap0.network << EOF
[Match]
Name=uap0
[Network]
Address=192.168.5.1/24
DHCPServer=no
EOF
echo "systemd-networkd files created."
echo ""

echo "--- 6. Configuring wpa_supplicant for STA mode ---"
cat > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf << EOF
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US
EOF
systemctl enable wpa_supplicant@wlan0.service
echo "wpa_supplicant configured."
echo ""

echo "--- 7. Configuring hostapd for AP mode ---"
cat > /etc/hostapd/hostapd.conf << EOF
interface=uap0
ssid=${HOSTNAME}
hw_mode=g
channel=7
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${AP_PASSWORD}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
systemctl enable hostapd.service
echo "hostapd configured."
echo ""

echo "--- 8. Configuring dnsmasq for AP DHCP ---"
cat > /etc/dnsmasq.conf << EOF
interface=uap0
dhcp-range=192.168.5.10,192.168.5.50,12h
domain=wlan
local=/local/
address=/#/192.168.5.1
EOF
systemctl enable dnsmasq.service
echo "dnsmasq configured."
echo ""

echo "--- 9. Creating the WiFi configuration web portal ---"
mkdir -p /opt/wifi_portal
cat > /opt/wifi_portal/app.py << 'EOF'
import subprocess
from flask import Flask, render_template_string, request

app = Flask(__name__)

HTML_TEMPLATE_FORM = '''
<!DOCTYPE html>
<html>
<head>
    <title>Wi-Fi 설정</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f0f2f5; }
        .container { max-width: 500px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 2px 15px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; }
        .label-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: -10px; }
        a.refresh-btn { font-size: 14px; text-decoration: none; color: #007bff; }
        a.refresh-btn:hover { text-decoration: underline; }
        select, input, button { width: 100%; padding: 12px; margin: 10px 0; display: inline-block; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; }
        button { background-color: #007bff; color: white; cursor: pointer; border: none; font-size: 16px; }
        button:hover { background-color: #0056b3; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Wi-Fi 연결 설정</h2>
        <form action="/save" method="post">
            <div class="label-container">
                <label for="ssid">Wi-Fi 네트워크 선택:</label>
                <a href="/" class="refresh-btn">새로고침</a>
            </div>
            <select id="ssid" name="ssid">
                {% for network in networks %}
                <option value="{{ network }}">{{ network }}</option>
                {% endfor %}
            </select>
            <label for="password">비밀번호:</label>
            <input type="password" id="password" name="password" autocomplete="current-password">
            <button type="submit">저장 및 연결</button>
        </form>
    </div>
</body>
</html>
'''

HTML_TEMPLATE_SUCCESS = '''
<!DOCTYPE html>
<html>
<head>
    <title>Wi-Fi 설정</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f0f2f5; text-align: center; }
        .container { max-width: 500px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 2px 15px rgba(0,0,0,0.1); }
        h2 { color: #333; }
    </style>
</head>
<body>
    <div class="container">
        <h2>설정이 저장되었습니다!</h2>
        <p>기기가 선택한 네트워크에 연결을 시도합니다.</p>
        <p>안정적인 작동을 위해 재부팅을 권장합니다.</p>
    </div>
</body>
</html>
'''

def get_wifi_ssids():
    try:
        cmd_output = subprocess.check_output(['iwlist', 'wlan0', 'scan'])
        output_str = cmd_output.decode('utf-8')
        ssids = set()
        for line in output_str.split('\n'):
            if 'ESSID:"' in line:
                ssid = line.split('ESSID:"')[1].split('"')[0]
                if ssid:
                    ssids.add(ssid)
        return sorted(list(ssids))
    except Exception:
        return []

def save_wifi_credentials(ssid, password):
    try:
        network_block = subprocess.check_output(['wpa_passphrase', ssid, password]).decode('utf-8')
    except subprocess.CalledProcessError:
        return False
    config_content = f'''ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

'''
    full_config = config_content + network_block
    try:
        with open("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf", "w") as f:
            f.write(full_config)
        return True
    except IOError:
        return False

@app.route("/")
def index():
    networks = get_wifi_ssids()
    return render_template_string(HTML_TEMPLATE_FORM, networks=networks)

@app.route("/save", methods=["POST"])
def save():
    ssid = request.form['ssid']
    password = request.form['password']
    if save_wifi_credentials(ssid, password):
        subprocess.run(['sudo', 'systemctl', 'restart', 'wpa_supplicant@wlan0.service'])
        return render_template_string(HTML_TEMPLATE_SUCCESS)
    else:
        return "Failed to save credentials.", 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80, debug=False)
EOF
cat > /etc/systemd/system/wifi-portal.service <<EOF
[Unit]
Description=WiFi Configuration Portal
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/wifi_portal/app.py
WorkingDirectory=/opt/wifi_portal
Restart=always
User=root
[Install]
WantedBy=multi-user.target
EOF
systemctl enable wifi-portal.service
echo "Web portal created and enabled."
echo ""

echo "--- 10. Enabling all services ---"
systemctl enable create_ap_interface.service
systemctl enable avahi-daemon.service
echo "All services enabled."
echo ""

echo "================================================="
echo "  Setup is complete!"
echo "  The system will now reboot to apply all changes."
echo "================================================="

reboot