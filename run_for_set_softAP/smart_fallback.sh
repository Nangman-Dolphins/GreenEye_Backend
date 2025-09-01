#!/bin/bash

# Final Network Setup Script (Smart Fallback v1.0)
# This script configures a robust fallback mechanism, which is more stable
# than concurrent mode on some systems.
# It completely resets previous configurations before applying the new setup.

# stop on any error
set -e

# check if running as root
if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root. Please use sudo." >&2
  exit 1
fi

echo "--- 0. Full Reset of All Previous Configurations ---"
# stop and disable all services from previous attempts
systemctl stop hostapd dnsmasq systemd-networkd wpa_supplicant@wlan0.service create_ap_interface.service NetworkManager wifi-portal.service wifi-fallback.service 2>/dev/null || true
systemctl disable hostapd dnsmasq systemd-networkd wpa_supplicant@wlan0.service create_ap_interface.service NetworkManager wifi-portal.service wifi-fallback.service 2>/dev/null || true

# remove old config and service files
rm -f /etc/systemd/system/create_ap_interface.service
rm -f /etc/systemd/network/*.network
rm -f /etc/wpa_supplicant/wpa_supplicant-wlan0.conf
rm -f /etc/hostapd/hostapd.conf
rm -f /etc/dnsmasq.conf
rm -f /etc/avahi/services/dashboard.service
rm -f /etc/systemd/system/wifi-fallback.service
rm -f /usr/local/bin/wifi_fallback.sh

# restore dhcpcd as the primary network manager
systemctl unmask dhcpcd.service 2>/dev/null || true
systemctl enable dhcpcd.service 2>/dev/null || true

echo "Previous network configurations have been reset."
echo ""

echo "--- 1. Installing Necessary Packages ---"
apt-get update
apt-get install -y hostapd dnsmasq python3-flask libnss-mdns avahi-daemon
echo "Packages installed."
echo ""

echo "--- 2. Setting up Hostname and Avahi ---"
MAC_SUFFIX=$(cat /sys/class/net/wlan0/address | sed 's/://g' | cut -c 9-12)
HOSTNAME="ge-ccu-${MAC_SUFFIX}"
AP_PASSWORD="defaultPW"

hostnamectl set-hostname "${HOSTNAME}"
# robustly fix /etc/hosts
sed -i "/^127.0.1.1/d" /etc/hosts || true
echo "127.0.1.1	${HOSTNAME} ${HOSTNAME}-dashboard" >> /etc/hosts
# fix nsswitch.conf for .local resolution
sed -i '/^hosts:/d' /etc/nsswitch.conf
echo "hosts: files mdns4_minimal [NOTFOUND=return] dns mdns4" >> /etc/nsswitch.conf
systemctl restart avahi-daemon.service
echo "Hostname and Avahi configured."
echo ""

echo "--- 3. Configuring AP Mode Services (hostapd & dnsmasq) ---"
# configure hostapd
cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=${HOSTNAME}
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${AP_PASSWORD}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

# configure dnsmasq
cat > /etc/dnsmasq.conf <<EOF
interface=wlan0
dhcp-range=192.168.5.2,192.168.5.20,255.255.255.0,24h
domain=wlan
address=/#/192.168.5.1
EOF
echo "AP services configured but will not be enabled by default."
echo ""

echo "--- 4. Creating Web Portal ---"
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
            <button type="submit">저장 및 재부팅</button>
        </form>
    </div>
</body>
</html>
'''

HTML_TEMPLATE_SUCCESS = '''
<!DOCTYPE html>
<html>
<head><title>Wi-Fi 설정</title></head>
<body><h2>설정이 저장되었습니다!</h2><p>기기가 재부팅됩니다. 잠시 후 새로고침하여 연결 상태를 확인하세요.</p></body>
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
                if ssid: ssids.add(ssid)
        return sorted(list(ssids))
    except Exception: return []

@app.route("/")
def index():
    networks = get_wifi_ssids()
    return render_template_string(HTML_TEMPLATE_FORM, networks=networks)

@app.route("/save", methods=["POST"])
def save():
    ssid = request.form['ssid']
    password = request.form['password']
    try:
        network_block = subprocess.check_output(['wpa_passphrase', ssid, password]).decode('utf-8')
        config_content = f'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\nupdate_config=1\ncountry=US\n\n'
        full_config = config_content + network_block
        with open("/etc/wpa_supplicant/wpa_supplicant.conf", "w") as f:
            f.write(full_config)
        subprocess.run(['sudo', 'reboot'], check=True)
        return render_template_string(HTML_TEMPLATE_SUCCESS)
    except Exception:
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
echo "Web portal created."
echo ""

echo "--- 5. Creating Smart Fallback Controller ---"
cat > /usr/local/bin/wifi_fallback.sh << 'EOF'
#!/bin/bash
# This script checks for a wifi connection and starts the AP if not connected.
# Wait for 30 seconds for the network to settle
sleep 30

# Check if we have an IP address on wlan0
if ! ip -4 addr show wlan0 | grep -q "inet"; then
    echo "No WiFi connection found. Starting SoftAP..."
    # Take control of wlan0
    systemctl stop wpa_supplicant.service
    systemctl stop dhcpcd.service
    # Configure the AP interface
    ifconfig wlan0 192.168.5.1
    # Start AP services
    systemctl start dnsmasq.service
    systemctl start hostapd.service
    systemctl start wifi-portal.service
else
    echo "WiFi connection is active. Stopping AP services."
    # Ensure AP services are not running
    systemctl stop hostapd.service 2>/dev/null || true
    systemctl stop dnsmasq.service 2>/dev/null || true
    systemctl stop wifi-portal.service 2>/dev/null || true
fi
EOF

chmod +x /usr/local/bin/wifi_fallback.sh

cat > /etc/systemd/system/wifi-fallback.service << EOF
[Unit]
Description=Smart WiFi Fallback to AP Mode
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/wifi_fallback.sh
[Install]
WantedBy=multi-user.target
EOF

systemctl enable wifi-fallback.service
echo "Smart fallback service created and enabled."
echo ""

echo "================================================="
echo "  Setup is complete!"
echo "  The system will now reboot to apply all changes."
echo "================================================="

reboot
