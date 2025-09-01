#!/bin/bash

# A script to diagnose STA (Wi-Fi client) connection issues
# after using the web portal.

echo "================================================="
echo "        STA Connection Diagnostic Report"
echo "================================================="
echo ""

echo "--- 1. Check wpa_supplicant Configuration File ---"
CONFIG_FILE="/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
if [ -f "$CONFIG_FILE" ]; then
    echo "   Configuration file found: $CONFIG_FILE"
    echo "   --- File Contents (password hidden) ---"
    grep -v "psk=" "$CONFIG_FILE" | sed 's/^/   /'
    echo "   ------------------------------------"
else
    echo "   ERROR: Configuration file $CONFIG_FILE not found!"
fi
echo ""

echo "--- 2. Status of wpa_supplicant@wlan0.service ---"
systemctl status wpa_supplicant@wlan0.service --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 3. Recent Logs for wpa_supplicant (Last 30 lines) ---"
echo "   (Look for messages like 'authentication failed', 'associated', 'CTRL-EVENT-CONNECTED')"
journalctl -u wpa_supplicant@wlan0.service -n 30 --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 4. wlan0 Interface Status ---"
wpa_cli -i wlan0 status | sed 's/^/   /'
echo ""
echo "--- 5. wlan0 IP Address ---"
ip -4 -br addr show wlan0 | sed 's/^/   /' || echo "   wlan0 has no IPv4 address."
echo ""


echo "================================================="
echo "  Diagnostic report complete."
echo "================================================="
