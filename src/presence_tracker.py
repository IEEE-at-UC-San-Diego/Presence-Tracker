import os
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Any, Optional
import convex
from dotenv import load_dotenv
import bluetooth_scanner
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        # Rotate log after ~100KB (approx 500 lines), keep 1 backup
        RotatingFileHandler("presence_tracker.log", maxBytes=100000, backupCount=1),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Get Convex deployment URL from environment
CONVEX_DEPLOYMENT_URL = os.getenv("CONVEX_DEPLOYMENT_URL")
CONVEX_SELF_HOSTED_URL = os.getenv("CONVEX_SELF_HOSTED_URL")
CONVEX_SELF_HOSTED_ADMIN_KEY = os.getenv("CONVEX_SELF_HOSTED_ADMIN_KEY")
DEPLOYMENT_URL = CONVEX_SELF_HOSTED_URL or CONVEX_DEPLOYMENT_URL

if not DEPLOYMENT_URL:
    raise ValueError(
        "CONVEX_DEPLOYMENT_URL or CONVEX_SELF_HOSTED_URL environment variable is not set. "
        "Please create a .env file with one of these variables."
    )

# Convex client will be initialized lazily to avoid startup hangs
_convex_client: convex.ConvexClient | None = None

def get_convex_client() -> convex.ConvexClient:
    """Get or initialize the Convex client."""
    global _convex_client
    if _convex_client is None:
        logger.info("Initializing Convex client...")
        _convex_client = convex.ConvexClient(DEPLOYMENT_URL)
        if CONVEX_SELF_HOSTED_ADMIN_KEY:
            _convex_client.client.set_admin_auth(CONVEX_SELF_HOSTED_ADMIN_KEY)
        logger.info("Convex client initialized")
    return _convex_client

# Polling interval in seconds
POLLING_INTERVAL = 5

# Grace period for new device registration in seconds
GRACE_PERIOD_SECONDS = int(os.getenv("GRACE_PERIOD_SECONDS", "300"))

# Presence TTL for recently seen devices (seconds)
# Increased to 45s to give more grace period with passive detection
PRESENT_TTL_SECONDS = int(os.getenv("PRESENT_TTL_SECONDS", "45"))

# Require at least one positive presence signal per cycle before marking any device absent
REQUIRE_PRESENCE_SIGNAL_FOR_ABSENCE = os.getenv(
    "REQUIRE_PRESENCE_SIGNAL_FOR_ABSENCE", "true"
).lower() in ("1", "true", "yes")

# Require N consecutive absence detections before flipping a device to absent
ABSENCE_HYSTERESIS_CYCLES = max(1, int(os.getenv("ABSENCE_HYSTERESIS_CYCLES", "2")))

# Allow absence flips after this many consecutive "all silent" cycles (0 means don't wait)
ALL_SILENT_ABSENCE_CYCLES = max(0, int(os.getenv("ALL_SILENT_ABSENCE_CYCLES", "2")))

# Grace period for newly registered devices to enter polling cycle (seconds)
# Ensures first-time registered devices are immediately tracked for connect/disconnect
NEWLY_REGISTERED_GRACE_PERIOD = int(os.getenv("NEWLY_REGISTERED_GRACE_PERIOD", "120"))

# Additional presence smoothing configuration
PRESENCE_RECHECK_INTERVAL_SECONDS = int(
    os.getenv("PRESENCE_RECHECK_INTERVAL_SECONDS", "30")
)
RECHECK_RESULT_TTL_SECONDS = int(os.getenv("RECHECK_RESULT_TTL_SECONDS", "60"))
PRESENCE_SIGNAL_DECAY_SECONDS = max(
    1, int(os.getenv("PRESENCE_SIGNAL_DECAY_SECONDS", "120"))
)
PRESENCE_CONFIDENCE_MIN = float(os.getenv("PRESENCE_CONFIDENCE_MIN", "0.4"))

