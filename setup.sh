#!/bin/bash

# IEEE Presence Tracker - Automated Setup Script
# This script prepares a Raspberry Pi for running the presence tracker

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Track services that need to be restarted after installation
TRACKED_SERVICES=(
    "presence-tracker.service"
    "web-dashboard.service"
    "bluetooth-agent.service"
    "bluetooth-discoverable.service"
)

# Array to store which services were running before installation
SERVICES_TO_RESTART=()

# Check and track active services before installation
log_info "Checking current service status..."
for service in "${TRACKED_SERVICES[@]}"; do
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        SERVICES_TO_RESTART+=("$service")
        log_info "Service $service is currently active - will be restarted after installation"
    else
        log_info "Service $service is not currently active - will not be started automatically"
    fi
done

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    log_error "Please run this script as a regular user, not as root."
    exit 1
fi

log_info "Starting IEEE Presence Tracker setup..."

# Update package lists
log_info "Updating package lists..."
sudo apt update

# Upgrade installed packages
log_info "Upgrading installed packages..."
sudo apt upgrade -y

# Install BlueZ and Bluetooth tools
log_info "Installing BlueZ and Bluetooth tools..."
sudo apt install -y bluez bluez-tools bluetooth python3-dev libbluetooth-dev libcairo2-dev libgirepository1.0-dev gir1.2-gtk-3.0 build-essential pkg-config

# Disable exit on error for Bluetooth handling (we warn instead)
set +e

# Enable Bluetooth service
log_info "Enabling and starting Bluetooth service..."
sudo systemctl enable bluetooth 2>/dev/null

# Start bluetooth service with retry logic
max_retries=3
retry_count=0
bluetooth_started=false
while [ $retry_count -lt $max_retries ]; do
    sudo systemctl start bluetooth 2>/dev/null
    sleep 2
    if sudo systemctl is-active --quiet bluetooth 2>/dev/null; then
        bluetooth_started=true
        break
    fi
    retry_count=$((retry_count + 1))
    if [ $retry_count -lt $max_retries ]; then
        log_warn "Bluetooth service start attempt $retry_count failed, retrying..."
        sleep 2
    fi
done

if [ "$bluetooth_started" = true ]; then
    log_info "Bluetooth service started successfully"
    # Give the bluetooth daemon time to initialize and detect adapters
    log_info "Waiting for bluetooth daemon to fully initialize..."
    sleep 3
else
    log_warn "Could not start bluetooth service after $max_retries attempts"
fi

# Verify bluetooth service status
if sudo systemctl is-enabled --quiet bluetooth 2>/dev/null; then
    log_info "Bluetooth service is enabled"
else
    log_warn "Bluetooth service is not enabled"
fi

# Robust Bluetooth adapter detection with multiple fallback methods
bluetooth_detected=false
detection_method=""

# Method 1: Check rfkill list
log_info "Checking for Bluetooth adapter (rfkill)..."
if command -v rfkill &> /dev/null; then
    rfkill_output=$(rfkill list bluetooth 2>/dev/null || true)
    if echo "$rfkill_output" | grep -q "Bluetooth"; then
        bluetooth_detected=true
        detection_method="rfkill"
        log_info "Bluetooth adapter detected via rfkill"
        if echo "$rfkill_output" | grep -q "Soft blocked: yes"; then
            log_warn "Bluetooth is soft blocked. Try: sudo rfkill unblock bluetooth"
        fi
        if echo "$rfkill_output" | grep -q "Hard blocked: yes"; then
            log_warn "Bluetooth is hard blocked (hardware switch)"
        fi
    fi
fi

# Method 2: Check /sys/class/bluetooth/ directory
if [ "$bluetooth_detected" = false ]; then
    log_info "Checking /sys/class/bluetooth/..."
    if [ -d "/sys/class/bluetooth" ] && [ "$(ls -A /sys/class/bluetooth 2>/dev/null)" ]; then
        bluetooth_detected=true
        detection_method="sysfs"
        hci_device=$(ls /sys/class/bluetooth 2>/dev/null | head -1)
        log_info "Bluetooth adapter detected via /sys/class/bluetooth/ (found: ${hci_device})"
    fi
fi

# Method 3: Check hciconfig
if [ "$bluetooth_detected" = false ]; then
    log_info "Checking hciconfig..."
    if command -v hciconfig &> /dev/null; then
        if hciconfig -a 2>/dev/null | grep -q "hci"; then
            bluetooth_detected=true
            detection_method="hciconfig"
            log_info "Bluetooth adapter detected via hciconfig"
        fi
    fi
fi

# Method 4: Check lsusb for USB Bluetooth dongles
if [ "$bluetooth_detected" = false ]; then
    log_info "Checking lsusb for Bluetooth dongles..."
    if command -v lsusb &> /dev/null; then
        if lsusb 2>/dev/null | grep -iE "bluetooth|wireless controller" &> /dev/null; then
            bluetooth_detected=true
            detection_method="lsusb"
            log_info "USB Bluetooth adapter detected via lsusb"
        fi
    fi
