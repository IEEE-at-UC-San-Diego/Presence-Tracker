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

# L2PING configuration for passive detection
L2PING_TIMEOUT_SECONDS = int(os.getenv("L2PING_TIMEOUT_SECONDS", "2"))
L2PING_COUNT = int(os.getenv("L2PING_COUNT", "1"))
L2PING_CONCURRENT_WORKERS = int(os.getenv("L2PING_CONCURRENT_WORKERS", "10"))

# Name request timeout for fallback detection
NAME_REQUEST_TIMEOUT_SECONDS = int(os.getenv("NAME_REQUEST_TIMEOUT_SECONDS", "3"))

# How long to consider a device "recently attempted" (seconds)
RECENT_ATTEMPT_WINDOW = 60

# How long to wait for bluetoothctl connect attempts (seconds)
CONNECT_TIMEOUT_SECONDS = int(os.getenv("CONNECT_TIMEOUT_SECONDS", "10"))

# How long to run bluetoothctl scan for in-range detection (seconds)
IN_RANGE_SCAN_SECONDS = int(os.getenv("IN_RANGE_SCAN_SECONDS", "5"))

# Optional staggered scan chunk length (seconds) for faster partial refreshes
SCAN_STAGGER_SECONDS = int(os.getenv("SCAN_STAGGER_SECONDS", "2"))

