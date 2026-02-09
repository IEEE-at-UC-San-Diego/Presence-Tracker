import json
import os
import time
import logging
import threading
from collections import deque
from pathlib import Path
from datetime import datetime
from queue import Empty
from typing import Any, Optional
import convex
from dotenv import load_dotenv
import bluetooth_scanner
import bluetooth_agent
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from fast_path_queue import start_queue_server
from logging_utils import configure_root_logger

# Load environment variables
load_dotenv()

# Configure logging
configure_root_logger("presence_tracker.log", level=logging.INFO)
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
POLLING_INTERVAL = int(os.getenv("POLLING_INTERVAL_SECONDS", "15"))

# Grace period for new device registration in seconds
GRACE_PERIOD_SECONDS = int(os.getenv("GRACE_PERIOD_SECONDS", "300"))

# Presence TTL for recently seen devices (seconds)
PRESENT_TTL_SECONDS = int(os.getenv("PRESENT_TTL_SECONDS", "60"))

# Adaptive smoothing / diagnostics configuration
def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


ENABLE_DEVICE_DIAGNOSTICS = _env_flag("ENABLE_DEVICE_DIAGNOSTICS", "false")
ENABLE_ADAPTIVE_HYSTERESIS = _env_flag("ENABLE_ADAPTIVE_HYSTERESIS", "true")
ABSENCE_HOLD_SECONDS = int(os.getenv("ABSENCE_HOLD_SECONDS", "90"))
ABSENCE_CONSECUTIVE_MISS_THRESHOLD = int(os.getenv("ABSENCE_CONSECUTIVE_MISS_THRESHOLD", "2"))
FLAP_MONITOR_WINDOW_SECONDS = int(os.getenv("FLAP_MONITOR_WINDOW_SECONDS", "3600"))
FLAP_ALERT_THRESHOLD = int(os.getenv("FLAP_ALERT_THRESHOLD", "4"))
ENABLE_AUTO_FREEZE_ON_FLAP = _env_flag("ENABLE_AUTO_FREEZE_ON_FLAP", "true")
AUTO_FREEZE_DURATION_SECONDS = int(os.getenv("AUTO_FREEZE_DURATION_SECONDS", "300"))
DEVICE_OVERRIDE_FILE = os.getenv("DEVICE_OVERRIDE_FILE", "config/device_overrides.json")
DEVICE_OVERRIDE_REFRESH_SECONDS = int(os.getenv("DEVICE_OVERRIDE_REFRESH_SECONDS", "30"))

FAST_PATH_QUEUE_ENABLED = _env_flag("FAST_PATH_QUEUE_ENABLED", "true")
FAST_PATH_EVENT_SUPPRESSION_SECONDS = int(os.getenv("FAST_PATH_EVENT_SUPPRESSION_SECONDS", "3"))

# Retry cadence for publishing newly seen devices to Convex (seconds)
REGISTRATION_RETRY_SECONDS = int(os.getenv("REGISTRATION_RETRY_SECONDS", "5"))

# How long to keep retrying to publish a device after it disconnects (seconds)
UNPUBLISHED_DEVICE_TTL_SECONDS = int(os.getenv("UNPUBLISHED_DEVICE_TTL_SECONDS", "600"))

# Tiered l2ping scheduler configuration
ACTIVE_TIER_MAX = int(os.getenv("ACTIVE_TIER_MAX", "20"))
WARM_TIER_BATCH = int(os.getenv("WARM_TIER_BATCH", "5"))
COLD_TIER_BATCH = int(os.getenv("COLD_TIER_BATCH", "3"))
WARM_TIER_THRESHOLD_SECONDS = int(os.getenv("WARM_TIER_THRESHOLD_SECONDS", "600"))

# Track devices that failed to register, so we retry them
failed_registrations: set[str] = set()

# Track previous status of each device for deduplication
device_previous_status: dict[str, str] = {}

# Track the last time we recorded any positive signal per device
last_presence_signal: dict[str, float] = {}

# Track per-device signal diagnostics and flapping metadata
device_signal_stats: dict[str, dict[str, Any]] = {}
status_transition_history: dict[str, deque[float]] = {}
device_freeze_until: dict[str, float] = {}

# Manual override cache
_override_cache: dict[str, Any] = {
    "expires": 0.0,
    "data": {
        "quarantine": set(),
        "force_status": {},
    },
}

# Track newly detected devices that still need to be published to Convex
unpublished_devices: dict[str, dict[str, Any]] = {}

# Timeout for Convex queries in seconds
CONVEX_QUERY_TIMEOUT = 10

# Track if Convex is responsive (skip if too many timeouts)
convex_responsive = True
consecutive_timeouts = 0
MAX_CONSECUTIVE_TIMEOUTS = 3