fi

# Method 5: Check bluetoothctl
if [ "$bluetooth_detected" = false ]; then
    log_info "Checking bluetoothctl..."
    if command -v bluetoothctl &> /dev/null; then
        if timeout 5 bluetoothctl list 2>/dev/null | grep -q "Controller"; then
            bluetooth_detected=true
            detection_method="bluetoothctl"
            log_info "Bluetooth adapter detected via bluetoothctl"
        fi
    fi
fi

# Final determination
if [ "$bluetooth_detected" = true ]; then
    log_info "Bluetooth adapter successfully detected (via ${detection_method})"
else
    log_warn "Bluetooth adapter not detected automatically"
    log_warn "This may be normal if:"
    log_warn "  - Bluetooth is disabled in system settings"
    log_warn "  - A USB Bluetooth dongle is not yet plugged in"
    log_warn "  - Raspberry Pi model does not have built-in Bluetooth"
    log_warn ""
    log_warn "Manual verification steps:"
    log_warn "  1. Check if bluetooth service is running: sudo systemctl status bluetooth"
    log_warn "  2. Try starting bluetooth: sudo systemctl start bluetooth"
    log_warn "  3. Check adapters: sudo rfkill list bluetooth"
    log_warn "  4. Check sysfs: ls /sys/class/bluetooth/"
    log_warn "  5. Check with bluetoothctl: bluetoothctl list"
    log_warn ""
    log_warn "If you have a Bluetooth adapter, you can continue the setup and configure it later."
    log_warn "If you don't have Bluetooth, the presence tracker will not function."
fi

# Re-enable exit on error for the rest of the script
set -e

# Configure Bluetooth for discoverable mode (if adapter detected)
if [ "$bluetooth_detected" = true ]; then
    log_info "Configuring Bluetooth for discoverable mode..."
    if [ -f "make_discoverable.sh" ]; then
        chmod +x make_discoverable.sh
        # Disable exit on error temporarily to handle Bluetooth setup failures gracefully
        set +e
        ./make_discoverable.sh
        discoverable_exit_code=$?
        set -e
        
        if [ $discoverable_exit_code -eq 0 ]; then
            log_info "Bluetooth configured as discoverable and pairable"
        else
            log_warn "Bluetooth discoverable configuration failed (exit code: $discoverable_exit_code)"
            log_warn "You can configure Bluetooth manually later by running: ./make_discoverable.sh"
            log_warn "Continuing with the rest of the setup..."
        fi
    else
        log_warn "make_discoverable.sh not found. Skipping Bluetooth discoverable configuration."
    fi

    # Set persistent Bluetooth name via /etc/machine-info
    # This ensures the name survives reboots (used by BlueZ hostname plugin)
    log_info "Setting persistent Bluetooth name..."
    echo "PRETTY_HOSTNAME=IEEE Presence Tracker" | sudo tee /etc/machine-info > /dev/null
    log_info "Persistent Bluetooth name configured"
    
    # Install bluetooth-discoverable service for persistent discovery
    if [ -f "bluetooth-discoverable.service" ]; then
        log_info "Installing bluetooth-discoverable service..."
        sudo cp bluetooth-discoverable.service /etc/systemd/system/bluetooth-discoverable.service
        sudo systemctl daemon-reload
        sudo systemctl enable bluetooth-discoverable.service
    else
        log_warn "bluetooth-discoverable.service not found. Skipping service installation."
    fi
else
    log_warn "Skipping Bluetooth discoverable configuration - no adapter detected"
    log_warn "You can run ./make_discoverable.sh manually after connecting a Bluetooth adapter"
fi

# Add user to bluetooth group
if groups $USER | grep -q '\bbluetooth\b'; then
    log_info "User $USER is already in the bluetooth group"
else
    log_info "Adding user $USER to bluetooth group..."
    sudo usermod -a -G bluetooth $USER
    log_warn "You will need to log out and log back in for group changes to take effect"
fi

# Install bun (JavaScript/Node.js package manager)
if ! command -v bun &> /dev/null; then
    log_info "Installing bun package manager..."
    curl -fsSL https://bun.sh/install | bash
    # Add bun to PATH for current session
    export BUN_INSTALL="$HOME/.bun"
    export PATH="$BUN_INSTALL/bin:$PATH"
    log_info "Bun installed successfully"
else
    log_info "Bun is already installed (version: $(bun --version))"
fi

# Install UV package manager (Python dependencies)
if ! command -v uv &> /dev/null; then
    log_info "Installing UV package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source $HOME/.local/bin/env
    log_info "UV installed successfully"
else
    log_info "UV is already installed (version: $(uv --version))"
fi