# Whether to disconnect after a successful auto-reconnect to free connection slots
DISCONNECT_AFTER_SUCCESS = os.getenv("DISCONNECT_AFTER_SUCCESS", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Whether to probe paired devices if scan finds nothing in range
# Setting this to false will prevent false presence detection when scan fails
ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN = os.getenv(
    "ALLOW_PAIRED_PROBE_ON_EMPTY_SCAN", "false"
).lower() in ("1", "true", "yes")

# Cap reconnection attempts per cycle to avoid long stalls
MAX_RECONNECT_PER_CYCLE = int(os.getenv("MAX_RECONNECT_PER_CYCLE", "4"))

# Thread pool size for probing / verification work
THREAD_PROBE_WORKERS = max(1, int(os.getenv("THREAD_PROBE_WORKERS", "8")))

# Max concurrent Bluetooth connections during probing (Bluetooth adapters typically limit to 7 ACL connections)
PROBE_CONCURRENT_CONNECTIONS = max(1, int(os.getenv("PROBE_CONCURRENT_CONNECTIONS", "3")))

# Cache TTL for bluetoothctl info calls (seconds)
DEVICE_INFO_CACHE_SECONDS = int(os.getenv("DEVICE_INFO_CACHE_SECONDS", "5"))


class DeviceInfoCache:
    """Simple in-memory cache for bluetoothctl info responses."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = max(0, ttl_seconds)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._refreshes = 0

    def get(self, mac_address: str) -> Optional[str]:
        if self.ttl <= 0:
            return None
        now = time.time()
        with self._lock:
            entry = self._cache.get(mac_address)
            if not entry:
                self._misses += 1
                return None
            ts, value = entry
            if now - ts > self.ttl:
                self._cache.pop(mac_address, None)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, mac_address: str, data: str) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._cache[mac_address] = (time.time(), data)
            self._refreshes += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "refreshes": self._refreshes,
            }


_device_info_cache = DeviceInfoCache(DEVICE_INFO_CACHE_SECONDS)

logger.info(
    "Bluetooth scanner config -> connect_timeout=%ss, scan_window=%ss, "
    "scan_stagger=%ss, probe_workers=%s, probe_concurrent=%s, info_cache_ttl=%ss",
    CONNECT_TIMEOUT_SECONDS,
    IN_RANGE_SCAN_SECONDS,
    SCAN_STAGGER_SECONDS,
    THREAD_PROBE_WORKERS,
    PROBE_CONCURRENT_CONNECTIONS,
    DEVICE_INFO_CACHE_SECONDS,
)


def l2ping_device(mac_address: str, count: int = None, timeout: int = None) -> bool:
    """
    Ping a Bluetooth device using L2CAP without establishing a full connection.
    This is a passive detection method that works for paired devices.
    
    Note: l2ping typically requires root/sudo or CAP_NET_RAW capability.
    
    Args:
        mac_address: The MAC address of the device to ping
        count: Number of ping packets to send (default: L2PING_COUNT)
        timeout: Timeout in seconds for each ping (default: L2PING_TIMEOUT_SECONDS)
    
    Returns:
        True if device responds (is in range), False otherwise
    """
    if not _is_valid_mac(mac_address):
        logger.debug(f"Invalid MAC address for l2ping: {mac_address}")
        return False
    
    if count is None:
        count = L2PING_COUNT
    if timeout is None:
        timeout = L2PING_TIMEOUT_SECONDS
    
    try:
        result = subprocess.run(
            ["l2ping", "-c", str(count), "-t", str(timeout), mac_address],
            capture_output=True,
            text=True,
            timeout=timeout + 2,  # Allow extra time for process overhead
        )
        # l2ping returns 0 if device responds
        success = result.returncode == 0 and "bytes from" in result.stdout.lower()
        if success:
            logger.debug(f"l2ping success for {mac_address}")
        else:
            # Check for permission error
            stderr = result.stderr.strip().lower()
            if "permission" in stderr or "operation not permitted" in stderr:
                logger.warning(f"l2ping permission denied for {mac_address} - run with sudo or set CAP_NET_RAW")
            else:
                logger.debug(f"l2ping failed for {mac_address}: {result.stderr.strip() or result.stdout.strip()}")
        return success
    except subprocess.TimeoutExpired:
        logger.debug(f"l2ping timeout for {mac_address}")
        return False
    except FileNotFoundError:
        logger.error("l2ping not found. Install bluez package.")
        return False
    except Exception as e:
        logger.debug(f"l2ping error for {mac_address}: {e}")
        return False


def _l2ping_single(mac_address: str) -> tuple[str, bool]:
    """Helper for concurrent l2ping."""
    return (mac_address, l2ping_device(mac_address))


def batch_l2ping_devices(mac_addresses: list[str]) -> dict[str, bool]:
    """
    Ping multiple devices concurrently using l2ping.
    
    Args:
        mac_addresses: List of MAC addresses to ping
    
    Returns:
        Dictionary mapping MAC address to ping result (True=responded, False=no response)
    """
    results: dict[str, bool] = {}
    
    if not mac_addresses:
        return results
    
    max_workers = min(len(mac_addresses), L2PING_CONCURRENT_WORKERS)
    start = time.perf_counter()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_mac = {
            executor.submit(_l2ping_single, mac): mac
            for mac in mac_addresses
        }
        
        for future in as_completed(future_to_mac):
            mac = future_to_mac[future]
            try:
                _, success = future.result()
                results[mac] = success
            except Exception as e:
                logger.error(f"l2ping task failed for {mac}: {e}")
                results[mac] = False
    
    duration = time.perf_counter() - start
    successes = sum(1 for s in results.values() if s)
    logger.info(
        f"l2ping batch complete: {successes}/{len(results)} responded in {duration:.2f}s"
    )
    return results


def name_request_device(mac_address: str, timeout: int = None) -> bool:
    """
    Request device name without establishing a full connection.
    If the device responds with its name, it's in range.
    
    Args:
        mac_address: The MAC address of the device
        timeout: Timeout in seconds (default: NAME_REQUEST_TIMEOUT_SECONDS)
    
    Returns:
        True if device responds with name (is in range), False otherwise
    """
    if not _is_valid_mac(mac_address):
        logger.debug(f"Invalid MAC address for name request: {mac_address}")
        return False
    
    if timeout is None:
        timeout = NAME_REQUEST_TIMEOUT_SECONDS
    
    try:
        result = subprocess.run(
            ["hcitool", "name", mac_address],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # If we get a name back (non-empty stdout), device is in range
        name = result.stdout.strip()
        if name:
            logger.debug(f"Name request success for {mac_address}: {name}")
            return True
        logger.debug(f"Name request failed for {mac_address}: no name returned")
        return False
    except subprocess.TimeoutExpired:
        logger.debug(f"Name request timeout for {mac_address}")
        return False
    except FileNotFoundError:
        logger.error("hcitool not found. Install bluez package.")
        return False
    except Exception as e:
        logger.debug(f"Name request error for {mac_address}: {e}")
        return False


def _name_request_single(mac_address: str) -> tuple[str, bool]:
    """Helper for concurrent name requests."""
    return (mac_address, name_request_device(mac_address))


def batch_name_request_devices(mac_addresses: list[str]) -> dict[str, bool]:
    """
    Request names from multiple devices concurrently.
    
    Args:
        mac_addresses: List of MAC addresses to query
    
    Returns:
        Dictionary mapping MAC address to result (True=responded, False=no response)
    """
    results: dict[str, bool] = {}
    
    if not mac_addresses:
        return results
    
    max_workers = min(len(mac_addresses), THREAD_PROBE_WORKERS)
    start = time.perf_counter()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_mac = {
            executor.submit(_name_request_single, mac): mac
            for mac in mac_addresses
        }
        
        for future in as_completed(future_to_mac):
            mac = future_to_mac[future]
            try:
                _, success = future.result()
                results[mac] = success
            except Exception as e:
                logger.error(f"Name request task failed for {mac}: {e}")
                results[mac] = False
    
    duration = time.perf_counter() - start
    successes = sum(1 for s in results.values() if s)
    logger.debug(
        f"Name request batch complete: {successes}/{len(results)} responded in {duration:.2f}s"
    )
    return results


def detect_devices_passive(mac_addresses: list[str]) -> dict[str, tuple[bool, str]]:
    """
    Detect devices using passive methods (no full connection required).
    Uses a tiered approach: l2ping first, then name request for failures.
    
    Args:
        mac_addresses: List of MAC addresses to detect
    
    Returns:
        Dictionary mapping MAC address to (detected, method) tuple.
        method is one of: 'l2ping', 'name_request', 'none'
    """
    results: dict[str, tuple[bool, str]] = {}
    
    if not mac_addresses:
        return results
    
    # Phase 1: L2PING all devices (fastest)
    logger.info(f"Phase 1: L2PING {len(mac_addresses)} device(s)...")
    l2ping_results = batch_l2ping_devices(mac_addresses)
    
    detected_macs: set[str] = set()
    failed_macs: list[str] = []
    
    for mac, success in l2ping_results.items():
        if success:
            results[mac] = (True, "l2ping")
            detected_macs.add(mac)
        else:
            failed_macs.append(mac)
    
    # Phase 2: Name request for devices that didn't respond to l2ping
    if failed_macs:
        logger.info(f"Phase 2: Name request for {len(failed_macs)} device(s)...")
        name_results = batch_name_request_devices(failed_macs)
        
        for mac, success in name_results.items():
            if success:
                results[mac] = (True, "name_request")
                detected_macs.add(mac)
            else:
                results[mac] = (False, "none")
    
    logger.info(
        f"Passive detection complete: {len(detected_macs)}/{len(mac_addresses)} detected"
    )
    return results


def _bluetoothctl_info(mac_address: str) -> Optional[str]:
    """Fetch bluetoothctl info output, using the cache when possible."""
    if not _is_valid_mac(mac_address):
        logger.error(f"Invalid MAC address format: {mac_address}")
        return None
    
    cached = _device_info_cache.get(mac_address)
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout fetching info for {mac_address}")
        return None
    except FileNotFoundError:
        logger.error("bluetoothctl not found. Bluetooth may not be available.")
        return None
    except Exception as exc:
        logger.error(f"Error fetching info for {mac_address}: {exc}")
        return None

    if result.returncode != 0:
        logger.debug(f"Failed to fetch info for {mac_address}: {result.stderr.strip()}")
        return None

    _device_info_cache.set(mac_address, result.stdout)
    return result.stdout


def _device_info_indicates_in_range(info_output: str) -> bool:
    """
    Simplified range detection based only on connection status.
    
    We treat a device as "in range" only if it's currently connected.
    RSSI is unreliable and causes false positives, so we rely on actual connections.
    """
    # Only consider a device in range if it's actually connected
    if "Connected: yes" in info_output:
        logger.debug("Device is connected - considered in range")
        return True
        
    # Don't use RSSI as it's unreliable and causes false positives
    logger.debug("Device not connected - considered out of range")
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
    info = _bluetoothctl_info(mac_address)
    if not info:
        logger.debug(f"No bluetoothctl info available for {mac_address}")
        return False

    if _device_info_indicates_in_range(info):
        logger.debug(f"Device {mac_address} is connected (cache-backed)")
        return True

    logger.debug(f"Device {mac_address} is not connected (cache-backed)")
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
    info = _bluetoothctl_info(mac_address)
    if not info:
        logger.debug(f"No bluetoothctl info available when fetching name for {mac_address}")
        return None

    for line in info.split("\n"):
        if "Name:" in line:
            name = line.split(":", 1)[1].strip()
            if name:
                logger.info(f"✓ Device {mac_address} name found: '{name}'")
                return name

    logger.warning(f"✗ No Name field found for device {mac_address}")
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
    verification_results: dict[str, bool] = {}
    start_time = time.perf_counter()

    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            logger.error("Failed to get connected devices")
            return []

        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Device "):
                parts = line.split(" ", 2)
                if len(parts) >= 2:
                    mac_address = parts[1]
                    connected_devices.append(mac_address)

        if connected_devices:
            verification_results = verify_devices_connected(connected_devices)
            connected_devices = [mac for mac, ok in verification_results.items() if ok]
        duration = time.perf_counter() - start_time
        cache_snapshot = _device_info_cache.snapshot()
        logger.info(
            "Connected devices discovered=%s verified=%s in %.2fs (cache hits=%s misses=%s refreshes=%s)",
            len(verification_results) or len(connected_devices),
            len(connected_devices),
            duration,
            cache_snapshot.get("hits", 0),
            cache_snapshot.get("misses", 0),
            cache_snapshot.get("refreshes", 0),
        )
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
    if not _is_valid_mac(mac_address):
        logger.error(f"Invalid MAC address format: {mac_address}")
        return False
    
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


def _is_valid_mac(mac_address: str) -> bool:
    """Validate MAC address format (XX:XX:XX:XX:XX:XX)."""
    if not mac_address or len(mac_address) != 17:
        return False
    parts = mac_address.split(":")
    if len(parts) != 6:
        return False
    for part in parts:
        if len(part) != 2 or not all(c in "0123456789ABCDEFabcdef" for c in part):
            return False
    return True


def connect_device(mac_address: str) -> bool:
    """
    Attempt to connect to a Bluetooth device.

    Args:
        mac_address: The MAC address of the device to connect to

    Returns:
        True if connected successfully, False otherwise
    """
    if not _is_valid_mac(mac_address):
        logger.error(f"Invalid MAC address format: {mac_address}")
        return False
    
    try:
        # Trust device unconditionally (idempotent operation)
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
    if not _is_valid_mac(mac_address):
        logger.error(f"Invalid MAC address format: {mac_address}")
        return False
    
    try:
        logger.info(f"Disconnecting from {mac_address}...")
        result = subprocess.run(
            ["bluetoothctl", "disconnect", mac_address],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if "Successful disconnected" in result.stdout:
            logger.info(f"Successfully disconnected from {mac_address}")
            return True
        else:
            # Don't trust cache - verify directly with fresh check
            is_connected = _bluetoothctl_info(mac_address)
            if is_connected and "Connected: yes" in is_connected:
                logger.debug(f"Could not disconnect from {mac_address}: {result.stdout.strip()}")
                return False
            else:
                logger.info(f"Device {mac_address} already disconnected")
                return True

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
    if not _is_valid_mac(mac_address):
        logger.error(f"Invalid MAC address format: {mac_address}")
        return False
    
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
    Also performs periodic cleanup to prevent unbounded growth.
    """
    with _connection_state_lock:
        _last_connection_attempts[mac_address] = time.time()
        # Periodic cleanup: if dict is large, remove old entries
        if len(_last_connection_attempts) > 100:
            current_time = time.time()
            to_remove = [
                mac for mac, attempt_time in _last_connection_attempts.items()
                if current_time - attempt_time > RECENT_ATTEMPT_WINDOW
            ]
            for mac in to_remove:
                del _last_connection_attempts[mac]
            if to_remove:
                logger.debug(f"Cleaned up {len(to_remove)} old connection attempts")


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


def probe_devices(mac_addresses: list[str], disconnect_after: bool = True, use_passive_first: bool = True) -> dict[str, bool]:
    """
    Probe devices to detect presence.
    
    Uses a tiered approach:
    1. Passive detection (l2ping + name request) - fast, no connection required
    2. Connection probe (connect + disconnect) - fallback for devices not detected passively

    Args:
        mac_addresses: List of MAC addresses to probe.
        disconnect_after: Whether to disconnect after a successful connect.
        use_passive_first: If True, try passive detection before connection probes.

    Returns:
        Dictionary mapping MAC address to detection result.
    """
    results: dict[str, bool] = {}
    
    if not mac_addresses:
        return results

    start = time.perf_counter()
    logger.info(f"probe_devices called with {len(mac_addresses)} device(s), use_passive_first={use_passive_first}")
    
    # Phase 1: Passive detection (l2ping + name request)
    if use_passive_first:
        passive_results = detect_devices_passive(mac_addresses)
        
        devices_needing_probe: list[str] = []
        for mac, (detected, method) in passive_results.items():
            if detected:
                results[mac] = True
                logger.debug(f"Device {mac} detected via {method}")
            else:
                devices_needing_probe.append(mac)
        
        # If all devices detected passively, we're done
        if not devices_needing_probe:
            duration = time.perf_counter() - start
            logger.info(
                f"All {len(results)} device(s) detected passively in {duration:.2f}s"
            )
            return results
        
        logger.info(
            f"Passive detection found {len(results)}/{len(mac_addresses)} device(s). "
            f"Probing {len(devices_needing_probe)} remaining device(s)..."
        )
    else:
        devices_needing_probe = list(mac_addresses)
    
    # Phase 2: Connection probe for devices not detected passively
    if devices_needing_probe:
        max_workers = min(len(devices_needing_probe), PROBE_CONCURRENT_CONNECTIONS)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_mac = {
                executor.submit(_probe_single_device, mac_address, disconnect_after): mac_address
                for mac_address in devices_needing_probe
            }

            for future in as_completed(future_to_mac):
                mac_address = future_to_mac[future]
                try:
                    mac, success = future.result()
                    results[mac] = success
                except Exception as exc:
                    logger.error(f"Probe task failed for {mac_address}: {exc}")
                    results[mac_address] = False

    duration = time.perf_counter() - start
    successes = sum(1 for success in results.values() if success)
    logger.info(
        f"Probe complete: {successes}/{len(results)} detected in {duration:.2f}s"
    )
    return results


def verify_devices_connected(mac_addresses: list[str]) -> dict[str, bool]:
    """Concurrent helper to confirm device connectivity state."""

    results: dict[str, bool] = {}
    if not mac_addresses:
        return results

    start = time.perf_counter()
    max_workers = min(len(mac_addresses), THREAD_PROBE_WORKERS)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(check_device_connected, mac): mac for mac in mac_addresses
        }
        for future in as_completed(future_map):
            mac = future_map[future]
            try:
                results[mac] = future.result()
            except Exception as exc:
                logger.error(f"Error verifying device {mac}: {exc}")
                results[mac] = False

    duration = time.perf_counter() - start
    logger.debug(
        "Verified %s device(s) in %.2fs using %s worker(s)",
        len(results),
        duration,
        max_workers,
    )
    return results


