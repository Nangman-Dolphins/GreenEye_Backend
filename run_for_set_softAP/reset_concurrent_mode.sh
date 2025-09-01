#!/bin/bash

# reset_concurrent_mode.sh
# reverts all changes made by the setup_concurrent_mode.sh script
# and restores the system to use dhcpcd for network management.

# check if running as root
if [ "$(id -u)" -ne 0 ]; then
  echo "This script must be run as root. Please use sudo." >&2
  exit 1
fi

echo "--- 1. Stopping and disabling concurrent mode services ---"
# stop and disable all services that were enabled
systemctl stop hostapd dnsmasq wpa_supplicant@wlan0.service create_ap_interface.service wifi-portal.service systemd-networkd systemd-resolved 2>/dev/null
systemctl disable hostapd dnsmasq wpa_supplicant@wlan0.service create_ap_interface.service wifi-portal.service systemd-networkd systemd-resolved 2>/dev/null
echo "Related services have been stopped and disabled."
echo ""

echo "--- 2. Deleting created configuration files and scripts ---"
# remove configuration files
rm -f /etc/hostapd/hostapd.conf
rm -f /etc/dnsmasq.conf
rm -f /etc/wpa_supplicant/wpa_supplicant-wlan0.conf
rm -f /etc/systemd/network/10-wlan0.network
rm -f /etc/systemd/network/20-uap0.network
# remove service files
rm -f /etc/systemd/system/create_ap_interface.service
rm -f /etc/systemd/system/wifi-portal.service
# remove web app directory
rm -rf /opt/wifi_portal
echo "All created configuration files have been deleted."
echo ""

echo "--- 3. Restoring system files to default ---"
# revert /etc/default/hostapd to its default state
if [ -f /etc/default/hostapd ]; then
    sed -i 's|^DAEMON_CONF=.*|#DAEMON_CONF=""|g' /etc/default/hostapd
fi
# revert hostname to 'raspberrypi'
DEFAULT_HOSTNAME="raspberrypi"
hostnamectl set-hostname ${DEFAULT_HOSTNAME}
sed -i "s/127.0.1.1.*/127.0.1.1\t${DEFAULT_HOSTNAME}/g" /etc/hosts
# remove the symlink for resolv.conf, dhcpcd will recreate it
rm -f /etc/resolv.conf
echo "System files have been reverted to default."
echo ""

echo "--- 4. Re-enabling the default network manager (dhcpcd) ---"
# re-enable dhcpcd for default network management
systemctl unmask dhcpcd 2>/dev/null
systemctl enable dhcpcd
systemctl start dhcpcd
echo "The default network manager (dhcpcd) has been re-enabled."
echo ""

echo "--- 5. Cleanup and reboot notice ---"
systemctl daemon-reload
echo "Systemd daemon has been reloaded."
echo ""

echo "================================================="
echo "  Concurrent Mode configuration reset is complete."
echo "  A reboot is required to apply all changes."
echo ""
echo "  sudo reboot"
echo "================================================="
echo "Note: Installed packages (hostapd, dnsmasq, etc.) were not removed."
echo "      You can remove them manually with 'sudo apt-get purge hostapd dnsmasq'."

exit 0