_fast_path_queue = None
_fast_path_thread: threading.Thread | None = None
_fast_path_stop_event = threading.Event()
_fast_path_recent_events: dict[str, float] = {}

# Single shared executor for all Convex calls (serialises access to the
# non-thread-safe ConvexClient while still allowing timeout enforcement).
_convex_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convex")

# In-cycle device cache — populated once per check_and_update_devices() call
# so that get_device_by_mac() doesn't re-query Convex.
_cycle_device_cache: list[dict[str, Any]] | None = None


def _convex_call(fn, timeout: float | None = None):
    """Submit *fn* to the shared Convex executor with a timeout."""
    global convex_responsive, consecutive_timeouts
    if timeout is None:
        timeout = CONVEX_QUERY_TIMEOUT
    try:
        future = _convex_executor.submit(fn)
        result = future.result(timeout=timeout)
        if consecutive_timeouts > 0:
            consecutive_timeouts = 0
            convex_responsive = True
            logger.info("Convex connection recovered")
        return result
    except TimeoutError:
        consecutive_timeouts += 1
        if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
            convex_responsive = False
            logger.error(
                "Convex call timed out (%dx) — entering circuit-breaker mode",
                consecutive_timeouts,
            )
        else:
            logger.error(
                "Convex call timed out after %ss (%d/%d)",
                timeout,
                consecutive_timeouts,
                MAX_CONSECUTIVE_TIMEOUTS,
            )
        return None
    except Exception as exc:
        logger.error("Convex call failed: %s", exc)
        return None


class DeviceScheduler:
    """Tiered l2ping scheduler that limits per-cycle work.

    Devices are split into three tiers based on how long they have been absent:

    * **Active** — present within the last ``PRESENT_TTL_SECONDS``.  Checked
      every cycle (up to ``ACTIVE_TIER_MAX``).
    * **Warm** — absent for less than ``WARM_TIER_THRESHOLD_SECONDS``.  A
      rotating batch of ``WARM_TIER_BATCH`` is checked each cycle.
    * **Cold** — absent longer than the warm threshold.  A rotating batch of
      ``COLD_TIER_BATCH`` is checked each cycle.

    Devices that are already confirmed connected (via ``bluetoothctl devices
    Connected``) are **never** l2pinged — they are automatically treated as
    present.
    """

    def __init__(self) -> None:
        self._warm_offset = 0
        self._cold_offset = 0

    def select(
        self,
        registered_macs: set[str],
        pending_macs: set[str],
        connected_set: set[str],
        now: float,
    ) -> list[str]:
        """Return the ordered list of MACs to l2ping this cycle (sequential)."""

        # Never l2ping devices that are already connected — they are present.
        candidates = (registered_macs | pending_macs) - connected_set

        active: list[str] = []
        warm: list[str] = []
        cold: list[str] = []

        for mac in sorted(candidates):
            last_ts = last_presence_signal.get(mac)
            if last_ts is None:
                cold.append(mac)
                continue
            age = now - last_ts
            if age <= PRESENT_TTL_SECONDS:
                active.append(mac)
            elif age <= WARM_TIER_THRESHOLD_SECONDS:
                warm.append(mac)
            else:
                cold.append(mac)

        # Cap active tier
        selected = active[:ACTIVE_TIER_MAX]

        # Rotate through warm tier
        if warm:
            batch = WARM_TIER_BATCH
            start = self._warm_offset % len(warm)
            selected += (warm + warm)[start : start + batch]
            self._warm_offset = (start + batch) % max(1, len(warm))

        # Rotate through cold tier
        if cold:
            batch = COLD_TIER_BATCH
            start = self._cold_offset % len(cold)
            selected += (cold + cold)[start : start + batch]
            self._cold_offset = (start + batch) % max(1, len(cold))

        logger.info(
            "DeviceScheduler: active=%d warm=%d/%d cold=%d/%d connected=%d (skipped) → probing %d",
            len(active),
            min(WARM_TIER_BATCH, len(warm)),
            len(warm),
            min(COLD_TIER_BATCH, len(cold)),
            len(cold),
            len(connected_set & (registered_macs | pending_macs)),
            len(selected),
        )
        return selected


_device_scheduler = DeviceScheduler()


def _prune_device_state(known_macs: set[str]) -> None:
    """Remove cached state for devices that are no longer tracked."""

    for mac in list(device_previous_status.keys()):
        if mac not in known_macs:
            device_previous_status.pop(mac, None)

    for mac in list(last_presence_signal.keys()):
        if mac not in known_macs:
            last_presence_signal.pop(mac, None)

    for mac in list(device_signal_stats.keys()):
        if mac not in known_macs:
            device_signal_stats.pop(mac, None)

    for mac in list(status_transition_history.keys()):
        if mac not in known_macs:
            status_transition_history.pop(mac, None)

    for mac in list(device_freeze_until.keys()):
        if mac not in known_macs:
            device_freeze_until.pop(mac, None)


