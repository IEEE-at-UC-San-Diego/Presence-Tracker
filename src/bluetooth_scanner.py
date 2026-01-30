import subprocess
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional
import bluetooth
import time
from threading import Lock
from datetime import datetime, timedelta
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

# Configure logger for bluetooth_scanner
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(
        "logs/bluetooth_scanner.log",
        maxBytes=100000,
        backupCount=1,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)

# Global state for auto-reconnection
# Track last connection attempt time for each device to avoid spamming
_last_connection_attempts: dict[str, float] = {}
_connection_state_lock = Lock()

# Cooldown period between connection attempts for the same device (seconds)
RECONNECT_COOLDOWN = 30

# How long to consider a device "recently attempted" (seconds)
RECENT_ATTEMPT_WINDOW = 60

# How long to wait for bluetoothctl connect attempts (seconds)
CONNECT_TIMEOUT_SECONDS = int(os.getenv("CONNECT_TIMEOUT_SECONDS", "10"))

# How long to run bluetoothctl scan for in-range detection (seconds)
IN_RANGE_SCAN_SECONDS = int(os.getenv("IN_RANGE_SCAN_SECONDS", "5"))

# Whether to disconnect after a successful auto-reconnect to free connection slots
DISCONNECT_AFTER_SUCCESS = os.getenv("DISCONNECT_AFTER_SUCCESS", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Whether to probe paired devices if scan finds nothing in range
ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN = os.getenv(
    "ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN", "true"
).lower() in ("1", "true", "yes")

# Cap reconnection attempts per cycle to avoid long stalls
MAX_RECONNECT_PER_CYCLE = int(os.getenv("MAX_RECONNECT_PER_CYCLE", "4"))


def _device_info_indicates_in_range(info_output: str) -> bool:
    """
    Heuristic to decide if a device is currently in range based on bluetoothctl info output.

    We treat a device as "in range" if it's connected or has a recent RSSI/TxPower.
    """
    if "Connected: yes" in info_output:
        return True
    if "RSSI:" in info_output or "TxPower:" in info_output:
        return True
    return False


def _refresh_bluetooth_scan(duration: int) -> None:
    """Trigger a short bluetoothctl scan to refresh RSSI for in-range devices."""
    if duration <= 0:
        return
    try:
        # Newer bluetoothctl supports --timeout
        result = subprocess.run(
            ["bluetoothctl", "--timeout", str(duration), "scan", "on"],
            capture_output=True,
            text=True,
            timeout=duration + 2,
        )
        if result.returncode == 0:
            return
        logger.debug(f"bluetoothctl --timeout scan failed: {result.stderr.strip()}")
    except FileNotFoundError:
        logger.error("bluetoothctl not found. Bluetooth may not be available.")
        return
    except Exception as e:
        logger.debug(f"bluetoothctl scan with --timeout failed: {e}")

    # Fallback: start scan, wait, then stop
    try:
        subprocess.run(
            ["bluetoothctl", "scan", "on"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(duration)
        subprocess.run(
            ["bluetoothctl", "scan", "off"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        logger.debug(f"Fallback bluetoothctl scan failed: {e}")


def check_device_connected(mac_address: str) -> bool:
    """
    Check if a specific Bluetooth device is currently connected/paired.

    Uses bluetoothctl to check if a device is connected. This is more reliable
    on Raspberry Pi than pybluez for checking connection status, especially
    for iOS devices which may not be discoverable via scanning.

    Args:
        mac_address: The MAC address of the device to check (format: XX:XX:XX:XX:XX:XX)

    Returns:
        True if the device is connected, False otherwise
    """
    try:
        # Use bluetoothctl to check if device is connected
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            logger.debug(f"Device {mac_address} not found or bluetoothctl error")
            return False

        # Check if "Connected: yes" is in the output
        if "Connected: yes" in result.stdout:
            logger.debug(f"Device {mac_address} is connected")
            return True
        else:
            logger.debug(f"Device {mac_address} is not connected")
            return False

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout checking connection for {mac_address}")
        return False
    except FileNotFoundError:
        logger.error("bluetoothctl not found. Bluetooth may not be available.")
        return False
    except Exception as e:
        logger.error(f"Error checking device connection: {e}")
        return False


def scan_for_devices() -> dict[str, str]:
    """
    Scan for visible Bluetooth devices and return a dictionary of MAC addresses to names.

    Uses pybluez to discover nearby Bluetooth devices. This works well for
    Android devices and other devices that are discoverable. Note that iOS
    devices are typically not discoverable via scanning when paired.

    Returns:
        Dictionary mapping MAC addresses to device names
    """
    devices: dict[str, str] = {}

    try:
        # Discover devices for 8 seconds
        logger.info("Scanning for Bluetooth devices...")
        discovered_devices = bluetooth.discover_devices(
            duration=8, lookup_names=True, flush_cache=True
        )

        for addr, name in discovered_devices:
            devices[addr] = name if name else "Unknown"
            logger.info(f"Found device: {addr} - {devices[addr]}")

        logger.info(f"Scan complete. Found {len(devices)} devices.")
        return devices

    except bluetooth.BluetoothError as e:
        logger.error(f"Bluetooth scanning error: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error during scan: {e}")
        return {}


def get_device_name(mac_address: str) -> Optional[str]:
    """
    Get the friendly name of a device by MAC address.

    Uses bluetoothctl to get device info, which is more reliable for
    paired devices including iOS devices.

    Args:
        mac_address: The MAC address of the device (format: XX:XX:XX:XX:XX:XX)

    Returns:
        The device name if found, None otherwise
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            logger.debug(f"Device {mac_address} not found")
            logger.debug(f"bluetoothctl stderr: {result.stderr}")
            return None

        # Log full output for debugging
        logger.info(f"bluetoothctl info for {mac_address}:\n{result.stdout}")

        # Parse the output to find the device name
        for line in result.stdout.split("\n"):
            if "Name:" in line:
                name = line.split(":", 1)[1].strip()
                if name:
                    logger.info(f"✓ Device {mac_address} name found: '{name}'")
                    return name

        logger.warning(f"✗ No Name field found for device {mac_address}")
        return None

    except subprocess.TimeoutExpired:
        logger.warning(f"✗ Timeout getting name for {mac_address}")
        return None
    except FileNotFoundError:
        logger.error("✗ bluetoothctl not found. Bluetooth may not be available.")
        return None
    except Exception as e:
        logger.error(f"✗ Error getting device name: {e}")
        return None


def scan_paired_devices() -> dict[str, str]:
    """
    Scan for paired Bluetooth devices using bluetoothctl.

    This is useful for getting a list of all paired devices, including
    iOS devices which may not be discoverable via scanning.

    Returns:
        Dictionary mapping MAC addresses to device names
    """
    devices: dict[str, str] = {}

    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Paired"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error("Failed to get paired devices")
            return {}

        # Parse output: format is "Device XX:XX:XX:XX:XX:XX Device Name"
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Device "):
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    mac_address = parts[1]
                    name = parts[2]
                    devices[mac_address] = name
                    logger.debug(f"Paired device: {mac_address} - {name}")

        logger.info(f"Found {len(devices)} paired devices")
        return devices

    except subprocess.TimeoutExpired:
        logger.warning("Timeout scanning paired devices")
        return {}
    except FileNotFoundError:
        logger.error("bluetoothctl not found. Bluetooth may not be available.")
        return {}
    except Exception as e:
        logger.error(f"Error scanning paired devices: {e}")
        return {}


def get_all_connected_devices() -> list[str]:
    """
    Get list of MAC addresses for all currently connected Bluetooth devices.

    Uses bluetoothctl to directly query connected devices. This is more reliable
    than scanning for findable devices, as it includes paired devices that are
    connected but not discoverable (like iOS devices).

    Returns:
        List of MAC addresses of connected devices
    """
    connected_devices: list[str] = []

    try:
        # Directly get connected devices using new bluetoothctl syntax
        result = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error("Failed to get connected devices")
            return []

        # Extract MAC addresses from connected devices
        # Output format: "Device XX:XX:XX:XX:XX:XX Device Name"
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Device "):
                parts = line.split(" ", 2)
                if len(parts) >= 2:
                    mac_address = parts[1]
                    connected_devices.append(mac_address)
                    logger.debug(f"Connected device: {mac_address}")

        logger.info(f"Found {len(connected_devices)} connected device(s)")
        return connected_devices

    except subprocess.TimeoutExpired:
        logger.warning("Timeout getting connected devices")
        return []
    except FileNotFoundError:
        logger.error("bluetoothctl not found. Bluetooth may not be available.")
        return []
    except Exception as e:
        logger.error(f"Error getting connected devices: {e}")
        return []


def trust_device(mac_address: str) -> bool:
    """
    Trust a Bluetooth device to allow auto-connect.

    Trusted devices can automatically reconnect when they come into range.

    Args:
        mac_address: The MAC address of the device to trust

    Returns:
        True if the device was trusted successfully, False otherwise
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "trust", mac_address],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if "trust succeeded" in result.stdout.lower() or result.returncode == 0:
            logger.info(f"Trusted device {mac_address}")
            return True
        else:
            logger.warning(f"Failed to trust device {mac_address}: {result.stdout}")
            return False

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout trusting device {mac_address}")
        return False
    except Exception as e:
        logger.error(f"Error trusting device {mac_address}: {e}")
        return False


def is_device_trusted(mac_address: str) -> bool:
    """
    Check if a device is trusted.

    Args:
        mac_address: The MAC address of the device to check

    Returns:
        True if the device is trusted, False otherwise
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True,
            text=True,
            timeout=5,
        )

        return "Trusted: yes" in result.stdout

    except Exception as e:
        logger.error(f"Error checking trust status for {mac_address}: {e}")
        return False


def connect_device(mac_address: str) -> bool:
    """
    Attempt to connect to a Bluetooth device.

    Args:
        mac_address: The MAC address of the device to connect to

    Returns:
        True if connected successfully, False otherwise
    """
    try:
        # First ensure the device is trusted
        if not is_device_trusted(mac_address):
            logger.info(f"Device {mac_address} not trusted, trusting now...")
            trust_device(mac_address)

        logger.info(f"Attempting to connect to {mac_address}...")
        result = subprocess.run(
            ["bluetoothctl", "connect", mac_address],
            capture_output=True,
            text=True,
            timeout=CONNECT_TIMEOUT_SECONDS,  # Connection can take time
        )

        if "Connection successful" in result.stdout or check_device_connected(mac_address):
            logger.info(f"Successfully connected to {mac_address}")
            return True
        else:
            logger.debug(f"Could not connect to {mac_address}: {result.stdout.strip()}")
            return False

    except subprocess.TimeoutExpired:
        logger.debug(f"Timeout connecting to {mac_address} (device may be out of range)")
        return False
    except Exception as e:
        logger.error(f"Error connecting to device {mac_address}: {e}")
        return False

def disconnect_device(mac_address: str) -> bool:
    """
    Disconnect from a Bluetooth device.

    Args:
        mac_address: The MAC address of the device to disconnect

    Returns:
        True if disconnected successfully, False otherwise
    """
    try:
        logger.info(f"Disconnecting from {mac_address}...")
        result = subprocess.run(
            ["bluetoothctl", "disconnect", mac_address],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if "Successful disconnected" in result.stdout or not check_device_connected(mac_address):
            logger.info(f"Successfully disconnected from {mac_address}")
            return True
        else:
            logger.debug(f"Could not disconnect from {mac_address}: {result.stdout.strip()}")
            return False

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout disconnecting from {mac_address}")
        return False
    except Exception as e:
        logger.error(f"Error disconnecting from device {mac_address}: {e}")
        return False


def remove_device(mac_address: str) -> bool:
    """Remove a device from the Bluetooth adapter's paired devices list.

    Args:
        mac_address: The MAC address of the device to remove

    Returns:
        True if the device was successfully removed, False otherwise
    """
    try:
        result = subprocess.run(
            ["bluetoothctl", "remove", mac_address],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0 and "has been removed" in result.stdout:
            logger.info(f"Successfully removed device {mac_address}")
            return True
        else:
            logger.warning(f"Failed to remove device {mac_address}: {result.stdout.strip()}")
            return False

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout removing device {mac_address}")
        return False
    except Exception as e:
        logger.error(f"Error removing device {mac_address}: {e}")
        return False


def get_paired_devices() -> list[str]:
    """
    Get list of all paired Bluetooth device MAC addresses.

    Returns:
        List of MAC addresses of paired devices
    """
    paired_devices: list[str] = []

    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Paired"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error("Failed to get paired devices")
            return []

        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Device "):
                parts = line.split(" ", 2)
                if len(parts) >= 2:
                    paired_devices.append(parts[1])

        return paired_devices

    except Exception as e:
        logger.error(f"Error getting paired devices: {e}")
        return []


def _can_attempt_reconnection(mac_address: str) -> bool:
    """
    Check if we can attempt reconnection to a device based on cooldown.

    Args:
        mac_address: The MAC address of the device

    Returns:
        True if reconnection can be attempted, False if in cooldown
    """
    with _connection_state_lock:
        last_attempt = _last_connection_attempts.get(mac_address, 0)
        time_since_attempt = time.time() - last_attempt

        if time_since_attempt < RECONNECT_COOLDOWN:
            logger.debug(
                f"Device {mac_address} in cooldown: "
                f"{time_since_attempt:.1f}s elapsed, need {RECONNECT_COOLDOWN}s"
            )
            return False

        return True


def _record_connection_attempt(mac_address: str) -> None:
    """
    Record a connection attempt for cooldown tracking.

    Args:
        mac_address: The MAC address of the device
    """
    with _connection_state_lock:
        _last_connection_attempts[mac_address] = time.time()


def _cleanup_old_attempts() -> None:
    """
    Clean up old connection attempts that are outside the time window.
    """
    with _connection_state_lock:
        current_time = time.time()
        to_remove = []

        for mac_address, attempt_time in _last_connection_attempts.items():
            if current_time - attempt_time > RECENT_ATTEMPT_WINDOW:
                to_remove.append(mac_address)

        for mac_address in to_remove:
            del _last_connection_attempts[mac_address]

        if to_remove:
            logger.debug(f"Cleaned up {len(to_remove)} old connection attempt records")


def _pick_reconnect_candidates(candidates: set[str], max_count: int) -> list[str]:
    """
    Pick a fair subset of reconnection candidates based on last attempt time.

    Oldest (or never attempted) devices are tried first.
    """
    if max_count <= 0:
        return list(candidates)

    def last_attempt(mac: str) -> float:
        return _last_connection_attempts.get(mac, 0)

    return sorted(candidates, key=last_attempt)[:max_count]


def _get_registered_convex_devices() -> set[str]:
    """Fetch the set of Convex-registered device MAC addresses."""
    try:
        from presence_tracker import get_known_devices  # type: ignore
    except Exception as e:
        logger.error(f"Convex registration prefilter unavailable (import error): {e}")
        return set()

    try:
        devices = get_known_devices()
    except Exception as e:
        logger.error(f"Convex registration prefilter failed when fetching devices: {e}")
        return set()

    registered = {
        device.get("macAddress")
        for device in devices
        if device.get("macAddress") and not device.get("pendingRegistration")
    }

    return set(registered)


def _probe_single_device(mac_address: str, disconnect_after: bool) -> tuple[str, bool]:
    """
    Helper function to probe a single device.
    
    Args:
        mac_address: The MAC address to probe.
        disconnect_after: Whether to disconnect after a successful connect.
        
    Returns:
        Tuple of (mac_address, success).
    """
    try:
        logger.info(f"Probing device: {mac_address}")
        success = connect_device(mac_address)
        if success and disconnect_after:
            disconnect_device(mac_address)
        return (mac_address, success)
    except Exception as e:
        logger.error(f"Error probing device {mac_address}: {e}")
        return (mac_address, False)


def probe_devices(mac_addresses: list[str], disconnect_after: bool = True) -> dict[str, bool]:
    """
    Probe devices by attempting a connect and optional disconnect.

    Uses concurrent processing to connect to multiple devices simultaneously,
    with each connection having its own timeout.

    Args:
        mac_addresses: List of MAC addresses to probe.
        disconnect_after: Whether to disconnect after a successful connect.

    Returns:
        Dictionary mapping MAC address to connection result.
    """
    results: dict[str, bool] = {}
    
    if not mac_addresses:
        return results
    
    # Determine concurrency limit - use MAX_RECONNECT_PER_CYCLE or reasonable default
    max_workers = MAX_RECONNECT_PER_CYCLE if MAX_RECONNECT_PER_CYCLE > 0 else min(len(mac_addresses), 4)
    
    # Use ThreadPoolExecutor for concurrent connection attempts
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all connection tasks
        future_to_mac = {
            executor.submit(_probe_single_device, mac_address, disconnect_after): mac_address
            for mac_address in mac_addresses
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_mac):
            mac_address, success = future.result()
            results[mac_address] = success
    
    return results


def scan_for_devices_in_range() -> set[str]:
    """
    Scan for Bluetooth devices that are currently in range.

    Uses both pybluez discovery and bluetoothctl to get a comprehensive list
    of devices that are discoverable or previously paired devices that are in range.

    Returns:
        Set of MAC addresses of devices in range
    """
    devices_in_range: set[str] = set()

    # Refresh bluetoothctl scan data to get recent RSSI readings
    _refresh_bluetooth_scan(IN_RANGE_SCAN_SECONDS)

    # Method 1: Use pybluez to discover visible devices
    try:
        logger.debug("Scanning for discoverable devices using pybluez...")
        discovered_devices = bluetooth.discover_devices(
            duration=5, lookup_names=False, flush_cache=True
        )
        devices_in_range.update(discovered_devices)
        logger.debug(f"Found {len(discovered_devices)} discoverable device(s)")
    except bluetooth.BluetoothError as e:
        logger.debug(f"Pybluez scan error: {e}")
    except Exception as e:
        logger.debug(f"Unexpected error during pybluez scan: {e}")

    # Method 2: Use bluetoothctl to scan for paired devices that are in range
    # This is more reliable for iOS devices and devices that don't advertise
    try:
        logger.debug("Checking for paired devices in range using bluetoothctl...")
        # Get all known devices (both paired and discovered)
        result = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("Device "):
                    parts = line.split(" ", 2)
                    if len(parts) >= 2:
                        mac_address = parts[1]
                        # Check if the device is actually accessible by getting its info
                        info_result = subprocess.run(
                            ["bluetoothctl", "info", mac_address],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if info_result.returncode == 0 and _device_info_indicates_in_range(
                            info_result.stdout
                        ):
                            devices_in_range.add(mac_address)
                            logger.debug(f"Device in range: {mac_address}")
                        else:
                            logger.debug(f"Device out of range (no RSSI/connection): {mac_address}")

        logger.debug(f"Total devices in range: {len(devices_in_range)}")
    except subprocess.TimeoutExpired:
        logger.debug("Timeout checking devices in range")
    except Exception as e:
        logger.debug(f"Error checking devices in range: {e}")

    return devices_in_range


def _reconnect_single_device(
    mac_address: str, disconnect_after_success: bool
) -> tuple[str, bool] | None:
    """
    Helper function to reconnect a single device.
    
    This function handles the cooldown check, connection attempt recording,
    and actual connection/disconnection logic for a single device.
    
    Args:
        mac_address: The MAC address to reconnect.
        disconnect_after_success: Whether to disconnect after a successful connection.
        
    Returns:
        Tuple of (mac_address, success) if a connection attempt was made, otherwise None.
    """
    # Check cooldown - this is thread-safe due to the lock in _can_attempt_reconnection
    if not _can_attempt_reconnection(mac_address):
        logger.debug(f"Skipping {mac_address} - in cooldown")
        return None

    # Record attempt before trying - thread-safe due to the lock in _record_connection_attempt
    _record_connection_attempt(mac_address)

    try:
        logger.info(f"Attempting auto-reconnection to: {mac_address}")

        # Attempt connection
        success = connect_device(mac_address)

        if success:
            logger.info(f"✓ Successfully auto-reconnected to: {mac_address}")
            if disconnect_after_success:
                disconnect_device(mac_address)
        else:
            logger.warning(f"✗ Failed to auto-reconnect to: {mac_address}")

        return (mac_address, success)
    except Exception as e:
        logger.error(f"Error during auto-reconnection to {mac_address}: {e}")
        return (mac_address, False)


def auto_reconnect_paired_devices(
    whitelist_macs: set[str] | None = None,
    disconnect_after_success: bool | None = None,
) -> dict[str, bool]:
    """
    Automatically reconnect to paired devices that are detected in range.

    This function:
    1. Gets the list of paired devices
    2. Scans for devices currently in range
    3. For paired devices in range that are not connected, attempts reconnection concurrently
    4. Respects cooldown periods to avoid spamming connection attempts
    5. Tracks connection attempts and logs success/failure

    Uses concurrent processing to connect to multiple devices simultaneously,
    with MAX_RECONNECT_PER_CYCLE controlling the concurrency limit.

    Args:
        whitelist_macs: Optional set of MAC addresses to limit reconnection attempts to.
                       If provided, only devices in this set will be candidates.
        disconnect_after_success: If True, disconnect after a successful connection
                                  to free connection slots.

    Returns:
        Dictionary mapping MAC addresses to connection results (True=success, False=failed)
    """
    results: dict[str, bool] = {}
    if disconnect_after_success is None:
        disconnect_after_success = DISCONNECT_AFTER_SUCCESS

    try:
        logger.info("=== Starting auto-reconnection cycle ===")

        # Clean up old attempt records periodically
        _cleanup_old_attempts()

        # Get currently connected devices
        connected_devices = get_all_connected_devices()
        connected_set = set(connected_devices)
        logger.info(f"Currently connected: {len(connected_set)} device(s)")

        # Get paired devices
        paired_devices = get_paired_devices()
        paired_set = set(paired_devices)
        logger.info(f"Paired devices: {len(paired_set)} device(s)")

        # Get devices in range
        devices_in_range = scan_for_devices_in_range()
        logger.info(f"Devices in range: {len(devices_in_range)} device(s)")

        if not devices_in_range and ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN:
            logger.info("No devices detected in range; probing paired devices instead")
            devices_in_range = paired_set

        # Find paired devices that are in range but not connected
        devices_to_connect = (paired_set & devices_in_range) - connected_set

        # Apply whitelist if provided
        if whitelist_macs is not None:
            logger.debug(f"Applying whitelist: {len(whitelist_macs)} allowed device(s)")
            original_count = len(devices_to_connect)
            devices_to_connect = devices_to_connect & whitelist_macs
            logger.debug(f"Filtered candidates from {original_count} to {len(devices_to_connect)}")

        if not devices_to_connect:
            logger.info("No paired devices in range that need connection")
            return results

        registered_convex_devices = _get_registered_convex_devices()
        if not registered_convex_devices:
            logger.warning(
                "Convex registration prefilter unavailable; skipping auto-reconnection cycle"
            )
            return results

        registered_candidates = devices_to_connect & registered_convex_devices
        if not registered_candidates:
            logger.info("No registered Convex devices in range that need connection")
            return results

        max_candidates = MAX_RECONNECT_PER_CYCLE if MAX_RECONNECT_PER_CYCLE > 0 else 0
        ordered_candidates = _pick_reconnect_candidates(registered_candidates, max_candidates)

        if not ordered_candidates:
            logger.info("No devices available for auto-reconnection after Convex filtering")
            return results

        logger.info(f"Attempting to connect to {len(ordered_candidates)} device(s)")

        if MAX_RECONNECT_PER_CYCLE > 0:
            max_workers = min(len(ordered_candidates), MAX_RECONNECT_PER_CYCLE)
        else:
            max_workers = min(len(ordered_candidates), 4)
        max_workers = max(1, max_workers)

        # Use ThreadPoolExecutor for concurrent connection attempts
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all connection tasks
            future_to_mac = {
                executor.submit(
                    _reconnect_single_device, mac_address, disconnect_after_success
                ): mac_address
                for mac_address in ordered_candidates
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_mac):
                mac_address = future_to_mac[future]
                try:
                    attempt = future.result()
                    if attempt is None:
                        continue
                    _, success = attempt
                except Exception as e:
                    logger.error(
                        f"Unhandled error during auto-reconnection to {mac_address}: {e}"
                    )
                    success = False
                results[mac_address] = success

        logger.info("=== Auto-reconnection cycle complete ===")
        return results

    except Exception as e:
        logger.error(f"Error during auto-reconnection: {e}")
        return results


def get_reconnection_status() -> dict[str, dict[str, any]]:
    """
    Get the current status of reconnection attempts.

    Returns:
        Dictionary with information about recent connection attempts
    """
    with _connection_state_lock:
        current_time = time.time()
        status = {}

        for mac_address, attempt_time in _last_connection_attempts.items():
            time_since = current_time - attempt_time
            status[mac_address] = {
                "last_attempt": datetime.fromtimestamp(attempt_time).isoformat(),
                "seconds_ago": round(time_since, 1),
                "in_cooldown": time_since < RECONNECT_COOLDOWN,
            }

        return status
