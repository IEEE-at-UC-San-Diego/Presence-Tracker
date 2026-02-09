import subprocess
import logging
from typing import Optional
import time
import os

from logging_utils import configure_logger

logger = configure_logger(
    logging.getLogger(__name__),
    log_filename="bluetooth_scanner.log",
    level=logging.DEBUG,
)

# L2PING configuration for passive detection
L2PING_TIMEOUT_SECONDS = int(os.getenv("L2PING_TIMEOUT_SECONDS", "2"))
L2PING_COUNT = int(os.getenv("L2PING_COUNT", "1"))

# Connect-probe fallback (sequential): quick connect attempt for devices
# that fail l2ping.  Runs after all l2pings complete.
CONNECT_PROBE_TIMEOUT_SECONDS = int(os.getenv("CONNECT_PROBE_TIMEOUT_SECONDS", "3"))

# Cache TTL for bluetoothctl info calls (seconds)
DEVICE_INFO_CACHE_SECONDS = int(os.getenv("DEVICE_INFO_CACHE_SECONDS", "5"))


class DeviceInfoCache:
    """Simple in-memory cache for bluetoothctl info responses."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = max(0, ttl_seconds)
        self._cache: dict[str, tuple[float, str]] = {}
        self._hits = 0
        self._misses = 0
        self._refreshes = 0

    def get(self, mac_address: str) -> Optional[str]:
        if self.ttl <= 0:
            return None
        now = time.time()
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
        self._cache[mac_address] = (time.time(), data)
        self._refreshes += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "refreshes": self._refreshes,
        }


_device_info_cache = DeviceInfoCache(DEVICE_INFO_CACHE_SECONDS)

logger.info(
    "Bluetooth scanner config -> l2ping_timeout=%ss, l2ping_count=%s, "
    "connect_probe_timeout=%ss, info_cache_ttl=%ss",
    L2PING_TIMEOUT_SECONDS,
    L2PING_COUNT,
    CONNECT_PROBE_TIMEOUT_SECONDS,
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
            timeout=timeout + 1,  # Allow extra time for process overhead
        )
        # l2ping returns 0 if device responds
        success = result.returncode == 0 and "bytes from" in result.stdout.lower()
        if success:
            logger.debug(f"l2ping success for {mac_address}")
        else:
            # Check for common errors
            stderr = result.stderr.strip().lower()
            if "permission" in stderr or "operation not permitted" in stderr:
                logger.warning(f"l2ping permission denied for {mac_address} - run with sudo or set CAP_NET_RAW")
            elif "too many links" in stderr:
                logger.debug(f"l2ping failed for {mac_address}: Bluetooth adapter connection limit reached")
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


def connect_probe(mac_address: str) -> bool:
    """Quick connect attempt to detect presence for devices that ignore l2ping.

    Some devices (especially certain Android phones) don't respond to L2CAP
    echo requests but will briefly show ``Connected: yes`` during a
    ``bluetoothctl connect`` attempt.  We look for that indicator to
    confirm the device is in range.

    Args:
        mac_address: MAC address to probe.

    Returns:
        True if the device showed any sign of being reachable.
    """
    if not _is_valid_mac(mac_address):
        return False

    try:
        result = subprocess.run(
            ["bluetoothctl", "connect", mac_address],
            capture_output=True,
            text=True,
            timeout=CONNECT_PROBE_TIMEOUT_SECONDS,
        )
        output = result.stdout
        if "Connected: yes" in output or "Connection successful" in output:
            logger.debug("connect_probe success for %s", mac_address)
            return True
        logger.debug("connect_probe failed for %s: %s", mac_address, output.strip()[:120])
        return False
    except subprocess.TimeoutExpired:
        logger.debug("connect_probe timeout for %s", mac_address)
        return False
    except Exception as exc:
        logger.debug("connect_probe error for %s: %s", mac_address, exc)
        return False


def l2ping_batch(
    mac_addresses: list[str],
    max_count: int | None = None,
) -> dict[str, bool]:
    """Probe devices sequentially: l2ping each, then connect-probe failures.

    All probing is sequential — only one l2ping / connect process runs at
    a time to avoid HCI contention.  Every device is disconnected at the
    end regardless of result.

    Args:
        mac_addresses: MACs to probe.
        max_count: If set, only probe the first *max_count* MACs.

    Returns:
        Dict mapping each probed MAC to True (present) or False (absent).
    """
    if max_count is not None and max_count > 0:
        mac_addresses = mac_addresses[:max_count]

    if not mac_addresses:
        return {}

    start = time.perf_counter()
    logger.info("l2ping_batch: probing %d device(s)...", len(mac_addresses))

    results: dict[str, bool] = {}
    successes = 0
    l2ping_failures: list[str] = []

    # Phase 1: l2ping every device sequentially, disconnect after each
    for mac in mac_addresses:
        success = l2ping_device(mac)
        results[mac] = success
        if success:
            successes += 1
            disconnect_device(mac)
        else:
            l2ping_failures.append(mac)

    # Phase 2: connect-probe every l2ping failure, disconnect immediately
    probe_hits = 0
    for mac in l2ping_failures:
        success = connect_probe(mac)
        if success:
            results[mac] = True
            successes += 1
            probe_hits += 1
        disconnect_device(mac)

    duration = time.perf_counter() - start
    probe_msg = ""
    if l2ping_failures:
        probe_msg = " (connect-probe: %d/%d)" % (probe_hits, len(l2ping_failures))
    logger.info(
        "l2ping_batch complete: %d/%d responded in %.2fs%s",
        successes,
        len(results),
        duration,
        probe_msg,
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

        duration = time.perf_counter() - start_time
        cache_snapshot = _device_info_cache.snapshot()
        logger.info(
            "Connected devices discovered=%s in %.2fs (cache hits=%s misses=%s refreshes=%s)",
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


def disconnect_all_connected(connected_macs: list[str] | None = None) -> int:
    """Disconnect all currently connected devices to free ACL slots.

    Args:
        connected_macs: Optional pre-fetched list of connected MACs.
            If *None*, queries bluetoothctl for the current list.

    Returns:
        Number of devices successfully disconnected.
    """
    if connected_macs is None:
        connected_macs = get_all_connected_devices()
    if not connected_macs:
        return 0

    count = 0
    for mac in connected_macs:
        if disconnect_device(mac):
            count += 1
    if count:
        logger.info("Disconnected %d/%d connected device(s) to free ACL slots", count, len(connected_macs))
    return count


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