def scan_for_devices_in_range() -> set[str]:
    """
    Scan for Bluetooth devices that are currently in range using connection-based detection only.

    This function no longer uses RSSI-based detection to avoid false positives.
    It only considers devices that are actually connected or can be successfully connected to.

    Returns:
        Set of MAC addresses of devices in range
    """
    devices_in_range: set[str] = set()

    # Method 1: Get currently connected devices (most reliable)
    try:
        logger.debug("Getting currently connected devices...")
        connected_devices = get_all_connected_devices()
        devices_in_range.update(connected_devices)
        logger.debug(f"Found {len(connected_devices)} connected device(s)")
    except Exception as e:
        logger.debug(f"Error getting connected devices: {e}")

    # Method 2: Staggered pybluez scans for rapid detection
    total_window = max(1, IN_RANGE_SCAN_SECONDS)
    chunk_seconds = max(1, min(SCAN_STAGGER_SECONDS, total_window))
    scan_deadline = time.time() + total_window

    while time.time() < scan_deadline:
        try:
            remaining = max(1, int(scan_deadline - time.time()))
            duration = min(chunk_seconds, remaining)
            logger.debug(
                "Scanning for discoverable devices using pybluez (chunk=%ss)...",
                duration,
            )
            discovered_devices = bluetooth.discover_devices(
                duration=duration, lookup_names=False, flush_cache=True
            )
            devices_in_range.update(discovered_devices)
            logger.debug(f"Chunk discovered {len(discovered_devices)} device(s)")
        except bluetooth.BluetoothError as e:
            logger.debug(f"Pybluez scan error: {e}")
            break
        except Exception as e:
            logger.debug(f"Unexpected error during pybluez scan: {e}")
            break
        if len(discovered_devices) == 0 and duration < chunk_seconds:
            break

    logger.debug(f"Total devices in range (connection-based): {len(devices_in_range)}")
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
