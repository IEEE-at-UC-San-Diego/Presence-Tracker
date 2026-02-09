import subprocess
import logging
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from threading import Lock
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

# How long to wait for bluetoothctl connect attempts (seconds)
CONNECT_TIMEOUT_SECONDS = int(os.getenv("CONNECT_TIMEOUT_SECONDS", "10"))

# Connect-probe fallback: quick connect attempt for devices that ignore l2ping
# Many devices (especially some Android phones) don't respond to L2CAP echo
# but briefly show Connected: yes on a connect attempt, confirming presence.
CONNECT_PROBE_FALLBACK = os.getenv("CONNECT_PROBE_FALLBACK", "true").lower() in ("1", "true", "yes", "on")
CONNECT_PROBE_TIMEOUT_SECONDS = int(os.getenv("CONNECT_PROBE_TIMEOUT_SECONDS", "3"))
CONNECT_PROBE_MAX_PER_BATCH = int(os.getenv("CONNECT_PROBE_MAX_PER_BATCH", "2"))

# After this many consecutive l2ping failures a device is flagged as
# "l2ping-resistant" and future probes skip l2ping, going straight to
# connect_probe.  A single l2ping success resets the counter.
L2PING_RESIST_THRESHOLD = int(os.getenv("L2PING_RESIST_THRESHOLD", "3"))

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

# Serialises bluetoothctl disconnect calls so parallel l2ping threads
# don't issue overlapping commands to the BlueZ daemon.
_disconnect_lock = Lock()

# Tracks consecutive l2ping failures per MAC.  Once a device exceeds
# L2PING_RESIST_THRESHOLD it is probed via connect_probe instead.
_l2ping_fail_count: dict[str, int] = {}
_l2ping_fail_lock = Lock()


def _record_l2ping_result(mac: str, success: bool) -> None:
    """Update the consecutive-failure counter for *mac*."""
    with _l2ping_fail_lock:
        if success:
            _l2ping_fail_count.pop(mac, None)
        else:
            _l2ping_fail_count[mac] = _l2ping_fail_count.get(mac, 0) + 1


def is_l2ping_resistant(mac: str) -> bool:
    """Return True if *mac* has failed l2ping enough times to skip it."""
    with _l2ping_fail_lock:
        return _l2ping_fail_count.get(mac, 0) >= L2PING_RESIST_THRESHOLD