# Install Python dependencies
if [ -f "pyproject.toml" ]; then
    log_info "Installing Python dependencies with UV..."
    uv sync
    log_info "Dependencies installed successfully"
else
    log_error "pyproject.toml not found. Please run this script from the project directory."
    exit 1
fi

# Install JavaScript/Node dependencies with bun
if [ -f "package.json" ]; then
    log_info "Installing JavaScript dependencies with bun..."
    bun install
    log_info "JavaScript dependencies installed successfully"
else
    log_info "No package.json found. Skipping JavaScript dependency installation."
fi

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        log_info "Creating .env file from .env.example..."
        cp .env.example .env
        log_warn "Please edit .env and add your CONVEX_DEPLOYMENT_URL"
    else
        log_error ".env.example not found. Please create .env file manually."
        exit 1
    fi
else
    log_info ".env file already exists"
fi

# Install systemd services
services_installed=false

# Install presence-tracker service
if [ -f "presence-tracker.service" ]; then
    log_info "Installing presence-tracker service..."
    sudo cp presence-tracker.service /etc/systemd/system/presence-tracker.service
    sudo systemctl daemon-reload
    sudo systemctl enable presence-tracker.service
    services_installed=true
else
    log_warn "presence-tracker.service not found. Skipping service installation."
fi

# Install web-dashboard service
if [ -f "web-dashboard.service" ]; then
    log_info "Installing web-dashboard service..."
    sudo cp web-dashboard.service /etc/systemd/system/web-dashboard.service
    sudo systemctl daemon-reload
    sudo systemctl enable web-dashboard.service
    services_installed=true
else
    log_warn "web-dashboard.service not found. Skipping web dashboard installation."
fi

# Install bluetooth-agent service (for automatic pairing acceptance)
if [ -f "bluetooth-agent.service" ]; then
    log_info "Installing bluetooth-agent service..."
    sudo cp bluetooth-agent.service /etc/systemd/system/bluetooth-agent.service
    sudo systemctl daemon-reload
    sudo systemctl enable bluetooth-agent.service
    services_installed=true
else
    log_warn "bluetooth-agent.service not found. Skipping Bluetooth agent installation."
fi

# Reload systemd daemon if any services were installed
if [ "$services_installed" = true ]; then
    log_info "Reloading systemd daemon..."
    sudo systemctl daemon-reload
fi

# Restart only services that were active before installation
if [ ${#SERVICES_TO_RESTART[@]} -gt 0 ]; then
    log_info ""
    log_info "Restarting services that were active before installation..."
    
    for service in "${SERVICES_TO_RESTART[@]}"; do
        log_info "Restarting $service..."
        set +e  # Don't exit if restart fails
        sudo systemctl restart "$service" 2>/dev/null
        restart_status=$?
        set -e
        
        if [ $restart_status -eq 0 ]; then
            log_info "Successfully restarted $service"
        else
            log_warn "Failed to restart $service (this may be normal on first run)"
        fi
    done
    
    log_info "Service restarts completed"
else
    log_info ""
    log_info "No services were active before installation - services will not be started automatically"
    log_info "You can start services manually with:"
    log_info "  sudo systemctl start presence-tracker.service"
    log_info "  sudo systemctl start web-dashboard.service"
    log_info "  sudo systemctl start bluetooth-agent.service"
fi

# Summary
log_info ""
log_info "=========================================="
log_info "Setup completed successfully!"
log_info "=========================================="
log_info ""
log_info "Installed components:"
log_info "  - UV package manager (Python dependencies)"
log_info "  - Bun package manager (JavaScript/Node dependencies)"
log_info "  - BlueZ and Bluetooth tools"
log_info "  - Systemd services (presence-tracker, web-dashboard, bluetooth-agent)"
log_info ""
log_info "Next steps:"
log_info "1. Edit .env and add your CONVEX_DEPLOYMENT_URL"
log_info "2. Access the web dashboard at http://$(hostname).local:5000 (or http://<pi-ip>:5000)"
log_info "3. Use the dashboard to:"
log_info "   - Scan for discoverable Bluetooth devices"
log_info "   - View currently connected devices"
log_info "   - Register devices with user names"
log_info "   - Monitor registered device status"
log_info "4. Services that were running before installation have been restarted"
log_info "5. Start services manually (if needed):"
log_info "   - sudo systemctl start presence-tracker.service"
log_info "   - sudo systemctl start web-dashboard.service"
log_info "   - sudo systemctl start bluetooth-agent.service"
log_info "6. Monitor logs:"
log_info "   - sudo journalctl -u presence-tracker -f"
log_info "   - sudo journalctl -u web-dashboard -f"
log_info "   - sudo journalctl -u bluetooth-agent -f"
log_info ""
log_info "Note: This script is idempotent and can be run multiple times safely."
log_info ""
log_info "For full deployment instructions, see DEPLOYMENT.md"
log_info "For Bluetooth pairing guide, see PAIRING.md"
log_info ""
