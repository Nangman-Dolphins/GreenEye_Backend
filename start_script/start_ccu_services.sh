#!/bin/bash

echo "=============================================================="
echo "  ____                     _____              ____ ____ _   _ "
echo " / ___|_ __ ___  ___ _ __ | ____|   _  ___   / ___/ ___| | | |"
echo "| |  _| '__/ _ \/ _ \ '_ \|  _|| | | |/ _ \ | |  | |   | | | |"
echo "| |_| | | |  __/  __/ | | | |__| |_| |  __/ | |__| |___| |_| |"
echo " \____|_|  \___|\___|_| |_|_____\__, |\___|  \____\____|\___/ "
echo "                                |___/ "
echo "=============================================================="

# move to project dir
echo "move to target dir"
cd /home/admin/GreenEye_Backend

# de-recompose containers
echo "Reset all GreenEye CCU system services"
docker compose down
echo "Starting all GreenEye CCU system services"
docker compose up -d

echo "!!!!!DONE!!!!!"
read