# Full probe interval (seconds): attempt connect+disconnect to each device
FULL_PROBE_ENABLED = os.getenv("FULL_PROBE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
FULL_PROBE_INTERVAL_SECONDS = int(os.getenv("FULL_PROBE_INTERVAL_SECONDS", "60"))
FULL_PROBE_DISCONNECT_AFTER = os.getenv("FULL_PROBE_DISCONNECT_AFTER", "true").lower() in (
    "1",
    "true",
    "yes",
)



# Disconnect connected devices after each cycle to free connection slots
DISCONNECT_CONNECTED_AFTER_CYCLE = os.getenv("DISCONNECT_CONNECTED_AFTER_CYCLE", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Track devices that failed to register, so we retry them
failed_registrations: set[str] = set()

# Track consecutive failed connection attempts per device
failed_connection_attempts: dict[str, int] = {}

# Track previous status of each device for deduplication
device_previous_status: dict[str, str] = {}

# Track devices seen recently to smooth presence when disconnecting after checks
recently_seen_devices: dict[str, float] = {}

# Track the last time we recorded any positive signal per device (not pruned with TTL)
last_presence_signal: dict[str, float] = {}

# Track consecutive positive detections for each device (for confirmation requirement)
consecutive_positive_detections: dict[str, int] = {}

# Number of consecutive positive detections required before marking device as present
# Reduced to 1 since l2ping passive detection is reliable and fast
POSITIVE_DETECTION_CONFIRMATIONS = max(1, int(os.getenv("POSITIVE_DETECTION_CONFIRMATIONS", "1")))

# Track consecutive cycles where a device was missing from the presence set
absence_miss_streaks: dict[str, int] = {}

# Pending and recent absence recheck tracking
pending_absence_rechecks: dict[str, float] = {}
recent_recheck_results: dict[str, tuple[float, bool]] = {}
device_confidence_scores: dict[str, float] = {}

# Track consecutive polling cycles where no presence signals were observed
silent_cycle_streak = 0

# Track when the last full probe ran
last_full_probe_time = 0.0

# Threshold for consecutive failed connections before backing off
FAILED_CONNECTION_THRESHOLD = 3

# Timeout for Convex queries in seconds
CONVEX_QUERY_TIMEOUT = 10

# Track if Convex is responsive (skip if too many timeouts)
convex_responsive = True
consecutive_timeouts = 0
MAX_CONSECUTIVE_TIMEOUTS = 3


def _prune_device_state(registered_macs: set[str], now: float) -> None:
    """Remove state for devices that are no longer registered."""
    global failed_connection_attempts, device_previous_status
    global last_presence_signal, consecutive_positive_detections
    global absence_miss_streaks, device_confidence_scores
    
    # Find MACs to remove (present in state but not in registered devices)
    all_state_keys = (
        set(failed_connection_attempts.keys()) |
        set(device_previous_status.keys()) |
        set(last_presence_signal.keys()) |
        set(consecutive_positive_detections.keys()) |
        set(absence_miss_streaks.keys()) |
        set(device_confidence_scores.keys())
    )
    
    to_remove = all_state_keys - registered_macs
    
    for mac in to_remove:
        failed_connection_attempts.pop(mac, None)
        device_previous_status.pop(mac, None)
        last_presence_signal.pop(mac, None)
        consecutive_positive_detections.pop(mac, None)
        absence_miss_streaks.pop(mac, None)
        device_confidence_scores.pop(mac, None)
    
    if to_remove:
        logger.debug(f"Pruned state for {len(to_remove)} removed device(s)")


def _prune_recheck_state(now: float) -> None:
    """Remove expired recheck state to avoid stale decisions."""

    expired = [
        mac
        for mac, (ts, _) in recent_recheck_results.items()
        if now - ts > RECHECK_RESULT_TTL_SECONDS
    ]
    for mac in expired:
        recent_recheck_results.pop(mac, None)


def _should_attempt_recheck(
    mac_address: str, now: float, current_status: Optional[str]
) -> bool:
    """Determine whether a device should trigger an immediate bluetoothctl recheck."""

    last_request = pending_absence_rechecks.get(mac_address)
    if last_request and now - last_request < PRESENCE_RECHECK_INTERVAL_SECONDS:
        return False

    last_recheck = recent_recheck_results.get(mac_address)
    if last_recheck and now - last_recheck[0] <= RECHECK_RESULT_TTL_SECONDS:
        return False

    # Only recheck devices that were recently seen or are still marked present
    if current_status == "present":
        return True

    last_signal = last_presence_signal.get(mac_address)
    if last_signal and now - last_signal <= PRESENCE_SIGNAL_DECAY_SECONDS:
        return True

    streak = absence_miss_streaks.get(mac_address, 0)
    return streak < ABSENCE_HYSTERESIS_CYCLES


def _perform_absence_rechecks(candidates: set[str]) -> dict[str, bool]:
    """Run concurrent rechecks for candidate devices and record results."""

    if not candidates:
        return {}

    results = bluetooth_scanner.verify_devices_connected(sorted(candidates))
    timestamp = time.time()
    for mac, success in results.items():
        pending_absence_rechecks[mac] = timestamp
        recent_recheck_results[mac] = (timestamp, success)
    return results


def _calculate_presence_confidence(
    mac_address: str, now: float, signal_detected: bool
) -> float:
    """Compute a confidence score [0-1] for a device being present."""

    if signal_detected:
        score = 1.0
    else:
        score = 0.0
        last_signal = last_presence_signal.get(mac_address)
        if last_signal:
            age = now - last_signal
            if age <= PRESENCE_SIGNAL_DECAY_SECONDS:
                score = max(score, 1 - (age / PRESENCE_SIGNAL_DECAY_SECONDS))

        recheck_entry = recent_recheck_results.get(mac_address)
        if recheck_entry:
            ts, success = recheck_entry
            if now - ts <= RECHECK_RESULT_TTL_SECONDS:
                if success:
                    score = max(score, 0.9)
                else:
                    score = min(score, 0.1)

    device_confidence_scores[mac_address] = score
    return score


def get_known_devices() -> list[dict[str, Any]]:
    """
    Fetch all known devices from Convex using the getDevices function.
    Uses a thread executor to enforce timeout.

    Returns:
        List of device dictionaries with macAddress, name, status, and lastSeen fields
    """
    global convex_responsive, consecutive_timeouts
    
    def _query():
        return get_convex_client().query("devices:getDevices")
    
    if not convex_responsive and consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
        logger.warning(f"Convex temporarily unavailable ({consecutive_timeouts} consecutive timeouts), skipping query")
        return []
    
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_query)
            result = future.result(timeout=CONVEX_QUERY_TIMEOUT)
            logger.info(f"Retrieved {len(result)} devices from Convex")
            # Reset timeout counter on success
            if consecutive_timeouts > 0:
                consecutive_timeouts = 0
                convex_responsive = True
                logger.info("Convex connection recovered")
            return result
    except TimeoutError:
        consecutive_timeouts += 1
        if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            convex_responsive = False
            logger.error(f"Convex query timed out ({consecutive_timeouts}x) - entering circuit breaker mode")
        else:
            logger.error(f"Convex query timed out after {CONVEX_QUERY_TIMEOUT} seconds ({consecutive_timeouts}/{MAX_CONSECUTIVE_TIMEOUTS})")
        return []
    except Exception as e:
        logger.error(f"Error fetching devices from Convex: {e}")
        return []


def get_device_by_mac(mac_address: str) -> dict[str, Any] | None:
    """
    Find a device by MAC address in the Convex database.

    Args:
        mac_address: The MAC address to search for

    Returns:
        Device dictionary if found, None otherwise
    """
    try:
        devices = get_known_devices()
        for device in devices:
            if device.get("macAddress") == mac_address:
                return device
        return None
    except Exception as e:
        logger.error(f"Error finding device {mac_address}: {e}")
        return None


def register_new_device(mac_address: str, name: str | None = None) -> bool:
    """
    Register a new device in Convex with grace period.

    New devices are registered in pending state, giving them time to be
    properly named before being tracked for presence.

    Args:
        mac_address: The MAC address of the device
        name: Optional device name from Bluetooth scan

    Returns:
        True if registration was successful, False otherwise
    """
    def _mutation():
        return get_convex_client().mutation(
            "devices:registerPendingDevice",
            {
                "macAddress": mac_address,
                "name": name or "",
            },
        )
    
    try:
        logger.info(f"→ register_new_device called: mac={mac_address}, name='{name}'")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_mutation)
            result = future.result(timeout=CONVEX_QUERY_TIMEOUT)
            logger.info(
                f"✓ Registered new device {mac_address} (name='{name or 'unknown'}') in pending state -> {result}"
            )
            return True
    except TimeoutError:
        logger.error(f"✗ Convex mutation timed out after {CONVEX_QUERY_TIMEOUT} seconds for {mac_address}")
        logger.info(f"  Device {mac_address} will be retried on next polling cycle")
        return False
    except Exception as e:
        logger.error(f"✗ Error registering new device {mac_address}: {e}")
        logger.info(f"  Device {mac_address} will be retried on next polling cycle")
        return False


