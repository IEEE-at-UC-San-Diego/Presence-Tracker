import os
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Any
import convex
from dotenv import load_dotenv
import bluetooth_scanner

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

if not CONVEX_DEPLOYMENT_URL:
    raise ValueError(
        "CONVEX_DEPLOYMENT_URL environment variable is not set. "
        "Please create a .env file with this variable."
    )

# Initialize Convex client
convex_client = convex.ConvexClient(CONVEX_DEPLOYMENT_URL)

# Polling interval in seconds
POLLING_INTERVAL = 5

# Grace period for new device registration in seconds
GRACE_PERIOD_SECONDS = int(os.getenv("GRACE_PERIOD_SECONDS", "300"))

# Track connected devices across cycles
recently_connected: set[str] = set()

# Track consecutive failed connection attempts per device
failed_connection_attempts: dict[str, int] = {}

# Threshold for consecutive failed connections before auto-removal
FAILED_CONNECTION_THRESHOLD = 3


def get_known_devices() -> list[dict[str, Any]]:
    """
    Fetch all known devices from Convex using the getDevices function.

    Returns:
        List of device dictionaries with macAddress, name, status, and lastSeen fields
    """
    try:
        result = convex_client.query("devices:getDevices")
        logger.info(f"Retrieved {len(result)} devices from Convex")
        return result
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
    try:
        logger.info(f"→ register_new_device called: mac={mac_address}, name='{name}'")
        result = convex_client.mutation(
            "devices:registerPendingDevice",
            {
                "macAddress": mac_address,
                "name": name or "",
            },
        )
        logger.info(
            f"✓ Registered new device {mac_address} (name='{name or 'unknown'}') in pending state -> {result}"
        )
        return True
    except Exception as e:
        logger.error(f"✗ Error registering new device {mac_address}: {e}")
        return False


def cleanup_expired_devices() -> bool:
    """
    Clean up devices whose grace period has expired and are still pending.
    Also disconnects and removes the Bluetooth pairing for those devices.

    Returns:
        True if cleanup was successful, False otherwise
    """
    try:
        result = convex_client.mutation("devices:cleanupExpiredGracePeriods", {})
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
                except Exception as e:
                    logger.error(f"Error removing Bluetooth device {mac_address}: {e}")
        else:
            logger.debug("No expired grace periods to clean up")
            
        return True
    except Exception as e:
        logger.error(f"Error cleaning up expired devices: {e}")
        return False


def cleanup_stale_bluetooth_pairings() -> None:
    """
    Remove Bluetooth pairings for devices that are no longer in the Convex database.
    """
    try:
        # Get all paired devices from Bluetooth
        paired_devices = bluetooth_scanner.get_paired_devices()
        paired_set = set(paired_devices)
        logger.info(f"Found {len(paired_set)} paired device(s) in Bluetooth")

        # Get all known devices from Convex
        convex_devices = get_known_devices()
        convex_macs = {device.get("macAddress") for device in convex_devices if device.get("macAddress")}
        logger.info(f"Found {len(convex_macs)} device(s) in Convex database")

        # Find devices that are paired but not in Convex
        stale_devices = paired_set - convex_macs

        if stale_devices:
            logger.info(f"Found {len(stale_devices)} stale Bluetooth pairing(s) to clean up")
            for mac_address in stale_devices:
                try:
                    bluetooth_scanner.remove_device(mac_address)
                    logger.info(f"Removed stale Bluetooth pairing: {mac_address}")
                except Exception as e:
                    logger.error(f"Failed to remove stale pairing {mac_address}: {e}")
        else:
            logger.info("No stale Bluetooth pairings found")
    except Exception as e:
        logger.error(f"Error during stale Bluetooth pairing cleanup: {e}")


def scan_all_connected_devices() -> list[str]:
    """
    Get all currently connected Bluetooth devices.

    Returns:
        List of MAC addresses of connected devices
    """
    return bluetooth_scanner.get_all_connected_devices()


def update_device_status(mac_address: str, is_connected: bool) -> bool:
    """
    Update a device's status in Convex using the updateDeviceStatus function.

    Args:
        mac_address: The MAC address of the device to update
        is_connected: True if the device is present (connected), False if absent

    Returns:
        True if the update was successful, False otherwise
    """
    try:
        new_status = "present" if is_connected else "absent"
        result = convex_client.mutation(
            "devices:updateDeviceStatus", {"macAddress": mac_address, "status": new_status}
        )
        logger.info(
            f"Updated device {mac_address} status to {new_status} -> {result}"
        )
        return True
    except Exception as e:
        logger.error(f"Error updating device status for {mac_address}: {e}")
        return False


