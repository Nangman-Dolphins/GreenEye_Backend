#!/bin/bash

# --- configuration ---
# set the user, default is 'pi'
USERNAME="admin"

# set the full path to the user's home directory
USER_HOME="/home/$USERNAME"

# set the full path to your *existing* shell script
SH_SCRIPT_PATH="$USER_HOME/GreenEye_Backend/start_script/start_ccu_services.sh"

# --- do not edit below this line ---

# define path for the new .desktop file
AUTOSTART_DIR="$USER_HOME/.config/autostart"
DESKTOP_FILE_PATH="$AUTOSTART_DIR/greeneye.desktop"

echo "starting the GreenEye autostart setup script (no sudo)"
echo "    (target script: $SH_SCRIPT_PATH)"
echo "-------------------------------------------------"

# step 1: ensure the target .sh script is executable
echo "1. setting executable permission for target script ($SH_SCRIPT_PATH)..."
if [ -f "$SH_SCRIPT_PATH" ]; then
    # grant execute permission
    chmod +x "$SH_SCRIPT_PATH"
    echo "   ...permission granted."
else
    # error handling if script not found
    echo "   ...WARNING: '$SH_SCRIPT_PATH' file not found!"
    echo "       please correct the SH_SCRIPT_PATH variable at the top of the script and run again."
    exit 1
fi

# step 2: create the autostart directory if it doesn't exist
echo "2. ensuring autostart directory exists ($AUTOSTART_DIR)..."
mkdir -p "$AUTOSTART_DIR"
echo "   ...directory ready."

# step 3: create the .desktop file using the correct 'lxterminal'
echo "3. creating '$DESKTOP_FILE_PATH' file..."

# use 'cat' and 'EOF' (Here Document) to write the file content
cat > "$DESKTOP_FILE_PATH" << EOF
[Desktop Entry]
Name=Start GreenEye
Comment=Automatically starts the GreenEye containers
Exec=lxterminal -e "$SH_SCRIPT_PATH"
Type=Application
EOF

echo "   ....desktop file created (using lxterminal)."

# make the .desktop file "trusted" by making it executable
echo "4. setting executable permission for .desktop file (to 'trust' it)..."
# this fixes the 'bad name' or 'untrusted' error
chmod +x "$DESKTOP_FILE_PATH"
echo "   ...permission granted."

echo "all settings are complete!"