#!/bin/bash

# A script to diagnose Avahi and .local DNS setup.

echo "================================================="
echo "      Avahi (.local DNS) Diagnostic Report"
echo "================================================="
echo ""

echo "--- 1. Status of Avahi Daemon ---"
# Check if the avahi-daemon service is active and running.
systemctl status avahi-daemon.service --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 2. Checking /etc/hosts Configuration ---"
# The /etc/hosts file should contain both the primary and alias hostnames.
echo "   The following line should contain two hostnames (e.g., ge-ccu-xxxx and ge-ccu-xxxx-dashboard)"
grep "127.0.1.1" /etc/hosts | sed 's/^/   /'
echo "-------------------------------------------------"
echo ""

echo "--- 3. Testing Local Resolution ---"
# Get the dashboard hostname from the system's current hostname.
HOSTNAME=$(hostname)
DASHBOARD_HOSTNAME="${HOSTNAME}-dashboard.local"
echo "   Attempting to resolve '${DASHBOARD_HOSTNAME}' on this device..."
# Use avahi-resolve to test if the name is resolvable locally.
RESOLVED_IP=$(avahi-resolve --name "${DASHBOARD_HOSTNAME}" | awk '{print $2}')

if [ -n "${RESOLVED_IP}" ]; then
    echo "   SUCCESS: Resolved to ${RESOLVED_IP}"
else
    echo "   FAILURE: Could not resolve the hostname locally. Avahi might not be broadcasting it."
fi
echo "-------------------------------------------------"
echo ""

echo "================================================="
echo "  Diagnostic report complete."
echo "================================================="