def check_and_update_devices() -> None:
    """
    Check the connection status of all devices and update Convex.

    - Registers new devices that connect as pending (with grace period)
    - Updates status for all known devices (named or pending)
    - Marks devices as absent when they disconnect
    - Cleans up expired pending devices at end of cycle
    """
    global recently_connected

    # Get all connected Bluetooth devices
    connected_devices = scan_all_connected_devices()
    connected_set = set(connected_devices)

    # Get known devices from Convex
    devices = get_known_devices()

    if not devices:
        logger.warning("No devices found in Convex database")
    else:
        logger.info(f"Found {len(devices)} known device(s) in Convex")

    updated_count = 0
    newly_registered_count = 0

    # Process each connected device
    for mac_address in connected_devices:
        device = get_device_by_mac(mac_address)

        if device:
            # Device exists in Convex
            name = device.get("name")
            current_status = device.get("status", "unknown")

            if name:
                display_name = name
            else:
                display_name = f"[pending] {mac_address}"

            logger.info(f"Checking device: {display_name} ({mac_address})")

            # Update status if changed
            new_status = "present"
            if new_status != current_status:
                logger.info(
                    f"Status changed for {display_name} ({mac_address}): "
                    f"{current_status} -> {new_status}"
                )
                if update_device_status(mac_address, True):
                    updated_count += 1
            else:
                logger.debug(f"No status change for {display_name} ({mac_address}): {current_status}")
        else:
            # New device - register as pending with device name from Bluetooth
            if mac_address not in recently_connected:
                # Get device name from Bluetooth
                device_name = bluetooth_scanner.get_device_name(mac_address)
                logger.info(
                    f"New device detected: {mac_address} ({device_name or 'unknown'}) - registering as pending"
                )
                if register_new_device(mac_address, device_name):
                    newly_registered_count += 1
                    recently_connected.add(mac_address)

    # Track connected devices for next cycle (remove old ones after 2 cycles)
    recently_connected = connected_set

    # Process known devices that are NOT connected
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

        if mac_address not in connected_set:
            logger.info(f"Checking device: {display_name} ({mac_address})")

            # Only auto-connect to registered devices (not pending)
            if name and not device.get("pendingRegistration"):
                # Check if device is paired
                paired_devices = bluetooth_scanner.get_paired_devices()
                if mac_address in paired_devices:
                    logger.info(f"Attempting auto-connect to registered device: {display_name}")
                    if bluetooth_scanner.connect_device(mac_address):
                        # Successfully connected - reset failed attempts counter
                        failed_connection_attempts.pop(mac_address, None)
                        # Successfully connected - update status to present
                        if current_status != "present":
                            logger.info(f"Auto-connected to {display_name}, marking as present")
                            if update_device_status(mac_address, True):
                                updated_count += 1
                        continue  # Skip marking as absent
                    else:
                        # Connection failed - increment counter
                        failed_connection_attempts[mac_address] = (
                            failed_connection_attempts.get(mac_address, 0) + 1
                        )
                        failed_count = failed_connection_attempts[mac_address]
                        logger.warning(
                            f"Failed to auto-connect to {display_name} ({mac_address}) - "
                            f"Attempt {failed_count}/{FAILED_CONNECTION_THRESHOLD}"
                        )
                        # Check if threshold exceeded - just log, don't delete
                        if failed_count >= FAILED_CONNECTION_THRESHOLD:
                            logger.warning(
                                f"Threshold exceeded for {display_name} ({mac_address}) - "
                                f"device remains in database (deletion disabled)"
                            )
                            # Reset counter to avoid spamming the log
                            failed_connection_attempts.pop(mac_address, None)

            # Device is not connected and couldn't auto-connect - mark as absent
            new_status = "absent"
            if new_status != current_status:
                logger.info(
                    f"Status changed for {display_name} ({mac_address}): "
                    f"{current_status} -> {new_status}"
                )
                if update_device_status(mac_address, False):
                    updated_count += 1
            else:
                logger.debug(f"No status change for {display_name} ({mac_address}): {current_status}")

    # Clean up expired grace periods
    cleanup_expired_devices()

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

    Runs continuously with a 60-second polling interval, checking device
    connection status and updating Convex as needed.
    """
    logger.info("Starting IEEE Presence Tracker")
    logger.info(f"Polling interval: {POLLING_INTERVAL} seconds")
    logger.info(f"Grace period for new devices: {GRACE_PERIOD_SECONDS} seconds")

    # Cleanup stale Bluetooth pairings at startup
    cleanup_stale_bluetooth_pairings()

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
