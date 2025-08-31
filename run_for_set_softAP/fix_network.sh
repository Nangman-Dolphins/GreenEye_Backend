#!/bin/bash

# A script to fix the port 53 conflict and the 'device busy' error.
# Version 3: Adds more robust service restart logic with delays to prevent race conditions.

# check if running as root
if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root. Please use sudo." >&2
  exit 1
fi

echo "--- 1. Resolving Port 53 Conflict ---"

# check if DNSStubListener is already configured
if grep -q "^DNSStubListener=" /etc/systemd/resolved.conf; then
    sed -i 's/^DNSStubListener=.*/DNSStubListener=no/' /etc/systemd/resolved.conf
    echo "   Updated existing DNSStubListener setting."
else
    # if it doesn't exist, add it
    echo "DNSStubListener=no" >> /etc/systemd/resolved.conf
    echo "   Added DNSStubListener=no to configuration."
fi
echo ""

echo "--- 2. Resetting and Restarting Services in Correct Order ---"

echo "   - Stopping all related services to free wlan0..."
# stop all services that might use the wlan0 interface
systemctl stop wpa_supplicant@wlan0.service
systemctl stop hostapd.service
systemctl stop dnsmasq.service
systemctl stop create_ap_interface.service
sleep 1

echo "   - Cleaning up virtual interface..."
# explicitly delete the virtual interface in case it's in a bad state
iw dev uap0 del 2>/dev/null || true
sleep 1

# ensure the interface is down before bringing it up
echo "   - Resetting wlan0 interface..."
ip link set wlan0 down
sleep 2
ip link set wlan0 up
sleep 2

echo "   - Restarting core network services..."
systemctl restart systemd-resolved
systemctl restart systemd-networkd
# wait for the core network manager to settle before proceeding
echo "   - Waiting for networkd to settle..."
sleep 3

echo "   - Restarting AP and STA services one by one..."
# now that wlan0 is free and networkd is ready, this should succeed
systemctl restart create_ap_interface.service
sleep 1
# start the supplicant for STA mode
systemctl restart wpa_supplicant@wlan0.service
sleep 1
# start hostapd for AP mode
systemctl restart hostapd.service
sleep 1
# start dnsmasq for DHCP on the AP
systemctl restart dnsmasq.service

echo ""
echo "================================================="
echo "  Fix applied successfully."
echo "  Please run the status check script again to verify."
echo "  A reboot is recommended for full stability."
echo "================================================="

