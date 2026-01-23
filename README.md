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

| Variable | Description | Example |
|----------|-------------|---------|
| `CONVEX_DEPLOYMENT_URL` | Your Convex backend deployment URL (required) | `https://chatty-akita-508.convex.cloud` |
| `ORGANIZATION_NAME` | Display name for your organization (e.g., "My Organization") | - |

Note: `CONVEX_DEPLOYMENT_URL` is for the tracker/`.env` file. For the web dashboard, configure `CONVEX_DEPLOYMENT_URL` in GitHub Secrets (if using GitHub Pages) or as a Docker environment variable.

### Convex Environment Variables (Authentication)

The web dashboard uses password authentication to protect access. Configure these in your **Convex dashboard** under **Settings > Environment Variables**:

| Variable | Description | Required |
|----------|-------------|----------|
| `AUTH_PASSWORD` | Regular user password - provides view-only access | ✅ Yes |
| `ADMIN_PASSWORD` | Admin password - provides full access to all features (edit, delete, manage) | Optional |
| `ORGANIZATION_NAME` | Organization name for UI customization (fallback if not set in deployment config) | Optional |

**Setting up Authentication:**

1. Go to your [Convex dashboard](https://dashboard.convex.dev)
2. Select your deployment
3. Navigate to **Settings > Environment Variables**
4. Add `AUTH_PASSWORD` with your desired user password
5. (Optional) Add `ADMIN_PASSWORD` for admin-level access

**Access Levels:**
- **User** (AUTH_PASSWORD): View device status, attendance logs, and run Bluetooth scans
- **Admin** (ADMIN_PASSWORD): All user permissions plus device registration, editing, deletion, and integration management

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
ORGANIZATION_NAME=Your Organization Name

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
