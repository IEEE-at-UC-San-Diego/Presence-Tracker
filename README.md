# Presence Tracker

Bluetooth-based presence detection system using Convex backend, designed for Raspberry Pi.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Convex](https://img.shields.io/badge/Convex-1.31.5-purple.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## Quick Links

- [Initial Setup](#initial-setup) - Get started in minutes
- [Quick Start](#quick-start) - Detailed installation instructions
- [Deploy to GitHub Pages](#deploy-web-dashboard-to-github-pages) - Host web dashboard for free
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

## l2ping

`l2ping` is a utility for sending ICMPv6 echo requests to a Bluetooth device. It is used to determine if a device is present on the network.

Run the command
```
sudo setcap cap_net_raw+ep /usr/bin/l2ping
``` 
to grant the l2ping utility the necessary permissions to send ICMPv6 echo requests.

## Quick Start

### Automated Setup (Recommended)

Run the automated setup script on your Raspberry Pi:

```bash
cd "/home/user/Desktop/Presence Tracker"
./setup.sh
```

The script will display an interactive menu with options:
1. **Full Install** - Complete installation and configuration
2. **Update Config** - Update Bluetooth name and configuration
3. **Resetup/Redeploy Convex** - Trigger Convex re-deployment
4. **Restart Services** - Restart systemd services
5. **Make Bluetooth Discoverable** - Configure Bluetooth for discoverable mode

The script handles:
- Installing Node.js/npm (latest LTS)
- Installing UV and Bun package managers
- Installing BlueZ and Bluetooth tools
- Installing Python and JavaScript dependencies
- Configuring Bluetooth permissions and discoverability
- Installing and configuring systemd services
- Deploying to Convex backend
- Persisting configuration to `setup.config` and `.env`

**Configuration Persistence:**
The setup script automatically saves your settings to:
- `setup.config` - Stores BLUETOOTH_NAME and DEPLOYMENT_MODE for future runs
- `.env` - Updates with BLUETOOTH_NAME and DEPLOYMENT_MODE variables

Subsequent runs will recall saved configuration.

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

Your Pi will appear as **"Presence Tracker"** in Bluetooth scans.

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

### Deploy Web Dashboard to GitHub Pages

You can automatically deploy the web dashboard to GitHub Pages using GitHub Actions. This provides free hosting with HTTPS and automatic deployments when changes are pushed to the `main` branch.

**Setup Steps:**

 1. **Add GitHub Secrets**

    Go to your repository **Settings > Secrets and variables > Actions** and add:

    | Secret Name | Value | Required |
    |-------------|-------|----------|
    | `CONVEX_DEPLOYMENT_URL` | Your Convex backend URL (e.g., `https://chatty-akita-508.convex.cloud`) | ✅ Yes |
    | `CONVEX_URL_MODE` | `convex` (default) or `selfhosted` | Optional |
    | `CONVEX_SELF_HOSTED_URL` | Self-hosted Convex URL | Only for self-hosted |
    | `ORGANIZATION_NAME` | Your organization name for UI customization (e.g., "My Company") | Optional |

2. **Enable GitHub Pages**

   Go to repository **Settings > Pages**:
   - **Build and deployment** > **Source**: Select **GitHub Actions**
   - The workflow will handle deployment automatically

3. **Configure CORS in Convex**

   Add your GitHub Pages URL to Convex CORS settings:

   ```
   https://<username>.github.io
   ```

   1. Go to [Convex dashboard](https://dashboard.convex.dev)
   2. Select your deployment
   3. Navigate to **Settings > CORS**
   4. Add your GitHub Pages URL

4. **Push and Deploy**

   Push changes to `main` branch:
   ```bash
   git push origin main
   ```

   The workflow will automatically:
   - Generate `config.js` with your Convex URL from secrets
   - Deploy the frontend to GitHub Pages
   - Provide the live URL in the workflow run logs

5. **Access Your Dashboard**

   Your dashboard will be available at:
   ```
   https://<username>.github.io/<repository-name>/
   ```

**Manual Deployment:**

You can also manually trigger the deployment from GitHub Actions:
1. Go to **Actions** tab
2. Select **Deploy Website to GitHub Pages** workflow
3. Click **Run workflow** > **Run workflow**

**GitHub Actions Workflow:**

The workflow file is located at `.github/workflows/deploy-website.yml`.

Features:
- ✅ Auto-deploys on push to `main`
- ✅ Manual trigger available
- ✅ Supports both Convex cloud and self-hosted deployments
- ✅ Validates secrets before deployment
- ✅ Free hosting with HTTPS

**Comparing Deployment Options:**

| Option | Use Case | Pros | Cons |
|--------|----------|------|------|
| **Docker** | Raspberry Pi/local hosting | No setup required, runs with tracker | Need Docker, manage updates manually |
| **GitHub Pages** | Public web dashboard | Free, HTTPS, auto-deploys, CDN | Cannot access on offline network |

## Initial Setup

Follow these steps to get your presence tracker up and running:

### 1. Set Up Convex Backend

1. Create a free account at [convex.dev](https://convex.dev)
2. Create a new deployment
3. In your Convex dashboard, navigate to **Settings > Environment Variables** and add:
   - **Required**: `AUTH_PASSWORD` - Set a password for regular user access (view-only)
   - **Optional**: `ADMIN_PASSWORD` - Set a password for admin access (full permissions)
4. In **Settings > CORS**, add your web dashboard URL:
   - If using GitHub Pages: `https://<username>.github.io`
   - If using Docker locally: `http://localhost:3000`

### 2. Deploy the Web Dashboard

Choose one of the deployment options:

**Option A: GitHub Pages (Recommended)**
1. Follow the [Deploy Web Dashboard to GitHub Pages](#deploy-web-dashboard-to-github-pages) section
2. Add the following secrets in GitHub Settings:
   - `CONVEX_DEPLOYMENT_URL` (required)
   - `ORGANIZATION_NAME` (optional) - Your organization name for UI customization
3. Push to `main` to trigger deployment

**Option B: Docker**
1. Set environment variables in your `.env` file:
   - `CONVEX_DEPLOYMENT_URL` (required)
   - `ORGANIZATION_NAME` (optional) - Your organization name for UI customization
2. Run `docker-compose up -d`
3. Access at `http://localhost:3000`

### 3. Connect to the Raspberry Pi

1. SSH into your Raspberry Pi or connect directly
2. Run the setup script:
   ```bash
   cd "/path/to/Presence-Tracker"
   ./setup.sh
   ```
3. Select **Option 1: Full Install**
4. Follow the prompts:
   - Select deployment mode (Convex, self-hosted, or skip)
   - Set your Bluetooth device name
   - The script will handle all installation and configuration

### 4. Login to the Web Dashboard

1. Open your web dashboard in a browser:
   - GitHub Pages: `https://<username>.github.io/<repository-name>/`
   - Docker: `http://<pi-ip>:3000` or `http://localhost:3000`
2. Enter the password you set in Step 1 (`AUTH_PASSWORD` or `ADMIN_PASSWORD`)
3. **Important**: It may take up to a minute for the dashboard to fully load and connect to the backend

### 5. Register Your Device

1. Once logged in, click **"Scan for Devices"** to discover your Bluetooth device
2. Find your device in the scan results and click **"Register"**
3. Enter your **first name** and **last name**
4. Click **"Save"** to complete registration

> **Heads up:** As soon as a new device connects to the Raspberry Pi it is published to Convex as a *pending* entry. It will show up on the website immediately, but it will remain pending (and excluded from attendance) until you finish this step in the UI.

**You're all set!** 🎉

Your device will now be tracked for presence. The tracker will automatically:
- Detect when your device is connected/present
- Update the dashboard in real-time
- Log attendance history

**What's Next:**
- Pair other devices and repeat Step 5 to register them
- Configure Discord/Slack integrations for notifications (see dashboard settings)
- Monitor attendance logs in the dashboard

## Environment Configuration

Create a `.env` file by copying the example:

```bash
cp .env.example .env
```

### Required Variables

These variables must be set in your `.env` file for the tracker to run:

| Variable | Description | Example |
|----------|-------------|---------|
| `CONVEX_DEPLOYMENT_URL` | Your Convex backend deployment URL (cloud deployment) | `https://chatty-akita-508.convex.cloud` |

**Authentication Variables (Set in Convex Dashboard)**

| Variable | Description | Required | Where to Set |
|----------|-------------|----------|--------------|
| `AUTH_PASSWORD` | Regular user password - provides view-only access | ✅ Yes | Convex Dashboard → Settings → Environment Variables |
| `ADMIN_PASSWORD` | Admin password - provides full access (edit, delete, manage) | Optional | Convex Dashboard → Settings → Environment Variables |

**Setting up Authentication:**

1. Go to your [Convex dashboard](https://dashboard.convex.dev)
2. Select your deployment
3. Navigate to **Settings > Environment Variables**
4. Add `AUTH_PASSWORD` with your desired user password
5. (Optional) Add `ADMIN_PASSWORD` for admin-level access

**Access Levels:**
- **User** (AUTH_PASSWORD): View device status, attendance logs, and run Bluetooth scans
- **Admin** (ADMIN_PASSWORD): All user permissions plus device registration, editing, deletion, and integration management

### Optional Variables

These have sensible defaults and only need to be changed if you want to customize behavior.

#### Authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `ADMIN_PASSWORD` | Admin password - provides full access to all features | Not set (user-only access) |

#### Convex Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `CONVEX_SELF_HOSTED_URL` | Self-hosted Convex URL (alternative to CONVEX_DEPLOYMENT_URL) | Not set |
| `CONVEX_SELF_HOSTED_ADMIN_KEY` | Admin key for self-hosted Convex | Not set |
| `CONVEX_URL_MODE` | Mode selector: `convex` (cloud) or `selfhosted` | `convex` |
| `DEPLOYMENT_MODE` | Deployment mode (same as CONVEX_URL_MODE) | `convex` |
| `ORGANIZATION_NAME` | Display name for your organization | `Presence Tracker` |

#### Presence Tracking

| Variable | Description | Default |
|----------|-------------|---------|
| `GRACE_PERIOD_SECONDS` | Time new devices have to be registered before auto-deletion | `300` |
| `PRESENT_TTL_SECONDS` | How long a device is considered present after last seen | `45` |
| `REGISTRATION_RETRY_SECONDS` | How frequently the tracker retries publishing a newly seen device | `5` |
| `UNPUBLISHED_DEVICE_TTL_SECONDS` | How long to keep retrying to publish a device after it disconnects | `600` |
| `ENABLE_DEVICE_DIAGNOSTICS` | Enable detailed device diagnostic logging | `false` |
| `ENABLE_ADAPTIVE_HYSTERESIS` | Enable adaptive hysteresis for presence smoothing | `true` |
| `ABSENCE_HOLD_SECONDS` | Hold time before allowing absence transition | `120` |
| `ABSENCE_CONSECUTIVE_MISS_THRESHOLD` | Number of consecutive misses before marking absent | `3` |
| `FLAP_MONITOR_WINDOW_SECONDS` | Time window for detecting status flapping | `3600` |
| `FLAP_ALERT_THRESHOLD` | Number of transitions in window to trigger flap alert | `4` |
| `ENABLE_AUTO_FREEZE_ON_FLAP` | Auto-freeze device status when flapping detected | `true` |
| `AUTO_FREEZE_DURATION_SECONDS` | How long to freeze device status when flapping | `300` |
| `DEVICE_OVERRIDE_FILE` | Path to JSON file with device overrides | `config/device_overrides.json` |
| `DEVICE_OVERRIDE_REFRESH_SECONDS` | How often to reload device override file | `30` |
| `FAST_PATH_QUEUE_ENABLED` | Enable fast-path queue for rapid presence events | `true` |
| `FAST_PATH_EVENT_SUPPRESSION_SECONDS` | Suppress duplicate fast-path events within this window | `3` |
| `FAST_PATH_QUEUE_HOST` | Fast-path queue server host | `127.0.0.1` |
| `FAST_PATH_QUEUE_PORT` | Fast-path queue server port | `51975` |
| `FAST_PATH_QUEUE_AUTH_KEY` | Fast-path queue authentication key | `presence-fast-path` |
| `FAST_PATH_QUEUE_RETRY_SECONDS` | Retry delay for fast-path queue connection | `5` |
| `DISCONNECT_CONNECTED_AFTER_CYCLE` | Disconnect devices after each polling cycle | `true` |

#### Bluetooth

| Variable | Description | Default |
|----------|-------------|---------|
| `L2PING_TIMEOUT_SECONDS` | Timeout for l2ping device detection | `1` |
| `L2PING_COUNT` | Number of pings per device in each cycle | `1` |
| `CONNECT_TIMEOUT_SECONDS` | Max wait time for bluetoothctl connect attempts | `10` |
| `DEVICE_INFO_CACHE_SECONDS` | Cache TTL for bluetoothctl info calls | `5` |
| `ADAPTER_WATCHDOG_INTERVAL_SECONDS` | Interval for checking adapter health (0 = disabled) | `60` |
| `ADAPTER_RECOVERY_BACKOFF_SECONDS` | Backoff time between adapter recovery attempts | `5` |
| `ADVERTISE_NUDGE_COMMAND` | Command to nudge LE advertising if needed | `bluetoothctl advertise on` |
| `ADVERTISE_SCAN_DURATION_SECONDS` | Scan duration for adapter recovery | `3` |

#### Frontend

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Frontend server port | `3132` |
| `FRONTEND_PORT` | Alternative frontend port (same as PORT) | `3132` |

#### Logging

| Variable | Description | Default |
|----------|-------------|---------|
| `LOG_DIR` | Directory for log files | `logs` (created if needed) |
| `LOG_MAX_LINES` | Maximum lines per log file before rotation | Not set (no rotation) |

### Example .env File

```bash
# Required
CONVEX_DEPLOYMENT_URL=https://your-convex-deployment.convex.cloud

# Optional - Authentication (set in Convex Dashboard)
# AUTH_PASSWORD=your-password-here
# ADMIN_PASSWORD=admin-password-here

# Optional - Organization Name
ORGANIZATION_NAME=My Organization

# Optional - Presence Tracking (defaults shown)
GRACE_PERIOD_SECONDS=300
PRESENT_TTL_SECONDS=45
REGISTRATION_RETRY_SECONDS=5
UNPUBLISHED_DEVICE_TTL_SECONDS=600
ENABLE_DEVICE_DIAGNOSTICS=false
ENABLE_ADAPTIVE_HYSTERESIS=true
ABSENCE_HOLD_SECONDS=120
ABSENCE_CONSECUTIVE_MISS_THRESHOLD=3
FLAP_MONITOR_WINDOW_SECONDS=3600
FLAP_ALERT_THRESHOLD=4
ENABLE_AUTO_FREEZE_ON_FLAP=true
AUTO_FREEZE_DURATION_SECONDS=300
DEVICE_OVERRIDE_FILE=config/device_overrides.json
DEVICE_OVERRIDE_REFRESH_SECONDS=30
FAST_PATH_QUEUE_ENABLED=true
FAST_PATH_EVENT_SUPPRESSION_SECONDS=3
FAST_PATH_QUEUE_HOST=127.0.0.1
FAST_PATH_QUEUE_PORT=51975
FAST_PATH_QUEUE_AUTH_KEY=presence-fast-path
FAST_PATH_QUEUE_RETRY_SECONDS=5
DISCONNECT_CONNECTED_AFTER_CYCLE=true

# Optional - Bluetooth (defaults shown)
L2PING_TIMEOUT_SECONDS=1
L2PING_COUNT=1
CONNECT_TIMEOUT_SECONDS=10
DEVICE_INFO_CACHE_SECONDS=5
ADAPTER_WATCHDOG_INTERVAL_SECONDS=60
ADAPTER_RECOVERY_BACKOFF_SECONDS=5
ADVERTISE_NUDGE_COMMAND=bluetoothctl advertise on
ADVERTISE_SCAN_DURATION_SECONDS=3

# Optional - Frontend (defaults shown)
PORT=3132

# Optional - Logging (defaults shown)
LOG_DIR=logs
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
3. Find "Presence Tracker" 
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

When you first register a device (transition from pending to registered), the device is immediately entered into the polling cycle. Presence status will update within the next polling cycle (every 5 seconds by default).

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
- Reduce `PRESENT_TTL_SECONDS` (default: 45) for faster absence detection
- Reduce `L2PING_COUNT` (default: 1) or `L2PING_TIMEOUT_SECONDS` (default: 1)
- Reduce `DEVICE_INFO_CACHE_SECONDS` (default: 5) for fresher device info
- Check Bluetooth hardware issues with `hciconfig`
- Reduce number of tracked devices

#### High CPU Usage
- Decrease `FLAP_MONITOR_WINDOW_SECONDS` (default: 3600) to reduce monitoring overhead
- Disable device diagnostics: `ENABLE_DEVICE_DIAGNOSTICS=false`
- Disable adaptive hysteresis: `ENABLE_ADAPTIVE_HYSTERESIS=false`
- Disable fast-path queue: `FAST_PATH_QUEUE_ENABLED=false`
- Check for Bluetooth adapter issues

### Setup Issues

#### Bun Command Not Found After Setup Script
If `bun` or `bunx` commands are not available after running `setup.sh`:

```bash
# Source your bashrc to load bun in the current session
source ~/.bashrc

# Verify bun is installed
bun --version
```

This can happen because bash needs to be re-sourced afterbun is installed. The setup script attempts to handle this automatically, but if it doesn't work, you can manually source your bashrc.

#### Setup Script Fails at Convex Deployment
If the setup script fails during the Convex deployment, you can manually deploy later:

```bash
# Source bashrc first
source ~/.bashrc

# Initialize Convex deployment
bunx convex dev

# Deploy to Convex
bunx convex deploy
```

The setup script now automatically runs `bunx convex dev` before deploying to ensure the deployment is properly initialized.

#### Configuration Not Persisting
If `setup.config` is not being created or settings aren't saving:

```bash
# Check if setup.config exists
ls -la setup.config

# View saved configuration
cat setup.config

# Manually run setup configuration option
./setup.sh
# Select option 2) Update Config
```

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
├── setup.config                      # Setup script configuration (auto-generated)
└── .env.example                      # Environment variable template
```

**Additional Files Created During Setup:**
- `.env` - Environment configuration (created from .env.example)
- `setup.config` - Persists setup script settings (BLUETOOTH_NAME, DEPLOYMENT_MODE)

## Systemd Services

The setup script automatically installs and configures three systemd services:

- **presence-tracker.service** - Runs the main presence tracker (`src/presence_tracker.py`)
- **bluetooth-agent.service** - Runs the Bluetooth pairing agent (`src/bluetooth_agent.py`)
- **bluetooth-discoverable.service** - Makes the Pi discoverable on boot

All services are automatically enabled and started during full installation.

## License

MIT
