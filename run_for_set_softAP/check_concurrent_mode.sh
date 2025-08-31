#!/bin/bash

# check_concurrent_mode_status.sh
# Checks the status of the configuration set by setup_concurrent_mode.sh.

# check if running as root
if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root. Please use sudo." >&2
  exit 1
fi

# helper function to print service status
print_status() {
    local service=$1
    local expected=$2
    local status
    local enabled
    status=$(systemctl is-active "$service")
    enabled=$(systemctl is-enabled "$service" 2>/dev/null)

    printf "   - %-30s: [%-8s] [%-8s]\n" "$service" "$status" "$enabled"
}

echo "================================================="
echo "      Concurrent AP+STA Mode Status Check"
echo "================================================="
echo ""

echo "--- 1. Core Network Stack Status ---"
echo "   SERVICE                       STATE     ENABLED"
echo "   -------------------------------------------------"
print_status "systemd-networkd"
print_status "systemd-resolved"
print_status "NetworkManager"
print_status "dhcpcd"
echo ""

echo "--- 2. Hostname and Network Interfaces ---"
CURRENT_HOSTNAME=$(hostname)
echo "   - Current Hostname: ${CURRENT_HOSTNAME}"
echo "   - Expected AP SSID: ${CURRENT_HOSTNAME}"
echo ""
echo "   - Network Interfaces found:"
ip -br link | grep -E "lo|wlan0|uap0" | sed 's/^/     /'
echo ""

echo "--- 3. IP Address Allocation ---"
echo "   - IP addresses for wlan0 (STA mode):"
ip -4 -br addr show wlan0 | sed 's/^/     /' || echo "     wlan0 has no IPv4 address."
echo "   - IP addresses for uap0 (AP mode):"
ip -4 -br addr show uap0 | sed 's/^/     /' || echo "     uap0 has no IPv4 address."
echo ""

echo "--- 4. Application Services Status ---"
echo "   SERVICE                       STATE     ENABLED"
echo "   -------------------------------------------------"
print_status "create_ap_interface.service"
print_status "hostapd.service"
print_status "dnsmasq.service"
print_status "wpa_supplicant@wlan0.service"
print_status "wifi-portal.service"
print_status "avahi-daemon.service"
echo ""

echo "--- 5. STA (wlan0) Connection Details ---"
if systemctl is-active --quiet wpa_supplicant@wlan0.service; then
    echo "   wpa_supplicant is active. Getting status..."
    wpa_cli -i wlan0 status | sed 's/^/     /'
else
    echo "   wpa_supplicant is not active. STA connection is down."
fi
echo ""

echo "================================================="
echo "  Check complete."
echo "================================================="