def _start_fast_path_consumer() -> None:
    global _fast_path_queue, _fast_path_thread
    if not FAST_PATH_QUEUE_ENABLED:
        return
    if _fast_path_thread and _fast_path_thread.is_alive():
        return
    try:
        queue = start_queue_server()
    except Exception as exc:
        logger.error("Failed to start fast-path queue server: %s", exc)
        return

    _fast_path_queue = queue
    _fast_path_stop_event.clear()

    thread = threading.Thread(
        target=_fast_path_consumer_loop,
        name="FastPathConsumer",
        daemon=True,
    )
    thread.start()
    _fast_path_thread = thread
    logger.info("Fast-path queue consumer started")


def _stop_fast_path_consumer() -> None:
    if not FAST_PATH_QUEUE_ENABLED:
        return
    _fast_path_stop_event.set()
    thread = _fast_path_thread
    if thread and thread.is_alive():
        thread.join(timeout=2)


def _fast_path_consumer_loop() -> None:
    while not _fast_path_stop_event.is_set():
        if _fast_path_queue is None:
            time.sleep(1)
            continue
        try:
            payload = _fast_path_queue.get(timeout=1)
        except Empty:
            continue
        except Exception as exc:
            logger.warning("Fast-path queue read failed: %s", exc)
            time.sleep(1)
            continue

        try:
            _handle_fast_path_payload(payload)
        except Exception as exc:
            logger.error("Error handling fast-path payload %s: %s", payload, exc)


def _handle_fast_path_payload(payload: Any) -> None:
    if not FAST_PATH_QUEUE_ENABLED:
        return
    if not isinstance(payload, dict):
        logger.debug("Ignoring malformed fast-path payload: %s", payload)
        return

    mac = payload.get("mac")
    if not isinstance(mac, str) or not mac:
        logger.debug("Fast-path payload missing MAC: %s", payload)
        return
    mac = _normalize_mac(mac)

    now = time.time()
    suppression_window = max(0, FAST_PATH_EVENT_SUPPRESSION_SECONDS)
    if suppression_window > 0:
        last_ts = _fast_path_recent_events.get(mac)
        if last_ts and now - last_ts < suppression_window:
            logger.debug("Suppressing duplicate fast-path event for %s", mac)
            return
        _fast_path_recent_events[mac] = now

    last_presence_signal[mac] = now
    _update_signal_stats(mac, True, "fast_path", now)

    name = payload.get("name")
    device = get_device_by_mac(mac)

    if not device:
        logger.info("Fast-path detected new device %s (name=%s)", mac, name or "unknown")
        result = register_new_device(mac, name)
        if result:
            failed_registrations.discard(mac)
        else:
            failed_registrations.add(mac)
            _record_unpublished_device(mac, name, now)
        return

    if device.get("pendingRegistration"):
        logger.debug("Fast-path event for pending device %s (awaiting approval)", mac)
        return

    current_status = device.get("status")
    update_device_status(mac, True, current_status, device)


def _normalize_mac(mac: str | None) -> str:
    return (mac or "").strip().upper()


def _get_device_overrides(now: float | None = None) -> dict[str, Any]:
    """Load quarantine/force-status overrides from disk with simple caching."""

    global _override_cache
    now = now or time.time()
    if now < _override_cache.get("expires", 0.0):
        return _override_cache["data"]

    overrides = {
        "quarantine": set(),
        "force_status": {},
    }

    override_path = Path(DEVICE_OVERRIDE_FILE)
    if override_path.is_file():
        try:
            with override_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            quarantine = payload.get("quarantine", [])
            overrides["quarantine"] = {
                _normalize_mac(mac)
                for mac in quarantine
                if isinstance(mac, str)
            }

            force_status_raw = payload.get("forceStatus", {})
            force_status: dict[str, str] = {}
            if isinstance(force_status_raw, dict):
                for mac, status in force_status_raw.items():
                    if not isinstance(mac, str) or not isinstance(status, str):
                        continue
                    normalized = status.lower().strip()
                    if normalized in {"present", "absent"}:
                        force_status[_normalize_mac(mac)] = normalized
            overrides["force_status"] = force_status
        except Exception as exc:
            logger.error(
                "Failed to load device overrides from %s: %s",
                DEVICE_OVERRIDE_FILE,
                exc,
            )

    _override_cache = {
        "expires": now + max(5, DEVICE_OVERRIDE_REFRESH_SECONDS),
        "data": overrides,
    }
    return overrides