def cleanup_expired_devices() -> bool:
    """
    Clean up devices whose grace period has expired and are still pending.
    Also disconnects and removes the Bluetooth pairing for those devices.

    Returns:
        True if cleanup was successful, False otherwise
    """
    global failed_registrations
    
    def _action():
        return get_convex_client().action("devices:cleanupExpiredGracePeriods", {})
    
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_action)
            result = future.result(timeout=CONVEX_QUERY_TIMEOUT)
            deleted_count = result.get("deletedCount", 0)
            deleted_macs = result.get("deletedMacs", [])
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired grace period(s)")
                
                # Disconnect and remove Bluetooth pairing for deleted devices
                for mac_address in deleted_macs:
                    try:
                        logger.info(f"Disconnecting and removing expired device: {mac_address}")
                        bluetooth_scanner.disconnect_device(mac_address)
                        bluetooth_scanner.remove_device(mac_address)
                        logger.info(f"Successfully removed Bluetooth pairing for: {mac_address}")
                        # Remove from failed registrations since device was cleaned up
                        failed_registrations.discard(mac_address)
                    except Exception as e:
                        logger.error(f"Error removing Bluetooth device {mac_address}: {e}")
            else:
                logger.debug("No expired grace periods to clean up")
                
            return True
    except TimeoutError:
        logger.error(f"Convex action timed out after {CONVEX_QUERY_TIMEOUT} seconds")
        return False
    except Exception as e:
        logger.error(f"Error cleaning up expired devices: {e}")
        return False


