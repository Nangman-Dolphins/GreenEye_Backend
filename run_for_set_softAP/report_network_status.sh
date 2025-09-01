#!/bin/bash

# A script to diagnose issues with the concurrent mode setup.
# It collects status and log information for critical services.

echo "================================================="
echo "      Concurrent Mode Diagnostic Report"
echo "================================================="
echo ""

echo "--- 1. Status of systemd-networkd ---"
systemctl status systemd-networkd.service --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 2. Status of create_ap_interface service ---"
systemctl status create_ap_interface.service --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 3. Status of dnsmasq service ---"
systemctl status dnsmasq.service --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 4. Recent logs for dnsmasq (Last 20 lines) ---"
journalctl -u dnsmasq.service -n 20 --no-pager
echo "-------------------------------------------------"
echo ""

echo "--- 5. Network interface configuration ---"
echo "ip link output:"
ip -br link
echo ""
echo "ip addr output:"
ip -br addr
echo "-------------------------------------------------"
echo ""

echo "================================================="
echo "  Diagnostic report complete."
echo "================================================="