def _update_signal_stats(mac: str, success: bool, source: str | None, timestamp: float) -> None:
    stats = device_signal_stats.setdefault(
        mac,
        {
            "consecutive_hits": 0,
            "consecutive_misses": 0,
            "last_signal_ts": 0.0,
            "last_signal_source": None,
        },
    )

    if success:
        stats["consecutive_hits"] += 1
        stats["consecutive_misses"] = 0
        stats["last_signal_ts"] = timestamp
        stats["last_signal_source"] = source
    else:
        stats["consecutive_misses"] += 1
        stats["consecutive_hits"] = 0


def _record_status_transition(mac: str, previous_status: str | None, new_status: str, now: float) -> None:
    if previous_status is None or previous_status == new_status:
        return

    history = status_transition_history.setdefault(mac, deque())
    history.append(now)
    window = max(10, FLAP_MONITOR_WINDOW_SECONDS)
    while history and now - history[0] > window:
        history.popleft()

    transitions = len(history)
    if transitions >= max(1, FLAP_ALERT_THRESHOLD):
        logger.warning(
            "Device %s flapped %s times in the last %ss (prev=%s -> new=%s)",
            mac,
            transitions,
            window,
            previous_status,
            new_status,
        )
        if ENABLE_AUTO_FREEZE_ON_FLAP and AUTO_FREEZE_DURATION_SECONDS > 0:
            freeze_until = now + AUTO_FREEZE_DURATION_SECONDS
            prior_freeze = device_freeze_until.get(mac, 0.0)
            if freeze_until > prior_freeze:
                device_freeze_until[mac] = freeze_until
                logger.warning(
                    "Freezing device %s status updates until %s",
                    mac,
                    datetime.fromtimestamp(freeze_until).isoformat(),
                )


def _log_device_diagnostics(
    mac: str,
    device: dict[str, Any],
    now: float,
    desired_present: bool,
    overrides: dict[str, Any],
    decision_reason: Optional[str] = None,
) -> None:
    if not ENABLE_DEVICE_DIAGNOSTICS:
        return

    stats = device_signal_stats.get(mac, {})
    override_note = None
    norm_mac = _normalize_mac(mac)
    if norm_mac in overrides.get("quarantine", set()):
        override_note = "quarantine"
    elif overrides.get("force_status", {}).get(norm_mac):
        override_note = f"force={overrides['force_status'][norm_mac]}"

    diag = {
        "mac": mac,
        "user": device.get("name") or mac,
        "convex_status": device.get("status"),
        "desired": "present" if desired_present else "absent",
        "last_signal_age_s": round(now - last_presence_signal.get(mac, 0.0), 1)
        if mac in last_presence_signal
        else None,
        "last_signal_source": stats.get("last_signal_source"),
        "hits": stats.get("consecutive_hits"),
        "misses": stats.get("consecutive_misses"),
    }

    freeze_until = device_freeze_until.get(mac)
    if freeze_until and freeze_until > now:
        diag["freeze_until"] = datetime.fromtimestamp(freeze_until).isoformat()
    if override_note:
        diag["override"] = override_note
    if decision_reason:
        diag["decision"] = decision_reason

    logger.info("Device diagnostics: %s", diag)


def _compute_presence_decision(
    mac: str,
    now: float,
    previous_status: Optional[str],
    overrides: dict[str, Any],
) -> tuple[bool, str]:
    norm_mac = _normalize_mac(mac)
    if norm_mac in overrides.get("quarantine", set()):
        return False, "quarantine"

    forced_status = overrides.get("force_status", {}).get(norm_mac)
    if forced_status:
        return forced_status == "present", f"force:{forced_status}"

    freeze_until = device_freeze_until.get(mac, 0.0)
    if freeze_until and freeze_until > now and previous_status is not None:
        return previous_status == "present", "frozen"

    last_signal_ts = last_presence_signal.get(mac)
    signal_age = float("inf") if last_signal_ts is None else now - last_signal_ts
    within_ttl = signal_age <= PRESENT_TTL_SECONDS

    if within_ttl:
        return True, "ttl"

    if not ENABLE_ADAPTIVE_HYSTERESIS:
        return False, "ttl_expired"

    stats = device_signal_stats.get(mac, {})
    if previous_status == "present":
        misses = stats.get("consecutive_misses", 0)
        hold_elapsed = signal_age >= ABSENCE_HOLD_SECONDS
        threshold_met = misses >= ABSENCE_CONSECUTIVE_MISS_THRESHOLD
        if not (hold_elapsed and threshold_met):
            return True, "absence_hold"

    return False, "adaptive_absent"