def cleanup_stale_bluetooth_pairings() -> None:
    """
    Remove Bluetooth pairings for devices that are no longer in the Convex database.
    Only runs during normal polling cycles, not at startup to avoid hangs.
    """
    try:
        # Get all paired devices from Bluetooth
        paired_devices = bluetooth_scanner.get_paired_devices()
        paired_set = set(paired_devices)
        logger.debug(f"Found {len(paired_set)} paired device(s) in Bluetooth")

        # Do not remove devices that are currently connected.
        connected_devices = bluetooth_scanner.get_all_connected_devices()
        connected_set = set(connected_devices)
        if connected_set:
            logger.debug(f"Found {len(connected_set)} connected device(s) in Bluetooth")

        # Get all known devices from Convex (with timeout)
        convex_devices = get_known_devices()
        if not convex_devices:
            logger.debug("Skipping stale cleanup - no devices retrieved from Convex")
            return
            
        convex_macs = {device.get("macAddress") for device in convex_devices if device.get("macAddress")}
        logger.debug(f"Found {len(convex_macs)} device(s) in Convex database")

        # Find devices that are paired but not in Convex
        stale_devices = paired_set - convex_macs - connected_set

        if stale_devices:
            logger.info(f"Found {len(stale_devices)} stale Bluetooth pairing(s) to clean up")
            for mac_address in stale_devices:
                try:
                    bluetooth_scanner.remove_device(mac_address)
                    logger.info(f"Removed stale Bluetooth pairing: {mac_address}")
                except Exception as e:
                    logger.error(f"Failed to remove stale pairing {mac_address}: {e}")
        else:
            logger.debug("No stale Bluetooth pairings found")
    except Exception as e:
        logger.error(f"Error during stale Bluetooth pairing cleanup: {e}")