logger.info(
    "Bluetooth scanner config -> connect_timeout=%ss, l2ping_timeout=%ss, "
    "l2ping_count=%s, info_cache_ttl=%ss",
    CONNECT_TIMEOUT_SECONDS,
    L2PING_TIMEOUT_SECONDS,
    L2PING_COUNT,
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
    ``bluetoothctl connect`` attempt — even if the connection ultimately
    fails.  We look for that brief ``Connected: yes`` in the output to
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
        # "Connected: yes" appears even on connections that are later canceled
        if "Connected: yes" in output or "Connection successful" in output:
            logger.debug("connect_probe success for %s (saw Connected: yes)", mac_address)
            return True
        logger.debug("connect_probe failed for %s: %s", mac_address, output.strip()[:120])
        return False
    except subprocess.TimeoutExpired:
        logger.debug("connect_probe timeout for %s", mac_address)
        return False
    except Exception as exc:
        logger.debug("connect_probe error for %s: %s", mac_address, exc)
        return False


def run_l2ping_cycle(mac_addresses: list[str]) -> dict[str, bool]:
    """Sequentially l2ping each MAC address and report presence."""
    results: dict[str, bool] = {}

    if not mac_addresses:
        return results

    start = time.perf_counter()
    logger.info(f"Running l2ping cycle for {len(mac_addresses)} device(s)...")

    successes = 0
    for mac in mac_addresses:
        success = l2ping_device(mac)
        results[mac] = success
        if success:
            successes += 1

    duration = time.perf_counter() - start
    logger.info(
        f"l2ping cycle complete: {successes}/{len(results)} responded in {duration:.2f}s"
    )
    return results


def l2ping_batch(
    mac_addresses: list[str],
    max_count: int | None = None,
    disconnect_after: bool = True,
) -> dict[str, bool]:
    """l2ping a subset of MACs sequentially, optionally disconnecting after each success.

    Args:
        mac_addresses: Full list of MACs to probe.
        max_count: If set, only probe the first *max_count* MACs.
        disconnect_after: If True, disconnect each device immediately after a
            successful l2ping to free the ACL slot for the next device.

    Returns:
        Dict mapping each probed MAC to its result (True = responded).
    """
    if max_count is not None and max_count > 0:
        mac_addresses = mac_addresses[:max_count]

    if not mac_addresses:
        return {}

    start = time.perf_counter()
    logger.info("l2ping_batch: probing %d device(s)...", len(mac_addresses))

    results: dict[str, bool] = {}
    successes = 0
    probe_candidates: list[str] = []  # devices that need connect-probe

    # Phase 1: l2ping (skip l2ping-resistant devices)
    for mac in mac_addresses:
        if is_l2ping_resistant(mac):
            # Skip l2ping entirely — go straight to connect-probe later
            probe_candidates.append(mac)
            continue
        success = l2ping_device(mac)
        _record_l2ping_result(mac, success)
        results[mac] = success
        if success:
            successes += 1
            if disconnect_after:
                disconnect_device(mac)
        else:
            probe_candidates.append(mac)

    # Phase 2: connect-probe for failures + l2ping-resistant devices
    fallback_hits = 0
    fallback_tried = 0
    if CONNECT_PROBE_FALLBACK and probe_candidates:
        for mac in probe_candidates:
            if fallback_tried >= CONNECT_PROBE_MAX_PER_BATCH:
                # Cap reached — mark remaining as not-seen
                results.setdefault(mac, False)
                continue
            fallback_tried += 1
            success = connect_probe(mac)
            _record_l2ping_result(mac, success)  # reset counter on success
            results[mac] = success
            if success:
                fallback_hits += 1
                successes += 1
                if disconnect_after:
                    disconnect_device(mac)
    else:
        # No fallback — mark probe_candidates as failed
        for mac in probe_candidates:
            results.setdefault(mac, False)

    duration = time.perf_counter() - start
    fallback_msg = ""
    if fallback_tried:
        fallback_msg = " (connect-probe: %d/%d)" % (fallback_hits, fallback_tried)
    logger.info(
        "l2ping_batch complete: %d/%d responded in %.2fs%s",
        successes,
        len(results),
        duration,
        fallback_msg,
    )
    return results


def l2ping_parallel(
    tier_lists: list[list[str]],
    disconnect_after: bool = True,
    max_workers: int = 3,
) -> dict[str, bool]:
    """Run l2ping_batch on multiple disjoint MAC lists in parallel threads.

    Each list is processed sequentially within its own thread, but the
    threads run concurrently.  This keeps total simultaneous ACL
    connections equal to ``max_workers`` (default 3), well within the
    RPi5 adapter limit of ~7.

    Args:
        tier_lists: Up to *max_workers* disjoint lists of MAC addresses.
            Empty lists are silently skipped.
        disconnect_after: Passed through to ``l2ping_batch``.
        max_workers: Number of parallel threads.  Clamped to [1, 5] to
            stay within the Bluetooth ACL connection limit.

    Returns:
        Merged dict mapping every probed MAC to its result.
    """
    # Clamp workers to safe range (leave 2 ACL slots for fast-path events)
    max_workers = max(1, min(max_workers, 5))

    # Filter out empty lists
    non_empty = [lst for lst in tier_lists if lst]
    if not non_empty:
        return {}

    # If only 1 list or 1 worker, fall back to sequential
    if max_workers == 1 or len(non_empty) == 1:
        merged: dict[str, bool] = {}
        for lst in non_empty:
            merged.update(l2ping_batch(lst, disconnect_after=disconnect_after))
        return merged

    start = time.perf_counter()
    total_macs = sum(len(lst) for lst in non_empty)
    logger.info(
        "l2ping_parallel: %d tier(s), %d device(s), %d thread(s)",
        len(non_empty),
        total_macs,
        min(max_workers, len(non_empty)),
    )

    merged = {}
    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(non_empty)),
        thread_name_prefix="l2ping",
    ) as pool:
        futures = {
            pool.submit(l2ping_batch, lst, disconnect_after=disconnect_after): idx
            for idx, lst in enumerate(non_empty)
        }
        for future in as_completed(futures):
            tier_idx = futures[future]
            try:
                result = future.result()
                merged.update(result)
            except Exception as exc:
                logger.error(
                    "l2ping_parallel: tier %d raised %s — treating as all-absent",
                    tier_idx,
                    exc,
                )
                # Mark all MACs in this tier as absent
                for mac in non_empty[tier_idx]:
                    merged.setdefault(mac, False)

    duration = time.perf_counter() - start
    successes = sum(1 for v in merged.values() if v)
    logger.info(
        "l2ping_parallel complete: %d/%d responded in %.2fs (%d thread(s))",
        successes,
        len(merged),
        duration,
        min(max_workers, len(non_empty)),
    )
    return merged


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

    Thread-safe: uses ``_disconnect_lock`` so parallel l2ping threads
    don't issue overlapping ``bluetoothctl disconnect`` commands.

    Args:
        mac_address: The MAC address of the device to disconnect

    Returns:
        True if disconnected successfully, False otherwise
    """
    if not _is_valid_mac(mac_address):
        logger.error(f"Invalid MAC address format: {mac_address}")
        return False
    
    with _disconnect_lock:
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