def get_known_devices() -> list[dict[str, Any]]:
    """
    Fetch all known devices from Convex using the getDevices function.

    If an in-cycle cache is available it is returned immediately.

    Returns:
        List of device dictionaries with macAddress, name, status, and lastSeen fields
    """
    global _cycle_device_cache
    if _cycle_device_cache is not None:
        return _cycle_device_cache

    if not convex_responsive and consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
        logger.warning(
            "Convex temporarily unavailable (%d consecutive timeouts), skipping query",
            consecutive_timeouts,
        )
        return []

    result = _convex_call(lambda: get_convex_client().query("devices:getDevices"))
    if result is None:
        return []
    logger.info("Retrieved %d devices from Convex", len(result))
    return result


def get_device_by_mac(mac_address: str) -> dict[str, Any] | None:
    """
    Find a device by MAC address in the Convex database.

    Uses the in-cycle cache when available so this is essentially free
    after the first ``get_known_devices()`` call in a cycle.

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


def register_new_device(mac_address: str, name: str | None = None) -> dict[str, Any] | None:
    """
    Register a new device in Convex with grace period.

    New devices are registered in pending state, giving them time to be
    properly named before being tracked for presence.

    Only registers device if it has reached the "paired" state in bluetooth_agent.

    Args:
        mac_address: The MAC address of the device
        name: Optional device name from Bluetooth scan

    Returns:
        Device dictionary if registration was successful, None otherwise
    """
    # Check pairing state before registering to Convex
    try:
        if not bluetooth_agent.is_paired(mac_address):
            logger.info(
                f"Skipping Convex registration for {mac_address}: device not yet paired (awaiting pairing state)"
            )
            return None
    except Exception as e:
        logger.warning(f"Error checking pairing state for {mac_address}: {e}")
        return None

    logger.info("→ register_new_device called: mac=%s, name='%s'", mac_address, name)
    result = _convex_call(
        lambda: get_convex_client().mutation(
            "devices:registerPendingDevice",
            {"macAddress": mac_address, "name": name or ""},
        )
    )
    if result is not None:
        logger.info(
            "✓ Registered new device %s (name='%s') in pending state",
            mac_address,
            name or "unknown",
        )
    else:
        logger.error("✗ Failed to register device %s (will retry)", mac_address)
    return result


def _record_unpublished_device(
    mac_address: str,
    name: str | None,
    now: float,
    last_attempt_ts: float | None = None,
) -> None:
    """Track a newly seen device so we keep retrying registration even after it disconnects."""

    entry = unpublished_devices.get(mac_address)
    if entry is None:
        unpublished_devices[mac_address] = {
            "name": name,
            "last_seen": now,
            "last_attempt": last_attempt_ts or 0.0,
        }
        logger.info(
            "Tracking unpublished device %s for Convex retry (name=%s)",
            mac_address,
            name or "unknown",
        )
        return

    entry["last_seen"] = now
    if name and not entry.get("name"):
        entry["name"] = name
    if last_attempt_ts is not None:
        entry["last_attempt"] = last_attempt_ts


def _expire_unpublished_devices(now: float) -> None:
    """Drop unpublished devices that have not been seen recently."""

    for mac, data in list(unpublished_devices.items()):
        if now - data.get("last_seen", 0.0) > UNPUBLISHED_DEVICE_TTL_SECONDS:
            logger.info(
                "Giving up on unpublished device %s after %.0fs",
                mac,
                UNPUBLISHED_DEVICE_TTL_SECONDS,
            )
            unpublished_devices.pop(mac, None)


def _retry_unpublished_devices(
    now: float,
    device_map: dict[str, dict[str, Any]],
    registered_macs: set[str],
    pending_macs: set[str],
) -> None:
    """Attempt to publish any devices we previously discovered but failed to register."""

    for mac, data in list(unpublished_devices.items()):
        if mac in device_map:
            unpublished_devices.pop(mac, None)
            continue

        last_attempt = data.get("last_attempt", 0.0)
        if now - last_attempt < REGISTRATION_RETRY_SECONDS:
            continue

        device_name = data.get("name")
        logger.info(
            "Retrying unpublished device %s (last attempt %.1fs ago)",
            mac,
            now - last_attempt,
        )
        result = register_new_device(mac, device_name)
        data["last_attempt"] = now
        if result:
            unpublished_devices.pop(mac, None)
            device_map[mac] = result
            if result.get("pendingRegistration"):
                pending_macs.add(mac)
            else:
                registered_macs.add(mac)


def cleanup_expired_devices() -> bool:
    """
    Clean up devices whose grace period has expired and are still pending.
    Also disconnects and removes the Bluetooth pairing for those devices.

    Returns:
        True if cleanup was successful, False otherwise
    """
    global failed_registrations

    result = _convex_call(
        lambda: get_convex_client().action("devices:cleanupExpiredGracePeriods", {})
    )
    if result is None:
        return False

    deleted_count = result.get("deletedCount", 0)
    deleted_macs = result.get("deletedMacs", [])

    if deleted_count > 0:
        logger.info("Cleaned up %d expired grace period(s)", deleted_count)
        for mac_address in deleted_macs:
            try:
                logger.info("Disconnecting and removing expired device: %s", mac_address)
                bluetooth_scanner.disconnect_device(mac_address)
                bluetooth_scanner.remove_device(mac_address)
                bluetooth_agent.reset_device_state(mac_address)
                logger.info("Successfully removed Bluetooth pairing and cleared state for: %s", mac_address)
                failed_registrations.discard(mac_address)
            except Exception as e:
                logger.error("Error removing Bluetooth device %s: %s", mac_address, e)
    else:
        logger.debug("No expired grace periods to clean up")

    return True


def cleanup_stale_bluetooth_pairings() -> None:
    """
    Remove Bluetooth pairings for devices that are pending registration AND
    have past their grace period. Never removes registered devices.
    Only runs during normal polling cycles, not at startup to avoid hangs.
    """
    try:
        # Get all paired devices from Bluetooth
        paired_devices = bluetooth_scanner.get_paired_devices()
        paired_set = set(paired_devices)
        logger.debug(f"Found {len(paired_set)} paired device(s) in Bluetooth")

        # Do not remove devices that are currently connected
        connected_devices = bluetooth_scanner.get_all_connected_devices()
        connected_set = set(connected_devices)
        if connected_set:
            logger.debug(f"Found {len(connected_set)} connected device(s) in Bluetooth")

        # Get all known devices from Convex (with timeout)
        convex_devices = get_known_devices()
        if not convex_devices:
            logger.debug("Skipping stale cleanup - no devices retrieved from Convex")
            return

        logger.debug(f"Found {len(convex_devices)} device(s) in Convex database")

        # Build a map of MAC addresses to device info for quick lookup
        device_map = {device.get("macAddress"): device for device in convex_devices if device.get("macAddress")}

        # Current time in milliseconds for grace period comparison
        now_ms = time.time() * 1000
        devices_to_remove = []

        # Check each paired device to see if it should be removed
        for mac_address in paired_set:
            if mac_address in connected_set:
                logger.debug(f" keeping {mac_address}: currently connected")
                continue

            # Device not in Convex - keep it (could be a new device pending registration)
            device = device_map.get(mac_address)
            if device is None:
                logger.debug(f" keeping {mac_address}: not in Convex database (may be pending registration)")
                continue

            # Device is registered (not pending) - never remove
            if not device.get("pendingRegistration", False):
                logger.debug(f" keeping {mac_address}: registered device (not pending)")
                continue

            # Device is pending - check if grace period has expired
            grace_period_end = device.get("gracePeriodEnd")
            if grace_period_end is None:
                logger.debug(f" keeping {mac_address}: pending but no grace period set")
                continue

            # Check if grace period has expired
            if grace_period_end <= now_ms:
                grace_period_expired_seconds = (now_ms - grace_period_end) / 1000
                logger.info(
                    f" marking for removal: {mac_address} - pending registration with expired grace period "
                    f"({grace_period_expired_seconds:.1f}s ago)"
                )
                devices_to_remove.append(mac_address)
            else:
                grace_period_remaining_seconds = (grace_period_end - now_ms) / 1000
                logger.debug(
                    f" keeping {mac_address}: pending registration, grace period expires in "
                    f"{grace_period_remaining_seconds:.1f}s"
                )

        # Remove the identified devices
        if devices_to_remove:
            logger.info(f"Removing {len(devices_to_remove)} expired pending device(s) from Bluetooth")
            for mac_address in devices_to_remove:
                try:
                    bluetooth_scanner.remove_device(mac_address)
                    bluetooth_agent.reset_device_state(mac_address)
                    logger.info(f"Removed expired pending device Bluetooth pairing and cleared state: {mac_address}")
                except Exception as e:
                    logger.error(f"Failed to remove expired pending device {mac_address}: {e}")
        else:
            logger.debug("No expired pending devices found to remove")
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
    result = _convex_call(
        lambda: get_convex_client().mutation(
            "devices:logAttendance",
            {
                "userId": mac_address,
                "userName": name,
                "status": status,
                "deviceId": mac_address,
            },
        )
    )
    if result is None:
        logger.error("Attendance logging failed for %s (will retry)", mac_address)
        return False
    return True


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
    new_status = "present" if is_connected else "absent"
    previous_status = (
        current_status if current_status is not None else device_previous_status.get(mac_address)
    )

    # Only update Convex when status changes
    if previous_status == new_status:
        logger.debug(
            "Status unchanged for %s: %s (skipping Convex update)",
            mac_address,
            new_status,
        )
        device_previous_status[mac_address] = new_status
        return True

    _record_status_transition(mac_address, previous_status, new_status, time.time())

    result = _convex_call(
        lambda: get_convex_client().mutation(
            "devices:updateDeviceStatus",
            {"macAddress": mac_address, "status": new_status},
        )
    )
    if result is None:
        logger.error("Status update failed for %s (will retry)", mac_address)
        return False

    logger.info("Updated device %s status to %s", mac_address, new_status)

    # Log attendance only for registered devices (not pending)
    device = device_info if device_info is not None else get_device_by_mac(mac_address)
    if device and not device.get("pendingRegistration"):
        name = device.get("name", mac_address)
        if device.get("firstName") and device.get("lastName"):
            name = f"{device['firstName']} {device['lastName']}"
        log_attendance(mac_address, name, new_status)

    device_previous_status[mac_address] = new_status
    return True


def check_and_update_devices() -> None:
    """Run one auto-tracking cycle using tiered l2ping detection.

    Flow:
    1. Snapshot connected devices (instant).
    2. Record connected devices as present, then disconnect them to free ACL slots.
    3. Fetch Convex device list (cached for the cycle).
    4. Use DeviceScheduler to pick which MACs to l2ping.
    5. Run l2ping_batch (sequential, disconnect-after-success).
    6. Compute presence decisions and push status updates to Convex.
    7. Housekeeping (cleanup expired, stale pairings, failed pairings).
    """

    global failed_registrations, _cycle_device_cache

    # -- 1. Snapshot connected devices (instant bluetoothctl query) ----------
    connected_devices = scan_all_connected_devices()
    connected_set = set(connected_devices)
    if connected_set:
        logger.info("Found %d actively connected device(s)", len(connected_set))

    now = time.time()

    # -- 2. Record presence signal for connected devices, then disconnect ----
    for mac in connected_set:
        last_presence_signal[mac] = now
        _update_signal_stats(mac, True, "connected", now)

    # Disconnect all connected devices immediately to free ACL slots for l2ping
    if connected_set:
        bluetooth_scanner.disconnect_all_connected(list(connected_set))

    # -- 3. Fetch Convex device list (populate cycle cache) ------------------
    _cycle_device_cache = None  # reset cache
    devices = get_known_devices()
    _cycle_device_cache = devices  # cache for the rest of this cycle

    if not devices:
        logger.warning("No devices found in Convex database")
    else:
        logger.info("Loaded %d device(s) from Convex", len(devices))

    device_map: dict[str, dict[str, Any]] = {}
    registered_macs: set[str] = set()
    pending_macs: set[str] = set()
    for device in devices:
        mac = device.get("macAddress")
        if not mac:
            continue
        device_map[mac] = device
        if device.get("pendingRegistration"):
            pending_macs.add(mac)
        else:
            registered_macs.add(mac)

    _expire_unpublished_devices(now)
    _retry_unpublished_devices(now, device_map, registered_macs, pending_macs)
    overrides = _get_device_overrides(now)

    # -- 4. Tiered l2ping scheduling ----------------------------------------
    l2ping_targets = _device_scheduler.select(
        registered_macs, pending_macs, connected_set, now,
    )

    # -- 5. Run l2ping sequentially (no threading — avoids HCI contention) --
    l2ping_results: dict[str, bool] = {}
    if l2ping_targets:
        l2ping_results = bluetooth_scanner.l2ping_batch(
            l2ping_targets, disconnect_after=True,
        )
    else:
        logger.info("No devices selected for l2ping this cycle")

    # Update signal stats for probed devices
    for mac in l2ping_targets:
        success = l2ping_results.get(mac, False)
        _update_signal_stats(mac, success, "l2ping" if success else None, now)

    presence_signals = {mac for mac, ok in l2ping_results.items() if ok}
    for mac in presence_signals:
        last_presence_signal[mac] = now

    logger.info(
        "l2ping detected %d/%d device(s) in range",
        len(presence_signals),
        len(all_l2ping_targets),
    )

    # -- 6. Compute presence decisions and push updates ----------------------
    desired_presence: dict[str, bool] = {}
    decision_reasons: dict[str, str] = {}
    for mac in registered_macs:
        device = device_map.get(mac, {})
        previous_status = device_previous_status.get(mac) or device.get("status")
        desired_present, reason = _compute_presence_decision(
            mac, now, previous_status, overrides,
        )
        desired_presence[mac] = desired_present
        decision_reasons[mac] = reason
        _log_device_diagnostics(mac, device, now, desired_present, overrides, reason)

    present_set = {mac for mac, is_present in desired_presence.items() if is_present}
    logger.info(
        "Decision engine -> %d/%d registered device(s) marked present (adaptive=%s)",
        len(present_set),
        len(registered_macs),
        ENABLE_ADAPTIVE_HYSTERESIS,
    )

    # Register newly-connected devices that aren't in Convex yet
    newly_registered_count = 0
    for mac in connected_set:
        if mac in device_map:
            failed_registrations.discard(mac)
            continue

        if mac in failed_registrations:
            logger.info("Retrying pending registration for %s", mac)

        device_name = bluetooth_scanner.get_device_name(mac)
        logger.info(
            "New device detected via setup flow: %s (%s)",
            mac,
            device_name or "unknown",
        )
        result = register_new_device(mac, device_name)
        if result:
            newly_registered_count += 1
            failed_registrations.discard(mac)
            device_map[mac] = result
            if result.get("pendingRegistration"):
                pending_macs.add(mac)
            else:
                registered_macs.add(mac)
        else:
            failed_registrations.add(mac)
            _record_unpublished_device(mac, device_name, now)

    # Push status updates to Convex
    updated_count = 0
    for mac, device in device_map.items():
        if device.get("pendingRegistration"):
            continue

        current_status = device.get("status", "unknown")
        is_present_now = mac in present_set
        if update_device_status(mac, is_present_now, current_status, device):
            if current_status != ("present" if is_present_now else "absent"):
                updated_count += 1

    _prune_device_state(set(device_map.keys()))

    # -- 7. Housekeeping -----------------------------------------------------
    cleanup_expired_devices()
    cleanup_stale_bluetooth_pairings()

    # Clean up failed/timeout pairings from bluetooth_agent
    try:
        failed_pairing_addresses = bluetooth_agent.cleanup_failed_pairings()
        if failed_pairing_addresses:
            logger.info("Processing %d failed/timeout pairing(s)", len(failed_pairing_addresses))
            for address in failed_pairing_addresses:
                try:
                    bluetooth_scanner.remove_device(address)
                    device = get_device_by_mac(address)
                    if device and device.get("pendingRegistration"):
                        logger.info("Clearing pending Convex record for failed pairing: %s", address)
                    failed_registrations.discard(address)
                    unpublished_devices.pop(address, None)
                    logger.info("Cleaned up failed pairing: %s", address)
                except Exception as e:
                    logger.error("Error cleaning up failed pairing %s: %s", address, e)
    except Exception as e:
        logger.error("Error during failed pairing cleanup: %s", e)

    # Clear the cycle cache so next cycle fetches fresh data
    _cycle_device_cache = None

    if newly_registered_count:
        logger.info("Registered %d new device(s) as pending", newly_registered_count)
    if updated_count:
        logger.info("Updated %d device status(es) this cycle", updated_count)
    else:
        logger.info("No device status changes in this cycle")


def delete_device_from_convex(mac_address: str) -> bool:
    """Delete a device from Convex using the deleteDevice mutation."""
    result = _convex_call(
        lambda: get_convex_client().mutation(
            "devices:deleteDevice",
            {"macAddress": mac_address},
        )
    )
    if result is None:
        logger.error("Failed to delete device %s from Convex", mac_address)
        return False
    logger.info("Deleted device %s from Convex", mac_address)
    return True


def run_presence_tracker() -> None:
    """
    Main polling loop for the presence tracker.

    Runs continuously, checking device connection status and updating
    Convex as needed.  Uses tiered l2ping scheduling to keep cycle time
    well within the polling interval even with 50+ registered devices.
    """
    logger.info("Starting Presence Tracker")
    logger.info("Polling interval: %ds", POLLING_INTERVAL)
    logger.info("Grace period for new devices: %ds", GRACE_PERIOD_SECONDS)
    logger.info("Presence TTL: %ds", PRESENT_TTL_SECONDS)
    logger.info(
        "Tiered scheduler: active_max=%d warm_batch=%d cold_batch=%d warm_threshold=%ds (sequential l2ping)",
        ACTIVE_TIER_MAX, WARM_TIER_BATCH, COLD_TIER_BATCH, WARM_TIER_THRESHOLD_SECONDS,
    )
    logger.info("Convex query timeout: %ds", CONVEX_QUERY_TIMEOUT)

    # Skip startup cleanup to avoid hanging on Convex connection
    # Stale Bluetooth pairings will be cleaned during polling cycles

    _start_fast_path_consumer()

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
    finally:
        _stop_fast_path_consumer()


def main() -> None:
    """Entry point for the presence tracker."""
    try:
        run_presence_tracker()
    except Exception as e:
        logger.critical(f"Presence tracker crashed: {e}")
        raise


if __name__ == "__main__":
    main()