def scan_all_connected_devices() -> list[str]:
    """
    Get all currently connected Bluetooth devices.

    Returns:
        List of MAC addresses of connected devices
    """
    return bluetooth_scanner.get_all_connected_devices()


def log_attendance(mac_address: str, name: str, status: str) -> bool:
    """
    Log attendance change to Convex using the logAttendance function.

    Args:
        mac_address: The MAC address of the device
        name: The display name of the device/user
        status: Either "present" or "absent"

    Returns:
        True if log was successful, False otherwise
    """
    def _mutation():
        return get_convex_client().mutation(
            "devices:logAttendance",
            {
                "userId": mac_address,
                "userName": name,
                "status": status,
                "deviceId": mac_address,
            },
        )
    
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_mutation)
            future.result(timeout=CONVEX_QUERY_TIMEOUT)
        logger.info(f"✓ Logged attendance: {name} -> {status}")
        return True
    except TimeoutError:
        logger.error(f"✗ Convex mutation timed out after {CONVEX_QUERY_TIMEOUT} seconds for attendance logging")
        logger.info(f"  Attendance logging will be retried on next polling cycle")
        return False
    except Exception as e:
        logger.error(f"✗ Error logging attendance for {mac_address}: {e}")
        logger.info(f"  Attendance logging will be retried on next polling cycle")
        return False


def update_device_status(
    mac_address: str, is_connected: bool, current_status: str | None = None,
    device_info: dict[str, Any] | None = None
) -> bool:
    """
    Update a device's status in Convex using the updateDeviceStatus function.
    Only logs attendance if the status has actually changed (deduplication).

    Args:
        mac_address: The MAC address of the device to update
        is_connected: True if the device is present (connected), False if absent
        current_status: Optional current status to avoid extra lookup
        device_info: Optional device dict to avoid redundant Convex query

    Returns:
        True if the update was successful, False otherwise
    """
    def _mutation():
        return get_convex_client().mutation(
            "devices:updateDeviceStatus", {"macAddress": mac_address, "status": new_status}
        )
    
    try:
        new_status = "present" if is_connected else "absent"
        previous_status = (
            current_status if current_status is not None else device_previous_status.get(mac_address)
        )

        # Only update Convex when status changes
        if previous_status == new_status:
            logger.debug(
                f"Status unchanged for {mac_address}: {new_status} (skipping Convex update)"
            )
            device_previous_status[mac_address] = new_status
            return True

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_mutation)
            result = future.result(timeout=CONVEX_QUERY_TIMEOUT)
            logger.info(f"Updated device {mac_address} status to {new_status} -> {result}")

        # Log attendance only for registered devices (not pending)
        # Use device_info if provided, otherwise fetch from Convex
        device = device_info if device_info is not None else get_device_by_mac(mac_address)
        if device and not device.get("pendingRegistration"):
            name = device.get("name", mac_address)
            if device.get("firstName") and device.get("lastName"):
                name = f"{device['firstName']} {device['lastName']}"
            log_attendance(mac_address, name, new_status)

        # Update previous status
        device_previous_status[mac_address] = new_status
        return True
    except TimeoutError:
        logger.error(f"Convex mutation timed out after {CONVEX_QUERY_TIMEOUT} seconds for {mac_address}")
        logger.info(f"  Status update will be retried on next polling cycle")
        return False
    except Exception as e:
        logger.error(f"Error updating device status for {mac_address}: {e}")
        logger.info(f"  Status update will be retried on next polling cycle")
        return False


