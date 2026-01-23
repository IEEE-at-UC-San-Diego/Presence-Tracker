# IEEE Presence Tracker

Bluetooth-based presence detection system using Convex backend, designed for Raspberry Pi.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Convex](https://img.shields.io/badge/Convex-1.31.5-purple.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## Quick Links

- [Quick Start](#quick-start) - Get started in minutes
- [Architecture](#architecture) - System overview and data flow
- [Troubleshooting](#troubleshooting) - Common issues and solutions

## Overview

This system monitors Bluetooth device presence and updates a Convex backend database. It's designed to track when devices (phones, etc.) are connected/disconnected from a Raspberry Pi via Bluetooth, without requiring any mobile app installation. The system includes a web dashboard for monitoring device status and supporting Slack/Discord integrations for real-time notifications.

## Features

- **Bluetooth Presence Tracking** - Polls device status every 60 seconds
- **Cross-Platform Support** - Works with iOS (iPhone) and Android devices
- **No Mobile App Required** - Uses native Bluetooth pairing
- **Web Dashboard** - Real-time monitoring and device management interface
- **Discord Integration** - Send presence updates to Discord channels
- **Slack Integration** - Send presence updates to Slack channels
- **Device Registration Workflow** - Easy onboarding of new devices
- **Attendance Logging** - Track device presence over time
- **Grace Period Handling** - Automatic cleanup of unregistered devices
- **Comprehensive Logging** - File and console logging with error handling

## Deployment

This system is designed primarily for **Raspberry Pi deployment** with Bluetooth hardware access. The web dashboard container can run on any host (Pi or local machine) and connects to the Convex backend.

- **Presence Tracker** (`presence_tracker.py`) - Must run on Raspberry Pi with Bluetooth hardware
- **Web Dashboard** (`docker-compose.yml`) - Can run on Raspberry Pi or any machine with Docker
- **Convex Backend** - Cloud-hosted serverless database and functions

## Prerequisites

### Hardware
- **Raspberry Pi** (recommended: 4 or 5, minimum: 3B+) with built-in Bluetooth or USB Bluetooth dongle
- Stable internet connection (required for Convex API)

### Software (Raspberry Pi)
- **Python 3.10+** - Required for the tracker scripts
- **UV Package Manager** - Fast Python package installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Bun Runtime** - JavaScript runtime for Convex CLI (`curl -fsSL https://bun.sh/install | bash`)
- **BlueZ & Bluetooth Tools** - Bluetooth stack (`sudo apt install bluez bluez-tools bluetooth`)
- **Docker** (optional, for web dashboard): `curl -fsSL https://get.docker.com | sh`

### Software (Local Development)
- Python 3.10+ with UV
- Bun runtime
- Docker for web dashboard

### Accounts
- **Convex Account** - Free account at [convex.dev](https://convex.dev) (required for backend)
- **Discord** (optional) - For Discord integration notifications
- **Slack** (optional) - For Slack integration notifications

## Quick Start

### Automated Setup (Recommended)

Run the automated setup script on your Raspberry Pi:

```bash
cd "/home/ieee/Desktop/IEEE Presence Tracker"
./setup.sh
```

The script will display an interactive menu with options:
1. **Full Install** - Complete installation and configuration
2. **Update Config** - Update Bluetooth name and configuration
3. **Resetup/Redeploy Convex** - Trigger Convex re-deployment
4. **Restart Services** - Restart systemd services
5. **Make Bluetooth Discoverable** - Configure Bluetooth for discoverable mode

The script handles:
- Installing UV and Bun package managers
- Installing BlueZ and Bluetooth tools
- Installing Python and JavaScript dependencies
- Configuring Bluetooth permissions and discoverability
- Installing and configuring systemd services
- Deploying to Convex backend

### Manual Setup

#### Step 1: Install Package Managers

```bash
# Install UV (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Bun (JavaScript runtime)
curl -fsSL https://bun.sh/install | bash
```

#### Step 2: Install Bluetooth Tools

```bash
sudo apt update
sudo apt install bluez bluez-tools bluetooth -y
```

#### Step 3: Install Dependencies

```bash
# Python dependencies
uv sync

# JavaScript dependencies
bun install
```

#### Step 4: Deploy to Convex

You need a Convex deployment. Create one at [convex.dev](https://convex.dev) or run:

```bash
bunx convex dev
# Follow the prompts to create or select a deployment
```

Then deploy:

```bash
bunx convex deploy
```

This will output your `CONVEX_DEPLOYMENT_URL`.

#### Step 5: Configure Environment

Create `.env` file from the example:

```bash
cp .env.example .env
nano .env  # Add your CONVEX_DEPLOYMENT_URL
```

#### Step 6: Pair Bluetooth Devices

Make the Pi discoverable and pair your devices:

```bash
# Using the setup script (option 5)
./setup.sh
# Select option 5) Make Bluetooth Discoverable

# Or manually
sudo bluetoothctl
power on
agent on
default-agent
scan on
# Find your device's MAC address, then:
pair XX:XX:XX:XX:XX:XX
trust XX:XX:XX:XX:XX:XX
exit
```

Your Pi will appear as **"IEEE Knock Knock"** in Bluetooth scans.

#### Step 7: Register Devices in Convex

Register each paired device:

```bash
bunx convex run upsertDevice --json '{"macAddress":"AA:BB:CC:DD:EE:FF","name":"John Doe","status":"absent"}'
```

#### Step 8: Run the Tracker

```bash
# Run manually to test
uv run src/presence_tracker.py

# Run as systemd service for automatic startup
sudo cp presence-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable presence-tracker.service
sudo systemctl start presence-tracker.service
```

### Web Dashboard Setup

Run the web dashboard (can be on Pi or local machine):

```bash
# Set your Convex URL in .env
echo "CONVEX_DEPLOYMENT_URL=https://your-deployment.convex.cloud" >> .env

# Run with Docker
docker-compose up -d

# Access at http://localhost:3000 (or http://<pi-ip>:3000)
```

## Environment Configuration

Create a `.env` file by copying the example:

```bash
cp .env.example .env
```

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `CONVEX_DEPLOYMENT_URL` | Your Convex backend deployment URL (required) | `https://chatty-akita-508.convex.cloud` |
| `ORGANIZATION_NAME` | Display name for your organization | `IEEE` |

### Presence Tracking Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `PRESENT_TTL_SECONDS` | How long a device is considered present after last seen (seconds) | `120` |
| `NEWLY_REGISTERED_GRACE_PERIOD` | Grace period for newly registered devices to enter polling cycle (seconds) | `120` |
| `GRACE_PERIOD_SECONDS` | Time new devices have to be registered before auto-deletion (seconds) | `300` |

### Probing Strategy

| Variable | Description | Default |
|----------|-------------|---------|
| `FULL_PROBE_ENABLED` | Enable full probing (attempt connect+disconnect for each device) | `true` |
| `FULL_PROBE_INTERVAL_SECONDS` | Time between full probe cycles (seconds) | `60` |
| `FULL_PROBE_DISCONNECT_AFTER` | disconnect after successful probe | `true` |
| `REQUIRE_PRESENCE_SIGNAL_FOR_ABSENCE` | Only mark absent if presence signal was detected in a cycle | `true` |
| `IN_RANGE_SCAN_SECONDS` | Scan duration to refresh RSSI for in-range detection | `5` |
| `DISCONNECT_AFTER_SUCCESS` | Disconnect after successful auto-reconnect to free connection slots | `true` |
| `DISCONNECT_CONNECTED_AFTER_CYCLE` | Disconnect connected devices after each cycle to avoid limits | `true` |
| `CONNECT_TIMEOUT_SECONDS` | Max wait time for bluetoothctl connect attempts (seconds) | `10` |
| `MAX_RECONNECT_PER_CYCLE` | Maximum reconnect attempts per cycle (0 = no limit) | `4` |
| `ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN` | Probe paired devices when scan finds nothing in range | `true` |

### Example .env File

```bash
# Convex Deployment URL (required)
CONVEX_DEPLOYMENT_URL=https://your-convex-deployment.convex.cloud

# Organization Name
ORGANIZATION_NAME=IEEE

# Presence Tracking
PRESENT_TTL_SECONDS=120
NEWLY_REGISTERED_GRACE_PERIOD=120
GRACE_PERIOD_SECONDS=300

# Probing Strategy
FULL_PROBE_ENABLED=true
FULL_PROBE_INTERVAL_SECONDS=60
FULL_PROBE_DISCONNECT_AFTER=true
REQUIRE_PRESENCE_SIGNAL_FOR_ABSENCE=true
IN_RANGE_SCAN_SECONDS=5

# Connection Management
DISCONNECT_AFTER_SUCCESS=true
DISCONNECT_CONNECTED_AFTER_CYCLE=true
CONNECT_TIMEOUT_SECONDS=10
MAX_RECONNECT_PER_CYCLE=4
ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN=true
```

## Usage Instructions

### Running the Tracker

```bash
# Run manually (for testing)
uv run src/presence_tracker.py

# View live logs
tail -f logs/presence_tracker.log

# Run via systemd service (production)
sudo systemctl status presence-tracker.service
sudo systemctl restart presence-tracker.service
```

### Convex Operations

```bash
# Start Convex development server
npm run dev

# Deploy to Convex
npm run deploy

# List all devices
bunx convex run getDevices

# Register a new device
bunx convex run upsertDevice --json '{"macAddress":"AA:BB:CC:DD:EE:FF","name":"John Doe","status":"absent"}'

# Update device status
bunx convex run updateDeviceStatus --json '{"macAddress":"AA:BB:CC:DD:EE:FF","status":"present"}'

# List device logs
bunx convex run getDeviceLogs '{"deviceId":"j4k2l9..." }'
```

### Web Dashboard

```bash
# Start web dashboard (Docker)
docker-compose up -d

# View dashboard at http://localhost:3000

# Stop web dashboard
docker-compose down

# View dashboard logs
docker-compose logs -f web-dashboard
```

### Bluetooth Commands

```bash
# Check Bluetooth status
bluetoothctl show

# List paired devices
bluetoothctl paired-devices

# Check device connection
bluetoothctl info AA:BB:CC:DD:EE:FF

# Restart Bluetooth service
sudo systemctl restart bluetooth

# Scan for devices
bluetoothctl scan on
```

## Available Scripts

### npm Scripts

```bash
npm run dev      # Start Convex development server
npm run deploy   # Deploy to Convex cloud
```

### Python Scripts

```bash
uv run src/presence_tracker.py     # Main tracker (runs every 60 seconds)
uv run src/bluetooth_scanner.py    # Test Bluetooth scanning
uv run src/bluetooth_agent.py      # Bluetooth pairing agent
```

### Systemd Services

```bash
sudo systemctl start presence-tracker.service        # Start tracker service
sudo systemctl stop presence-tracker.service         # Stop tracker service
sudo systemctl restart presence-tracker.service      # Restart tracker service
sudo systemctl status presence-tracker.service       # Check service status
sudo systemctl enable presence-tracker.service       # Enable on boot
sudo journalctl -u presence-tracker.service -f       # View service logs

sudo systemctl start bluetooth-agent.service         # Start pairing agent
sudo systemctl start bluetooth-discoverable.service  # Start discoverable mode
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Raspberry Pi                           │
│  ┌──────────────────────────────────────────────────────┐  │
│  │         presence_tracker.py (Python)                 │  │
│  │  - Polls every 60 seconds                            │  │
│  │  - Checks Bluetooth connections                      │  │
│  │  - Syncs with Convex backend                         │  │
│  └──────────────────┬───────────────────────────────────┘  │
│                     │                                       │
│  ┌──────────────────▼───────────────────────────────────┐  │
│  │         bluetooth_scanner.py                         │  │
│  │  - Uses bluetoothctl to check device connections    │  │
│  └──────────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS API
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                      Convex Backend                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │         TypeScript Functions                        │  │
│  │  - getDevices() - Fetch all registered devices      │  │
│  │  - updateDeviceStatus() - Update device status       │  │
│  │  - registerDevice() - Add new device                │  │
│  └──────────────────┬───────────────────────────────────┘  │
│                     │                                       │
│  ┌──────────────────▼───────────────────────────────────┐  │
│  │         Database (Devices Table)                     │  │
│  │  - macAddress, name, status, lastSeen               │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Pairing Your Device

1. Open Bluetooth settings on your phone
2. Scan for devices
3. Find "IEEE Knock Knock" 
4. Tap to pair (no PIN required)

If the Pi is not appearing as discoverable, run:

```bash
./setup.sh
# Select option 5) Make Bluetooth Discoverable
```

## Troubleshooting

### Bluetooth Issues

#### Bluetooth Not Working
```bash
# Check Bluetooth service status
sudo systemctl status bluetooth

# Restart Bluetooth service
sudo systemctl restart bluetooth

# Check Bluetooth controller info
bluetoothctl show

# Check for hardware blocks
sudo rfkill list bluetooth
sudo rfkill unblock bluetooth
```

#### Device Not Detected
1. Ensure device is paired: `bluetoothctl paired-devices`
2. Verify MAC address matches exactly in Convex (case-sensitive)
3. Check device Bluetooth is enabled and in range
4. Some devices disconnect when locked (especially iOS)

#### Permission Denied
```bash
sudo usermod -a -G bluetooth $USER
# Log out and back in for changes to take effect
```

#### Pi Not Discoverable
```bash
# Re-run setup script discoverable option
./setup.sh
# Select option 5) Make Bluetooth Discoverable

# Manually configure
sudo bluetoothctl
power on
discoverable on
pairable on
agent on
default-agent
```

### Convex Issues

#### Connection Errors
- Verify `CONVEX_DEPLOYMENT_URL` in `.env` is correct
- Check internet connectivity
- Verify deployment exists at [convex.dev](https://convex.dev)

#### Deployment Problems
```bash
# Check Convex status
bunx convex dev

# Redeploy
npm run deploy
```

#### Device Not Registering
- Check MAC address format (should be `AA:BB:CC:DD:EE:FF`)
- Verify device is paired via `bluetoothctl paired-devices`
- Check Convex logs: `bunx convex logs`

### Docker / Web Dashboard Issues

#### Dashboard Not Accessible
```bash
# Check container status
docker-compose ps

# View logs
docker-compose logs web-dashboard

# Restart container
docker-compose restart

# Rebuild if needed
docker-compose up -d --build
```

#### Wrong Convex URL
Ensure `.env` has correct `CONVEX_DEPLOYMENT_URL` and restart container:
```bash
docker-compose down
# Edit .env with correct URL
docker-compose up -d
```

### Integration Issues

#### Discord Integration Not Working
- Verify webhook URL is correct in web dashboard
- Check Discord server has the webhook enabled
- View logs: `tail -f logs/presence_tracker.log`

#### Slack Integration Not Working
- Verify bot token and channel ID are correct
- Ensure bot has permission to post in the channel
- Test webhook:
```bash
curl -X POST -H 'Content-type: application/json' --data '{"text":"Test message"}' YOUR_WEBHOOK_URL
```

### Device Registration Issues

#### Newly Registered Device Not Updating Immediately

When you first register a device (transition from pending to registered), there is an automatic grace period (120 seconds by default) that ensures the device enters the polling cycle immediately. During this time:
- The device will be tracked for connect/disconnect management
- Presence status will update within the next polling cycle
- The grace period prevents race conditions between device registration and Bluetooth polling

Adjust this grace period by setting `NEWLY_REGISTERED_GRACE_PERIOD` in your `.env` file.

#### Pending Devices Not Auto-Deleted

Unregistered devices are automatically deleted after `GRACE_PERIOD_SECONDS` (default: 5 minutes). If not deleting:
- Check that the tracker is running: `sudo systemctl status presence-tracker.service`
- Verify grace period is set correctly in `.env`
- Manually fix: `bunx convex run fixPendingDevices`

### Platform-Specific Notes

#### iOS Devices
- iOS devices don't appear in Bluetooth scans when paired
- Tracker uses `bluetoothctl` to check connection status
- Keep iPhone unlocked and Bluetooth enabled
- Some iOS versions require active connection (not just paired)
- iOS may disconnect when device locks

#### Android Devices
- Android devices are more discoverable via Bluetooth scanning
- Connection checking works via `bluetoothctl`
- May disconnect when screen is off or in power saving mode
- Add the Pi to "Allowed devices" to prevent automatic disconnection

### Service Issues

#### Service Won't Start
```bash
# Check service logs
sudo journalctl -u presence-tracker.service -n 50

# Check for errors in logs
tail -f logs/presence_tracker.log

# Verify dependencies installed
uv sync

# Test manually
uv run src/presence_tracker.py
```

#### Service Crashes Automatically
- Check Python version: `python3 --version` (requires 3.10+)
- Verify UV is installed: `uv --version`
- Check Bluetooth hardware is accessible: `bluetoothctl show`
- Review logs for specific errors

### Performance Issues

#### Slow Device Detection
- Adjust `FULL_PROBE_INTERVAL_SECONDS` (default: 60)
- Reduce `IN_RANGE_SCAN_SECONDS` (default: 5)
- Check Bluetooth hardware issues with `hciconfig`
- Reduce number of tracked devices

#### High CPU Usage
- Decrease polling frequency
- Disable full probing if not needed: `FULL_PROBE_ENABLED=false`
- Check for Bluetooth adapter issues

## Project Structure

```
.
├── src/                              # Python source files
│   ├── presence_tracker.py           # Main presence tracking script
│   ├── bluetooth_scanner.py          # Bluetooth detection and connection management
│   └── bluetooth_agent.py            # Bluetooth pairing agent (no PIN required)
├── convex/                           # Convex backend
│   ├── schema.ts                     # Database schema (devices, logs, integrations)
│   ├── devices.ts                    # Device CRUD operations and queries
│   ├── integrations.ts               # Discord/Slack integration configuration
│   ├── notifications.ts              # Notification handlers for integrations
│   ├── crons.ts                      # Scheduled tasks
│   ├── auth.ts                       # Authentication functions
│   ├── fixPendingDevices.ts          # Utility for fixing pending devices
│   └── _generated/                   # Auto-generated Convex client code
├── frontend/                         # Web dashboard
│   ├── index.html                    # Main dashboard page
│   ├── app.js                        # Frontend application logic
│   ├── auth.js                       # Authentication handling
│   ├── integrations.js               # Integration configuration UI
│   ├── style.css                     # Dashboard styling
│   ├── config.js                     # Convex URL configuration (auto-generated)
│   ├── edit_modal.html               # Device edit modal
│   ├── Dockerfile                    # Container build definition
│   └── entrypoint.sh                 # Container startup script
├── logs/                             # Application logs
│   └── presence_tracker.log          # Presence tracker output
├── pyproject.toml                    # Python project configuration (UV)
├── requirements.txt                  # Python dependencies
├── package.json                      # JavaScript dependencies
├── bun.lock                          # Bun lock file
├── docker-compose.yml                # Web dashboard container orchestration
├── convex.json                       # Convex project configuration
├── setup.sh                          # Interactive setup script
└── .env.example                      # Environment variable template
```

## Systemd Services

The setup script automatically installs and configures three systemd services:

- **presence-tracker.service** - Runs the main presence tracker (`src/presence_tracker.py`)
- **bluetooth-agent.service** - Runs the Bluetooth pairing agent (`src/bluetooth_agent.py`)
- **bluetooth-discoverable.service** - Makes the Pi discoverable on boot

All services are automatically enabled and started during full installation.

## License

MIT