def check_and_update_devices() -> None:
    """
    Check the connection status of all devices and update Convex.

    - Registers new devices that connect as pending (with grace period)
    - Updates status for all known devices (named or pending)
    - Marks devices as absent when they disconnect
    - Cleans up expired pending devices at end of cycle
    """
    global failed_registrations
    global last_full_probe_time

    # Get all connected Bluetooth devices
    connected_devices = scan_all_connected_devices()
    connected_set = set(connected_devices)

    # Get known devices from Convex
    devices = get_known_devices()

    if not devices:
        logger.warning("No devices found in Convex database")
    else:
        logger.info(f"Found {len(devices)} known device(s) in Convex")

    # PRE-SCAN RECONNECTION / FULL PROBE:
    # Attempt to reconnect (or fully probe) known devices to detect presence.
    registered_macs = {
        d.get("macAddress")
        for d in devices
        if d.get("macAddress") and not d.get("pendingRegistration")
    }

    reconnect_results: dict[str, bool] = {}
    reconnected_success: set[str] = set()

    now = time.time()
    _prune_recheck_state(now)

    # Always use full probe mode (connect + disconnect) to check presence
    if registered_macs and FULL_PROBE_ENABLED:
        logger.info(
            f"Running full probe for {len(registered_macs)} registered device(s)..."
        )
        reconnect_results = bluetooth_scanner.probe_devices(
            sorted(registered_macs),
            disconnect_after=FULL_PROBE_DISCONNECT_AFTER,
        )
        last_full_probe_time = now
        reconnected_success = {mac for mac, success in reconnect_results.items() if success}

    global silent_cycle_streak

    presence_signals_this_cycle: set[str] = connected_set | reconnected_success

    if presence_signals_this_cycle:
        if silent_cycle_streak:
            logger.debug(
                "Resetting silent cycle streak after detecting %d presence signal(s)",
                len(presence_signals_this_cycle),
            )
        silent_cycle_streak = 0
    else:
        silent_cycle_streak += 1
        logger.debug(
            "No presence signals detected this cycle (streak %d/%s)",
            silent_cycle_streak,
            "∞" if ALL_SILENT_ABSENCE_CYCLES == 0 else ALL_SILENT_ABSENCE_CYCLES,
        )

    # Track recently seen devices (connected or successfully pinged)
    for mac in presence_signals_this_cycle:
        recently_seen_devices[mac] = now
        last_presence_signal[mac] = now

    # Add newly registered devices to present_set to ensure they enter polling cycle immediately
    # This fixes the bug where first-time registered devices are not properly tracked
    for device in devices:
        mac_address = device.get("macAddress")
        if not mac_address:
            continue
        connected_since = device.get("connectedSince")
        status = device.get("status")

        # If device was just registered (has recent connectedSince) and status is "present"
        # Always include it in present_set to add it to the polling cycle
        if (connected_since and
            status == "present" and
            now - (connected_since / 1000) <= NEWLY_REGISTERED_GRACE_PERIOD):
            recently_seen_devices[mac_address] = now
            last_presence_signal[mac_address] = now
            logger.debug(
                f"Added newly registered device to present_set: {mac_address} "
                f"(connectedSince: {connected_since / 1000:.1f}s ago)"
            )

    present_set = {
        mac
        for mac, last_seen in recently_seen_devices.items()
        if now - last_seen <= PRESENT_TTL_SECONDS
    }
    # Prune expired entries
    for mac in list(recently_seen_devices):
        if mac not in present_set:
            del recently_seen_devices[mac]

    logger.info(f"Present devices (connected/recently seen): {len(present_set)} device(s)")

    # Identify devices that need a quick bluetoothctl recheck before declaring absence
    recheck_candidates: set[str] = set()
    for device in devices:
        mac_address = device.get("macAddress")
        if not mac_address or mac_address in present_set:
            continue
        current_status = device.get("status") or "unknown"
        if _should_attempt_recheck(mac_address, now, current_status):
            recheck_candidates.add(mac_address)

    if recheck_candidates:
        logger.debug(f"Scheduling {len(recheck_candidates)} absence recheck(s)")
    recheck_results = _perform_absence_rechecks(recheck_candidates)
    recheck_successes = {mac for mac, success in recheck_results.items() if success}

    if recheck_successes:
        logger.info(
            "Rechecks recovered %d device(s) before absence flip", len(recheck_successes)
        )
        presence_signals_this_cycle.update(recheck_successes)
        for mac in recheck_successes:
            recently_seen_devices[mac] = now
            last_presence_signal[mac] = now
            present_set.add(mac)

    updated_count = 0
    newly_registered_count = 0

    # Register new devices based on current connections
    for mac_address in connected_set:
        device = get_device_by_mac(mac_address)
        if device:
            failed_registrations.discard(mac_address)
            continue

        # New device - register as pending with device name from Bluetooth
        if mac_address in failed_registrations:
            logger.info(
                f"Retrying registration for previously failed device: {mac_address}"
            )

        device_name = bluetooth_scanner.get_device_name(mac_address)
        logger.info(
            f"New device detected: {mac_address} ({device_name or 'unknown'}) - registering as pending"
        )
        if register_new_device(mac_address, device_name):
            newly_registered_count += 1
            failed_registrations.discard(mac_address)
        else:
            failed_registrations.add(mac_address)

    # Update status for known devices based on presence set
    for device in devices:
        mac_address = device.get("macAddress")
        if not mac_address:
            continue

        name = device.get("name")
        current_status = device.get("status", "unknown")

        if name:
            display_name = name
        else:
            display_name = f"[pending] {mac_address}"

        signal_detected = mac_address in presence_signals_this_cycle
        is_present_now = signal_detected
        held_present_reasons: list[str] = []
        streak = absence_miss_streaks.get(mac_address, 0)

        if signal_detected:
            # Reset absence streak
            absence_miss_streaks.pop(mac_address, None)
            pending_absence_rechecks.pop(mac_address, None)

            # Increment positive detection counter
            consecutive_positive_detections[mac_address] = consecutive_positive_detections.get(mac_address, 0) + 1

            # Check if we have enough confirmations to consider device present
            if consecutive_positive_detections[mac_address] < POSITIVE_DETECTION_CONFIRMATIONS:
                is_present_now = False
                held_present_reasons.append(
                    f"building confirmations {consecutive_positive_detections[mac_address]}/{POSITIVE_DETECTION_CONFIRMATIONS}"
                )
        else:
            # Reset positive detection counter
            consecutive_positive_detections[mac_address] = 0

            # Increment absence streak
            streak += 1
            absence_miss_streaks[mac_address] = streak

            if streak < ABSENCE_HYSTERESIS_CYCLES:
                held_present_reasons.append(
                    f"absence streak {streak}/{ABSENCE_HYSTERESIS_CYCLES}"
                )

            silent_grace_active = (
                REQUIRE_PRESENCE_SIGNAL_FOR_ABSENCE
                and not presence_signals_this_cycle
                and (ALL_SILENT_ABSENCE_CYCLES > 0 and silent_cycle_streak <= ALL_SILENT_ABSENCE_CYCLES)
            )

            if silent_grace_active:
                held_present_reasons.append(
                    "no presence signals detected this cycle (within silent timeout)"
                )

            if mac_address in recheck_candidates and mac_address not in recheck_successes:
                held_present_reasons.append("recent recheck failure")

        final_is_present = is_present_now
        confidence = _calculate_presence_confidence(mac_address, now, signal_detected)
        if (
            not final_is_present
            and current_status == "present"
            and confidence >= PRESENCE_CONFIDENCE_MIN
        ):
            final_is_present = True
            held_present_reasons.append(f"confidence {confidence:.2f}")

        new_status = "present" if final_is_present else "absent"

        if held_present_reasons and new_status == "present" and current_status == "present":
            logger.debug(
                "Holding %s (%s) as present due to %s",
                display_name,
                mac_address,
                "; ".join(held_present_reasons),
            )



        if new_status != current_status:
            logger.info(
                f"Status changed for {display_name} ({mac_address}): "
                f"{current_status} -> {new_status}"
            )
            if update_device_status(mac_address, final_is_present, current_status, device):
                updated_count += 1
        else:
            logger.debug(
                "No status change for %s (%s): %s (confidence %.2f)",
                display_name,
                mac_address,
                current_status,
                device_confidence_scores.get(mac_address, 0.0),
            )
            device_previous_status[mac_address] = new_status

    # Clean up state for devices that are no longer registered
    _prune_device_state(registered_macs, now)

    # Optionally disconnect devices to avoid hitting adapter connection limits
    if DISCONNECT_CONNECTED_AFTER_CYCLE and connected_set:
        logger.info(f"Disconnecting {len(connected_set)} device(s) to free slots")
        for mac_address in connected_set:
            bluetooth_scanner.disconnect_device(mac_address)

    # Clean up expired grace periods
    cleanup_expired_devices()
    
    # Clean up stale Bluetooth pairings (every cycle)
    cleanup_stale_bluetooth_pairings()

    # Log summary
    if newly_registered_count > 0:
        logger.info(f"Registered {newly_registered_count} new device(s) as pending")
    if updated_count > 0:
        logger.info(f"Updated {updated_count} device(s) in this cycle")
    else:
        logger.info("No device status changes in this cycle")


def run_presence_tracker() -> None:
    """
    Main polling loop for the presence tracker.

    Runs continuously with a 5-second polling interval, checking device
    connection status and updating Convex as needed. Attendance is only
    logged when device status actually changes (deduplication).
    """
    logger.info("Starting Presence Tracker")
    logger.info(f"Polling interval: {POLLING_INTERVAL} seconds")
    logger.info(f"Grace period for new devices: {GRACE_PERIOD_SECONDS} seconds")
    logger.info(f"Presence TTL: {PRESENT_TTL_SECONDS} seconds")
    logger.info(f"Full probe enabled: {FULL_PROBE_ENABLED} (uses passive detection: l2ping + name request)")
    logger.info(f"Full probe interval: {FULL_PROBE_INTERVAL_SECONDS} seconds")
    logger.info(f"Full probe disconnect after: {FULL_PROBE_DISCONNECT_AFTER}")
    logger.info(f"Positive detection confirmations: {POSITIVE_DETECTION_CONFIRMATIONS}")

    logger.info(f"Disconnect after cycle: {DISCONNECT_CONNECTED_AFTER_CYCLE}")
    logger.info(f"Convex query timeout: {CONVEX_QUERY_TIMEOUT} seconds")

    # Skip startup cleanup to avoid hanging on Convex connection
    # Stale Bluetooth pairings will be cleaned during polling cycles

    try:
        while True:
            logger.info("=" * 50)
            logger.info(f"Starting check cycle at {datetime.now().isoformat()}")

            try:
                check_and_update_devices()
            except Exception as e:
                logger.error(f"Error during check cycle: {e}")

            logger.info(f"Cycle complete. Next check in {POLLING_INTERVAL} seconds...")
            logger.info("=" * 50)

            time.sleep(POLLING_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Presence tracker stopped by user")
    except Exception as e:
        logger.error(f"Fatal error in presence tracker: {e}")
        raise


def main() -> None:
    """Entry point for the presence tracker."""
    try:
        run_presence_tracker()
    except Exception as e:
        logger.critical(f"Presence tracker crashed: {e}")
        raise


if __name__ == "__main__":
    main()
