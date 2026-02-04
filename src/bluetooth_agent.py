#!/usr/bin/env python3
"""
Bluetooth Agent for Presence Tracker

This script runs a persistent Bluetooth agent that automatically accepts
pairing requests without requiring a PIN. It uses D-Bus to register as
the default Bluetooth agent and handles pairing, authorization, and
trust requests automatically.

Audio Routing Disabled:
This agent rejects Bluetooth audio profile connections (A2DP, HSP, HFP)
to prevent audio output from being routed to the Raspberry Pi.
"""

import os
import shlex
import subprocess
import threading
import time
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import logging
import signal
import sys

from fast_path_queue import connect_to_queue
from logging_utils import configure_logger

logger = configure_logger(
    logging.getLogger(__name__),
    log_filename="bluetooth_agent.log",
    level=logging.INFO,
)

_fast_path_queue = None
_fast_path_lock = threading.Lock()
_fast_path_connect_next = 0.0
_agent_singleton = None

# D-Bus object paths and interfaces
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/ieee/presence/tracker/agent"
BLUEZ_SERVICE = "org.bluez"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
DEVICE_INTERFACE = "org.bluez.Device1"

# Adapter watchdog tuning (all values in seconds)
ADAPTER_WATCHDOG_INTERVAL_SECONDS = int(os.getenv("ADAPTER_WATCHDOG_INTERVAL_SECONDS", "60"))
ADAPTER_RECOVERY_BACKOFF_SECONDS = int(os.getenv("ADAPTER_RECOVERY_BACKOFF_SECONDS", "5"))
ADVERTISE_NUDGE_COMMAND = os.getenv("ADVERTISE_NUDGE_COMMAND", "bluetoothctl advertise on")
ADVERTISE_SCAN_DURATION_SECONDS = int(os.getenv("ADVERTISE_SCAN_DURATION_SECONDS", "3"))

FAST_PATH_QUEUE_ENABLED = os.getenv("FAST_PATH_QUEUE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
FAST_PATH_QUEUE_RETRY_SECONDS = int(os.getenv("FAST_PATH_QUEUE_RETRY_SECONDS", "5"))

# Pairing timeout configuration
PAIRING_TIMEOUT_SECONDS = int(os.getenv("PAIRING_TIMEOUT_SECONDS", "30"))


class Rejected(dbus.DBusException):
    """Exception for rejected pairing requests."""
    _dbus_error_name = "org.bluez.Error.Rejected"


class BluetoothAgent(dbus.service.Object):
    """
    Bluetooth agent that automatically accepts pairing requests.
    
    This agent implements the org.bluez.Agent1 interface and responds
    to all pairing-related callbacks by accepting the request without
    user interaction.
    """

    def __init__(self, bus, path):
        super().__init__(bus, path)
        self.bus = bus
        self.pending_devices = {}  # {address: {"state": str, "timestamp": float}}
        logger.info(f"Bluetooth agent initialized at {path}")

    def _set_trusted(self, device_path):
        """Set a device as trusted after pairing."""
        try:
            device = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE, device_path),
                "org.freedesktop.DBus.Properties"
            )
            device.Set(DEVICE_INTERFACE, "Trusted", True)
            logger.info(f"Device {device_path} marked as trusted")
        except Exception as e:
            logger.error(f"Error setting device as trusted: {e}")

    def _get_device_info(self, device_path):
        """Get device name and address for logging."""
        try:
            device = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE, device_path),
                "org.freedesktop.DBus.Properties"
            )
            props = device.GetAll(DEVICE_INTERFACE)
            name = props.get("Name", "Unknown")
            address = props.get("Address", "Unknown")
            return f"{name} ({address})"
        except Exception as e:
            logger.error(f"Error getting device info: {e}")
            return device_path

    def _get_device_props(self, device_path):
        """Fetch device properties for pairing decisions."""
        device = dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE, device_path),
            "org.freedesktop.DBus.Properties"
        )
        return device.GetAll(DEVICE_INTERFACE)

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        """Called when the agent is unregistered."""
        logger.info("Agent released")

    def _disconnect_audio_profile(self, device_path, uuid):
        """Disconnect an audio profile from a device."""
        try:
            device = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE, device_path),
                DEVICE_INTERFACE
            )
            device.DisconnectProfile(uuid)
            logger.info(f"Disconnected audio profile {uuid} from {device_path}")
        except Exception as e:
            logger.error(f"Error disconnecting audio profile {uuid}: {e}")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        """Authorize a service connection request."""
        device_info = self._get_device_info(device)
        logger.info(f"==== AuthorizeService START ====")
        logger.info(f"AuthorizeService: {device_info} UUID: {uuid}")

        # DISABLED: Bluetooth audio routing to prevent audio output to Pi
        # Audio profile UUIDs to reject:
        # A2DP (Advanced Audio Distribution Profile): 0000110d-0000-1000-8000-00805f9b34fb
        # HSP (Headset Profile): 00001108-0000-1000-8000-00805f9b34fb
        # HFP (Hands-Free Profile): 0000111e-0000-1000-8000-00805f9b34fb
        # HFP AG (Hands-Free Audio Gateway): 0000111f-0000-1000-8000-00805f9b34fb
        audio_uuids = [
            "0000110d-0000-1000-8000-00805f9b34fb",  # A2DP
            "00001108-0000-1000-8000-00805f9b34fb",  # HSP
            "0000111e-0000-1000-8000-00805f9b34fb",  # HFP
            "0000111f-0000-1000-8000-00805f9b34fb",  # HFP AG
        ]

        is_audio = uuid in audio_uuids
        logger.info(f"Is audio profile: {is_audio}")

        try:
            props = self._get_device_props(device)
            is_paired = props.get("Paired", False)
            is_trusted = props.get("Trusted", False)
            is_connected = props.get("Connected", False)
            logger.info(f"Device state - Paired: {is_paired}, Trusted: {is_trusted}, Connected: {is_connected}")
        except Exception as e:
            logger.error(f"Error reading device props for {device_info}: {e}")
            is_trusted = False

        if is_audio:
            logger.info(f"REJECTING audio service request: {uuid} for {device_info}")
            raise Rejected("Audio profile connection rejected")

        # Ensure the device is paired and trusted for non-audio services
        self._ensure_paired_and_trusted(device)

        # Accept all non-audio service authorizations
        logger.info(f"==== AuthorizeService END (accepting) ====")
        return
    
    def _ensure_paired_and_trusted(self, device_path):
        """Ensure a device is paired and trusted for persistent connection."""
        try:
            device = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE, device_path),
                "org.freedesktop.DBus.Properties"
            )
            device_iface = dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE, device_path),
                DEVICE_INTERFACE
            )
            
            props = device.GetAll(DEVICE_INTERFACE)
            name = props.get("Name", "Unknown")
            address = props.get("Address", "Unknown")
            is_paired = props.get("Paired", False)
            is_trusted = props.get("Trusted", False)
            
            logger.info(f"Device {name} ({address}): Paired={is_paired}, Trusted={is_trusted}")
            
            # If not paired, initiate pairing
            if not is_paired:
                logger.info(f"Initiating pairing with {name} ({address})...")
                try:
                    device_iface.Pair()
                    logger.info(f"Pairing initiated with {name} ({address})")
                except dbus.exceptions.DBusException as e:
                    # "Already Exists" means device is already paired
                    if "Already Exists" in str(e) or "AlreadyExists" in str(e):
                        logger.info(f"Device {name} ({address}) is already paired")
                    elif "InProgress" in str(e):
                        logger.info(f"Pairing already in progress for {name} ({address})")
                    else:
                        logger.warning(f"Pairing failed for {name} ({address}): {e}")
            
            # Set trusted if not already
            if not is_trusted:
                device.Set(DEVICE_INTERFACE, "Trusted", True)
                logger.info(f"Device {name} ({address}) marked as trusted")
                
        except Exception as e:
            logger.error(f"Error ensuring device is paired/trusted: {e}")

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        """Request a PIN code for pairing."""
        device_info = self._get_device_info(device)
        logger.info(f"==== RequestPinCode START ====")
        logger.info(f"RequestPinCode: {device_info}")
        logger.info(f"==== RequestPinCode END (returning empty string) ====")
        # Return empty PIN for NoInputNoOutput capability
        return ""

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        """Request a passkey for pairing."""
        device_info = self._get_device_info(device)
        logger.info(f"==== RequestPasskey START ====")
        logger.info(f"RequestPasskey: {device_info}")
        logger.info(f"==== RequestPasskey END (returning 0) ====")
        # Return 0 for NoInputNoOutput capability
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        """Display a passkey during pairing."""
        device_info = self._get_device_info(device)
        logger.info(f"==== DisplayPasskey START ====")
        logger.info(f"DisplayPasskey: {device_info} Passkey: {passkey:06d} Entered: {entered}")
        logger.info(f"==== DisplayPasskey END ====")

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        """Display a PIN code during pairing."""
        device_info = self._get_device_info(device)
        logger.info(f"==== DisplayPinCode START ====")
        logger.info(f"DisplayPinCode: {device_info} PIN: {pincode}")
        logger.info(f"==== DisplayPinCode END ====")

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        """Confirm a passkey during pairing."""
        device_info = self._get_device_info(device)
        logger.info(f"==== RequestConfirmation START ====")
        logger.info(f"RequestConfirmation: {device_info} Passkey: {passkey:06d}")
        # Track device as pairing_request
        try:
            props = self._get_device_props(device)
            address = props.get("Address")
            if address:
                self.pending_devices[address] = {
                    "state": "pairing_request",
                    "timestamp": time.time(),
                }
                logger.info(f"Device {address} marked as pairing_request")
        except Exception as e:
            logger.warning(f"Failed to track pairing request: {e}")
        # Auto-confirm all pairing requests
        self._set_trusted(device)
        logger.info(f"Pairing confirmed for {device_info}")
        logger.info(f"==== RequestConfirmation END ====")
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        """Authorize a pairing request."""
        device_info = self._get_device_info(device)
        logger.info(f"==== RequestAuthorization START ====")
        logger.info(f"RequestAuthorization: {device_info}")
        # Track device as pairing_request
        try:
            props = self._get_device_props(device)
            address = props.get("Address")
            if address:
                self.pending_devices[address] = {
                    "state": "pairing_request",
                    "timestamp": time.time(),
                }
                logger.info(f"Device {address} marked as pairing_request")
        except Exception as e:
            logger.warning(f"Failed to track pairing request: {e}")
        # Auto-authorize all pairing requests
        self._set_trusted(device)
        logger.info(f"Authorization granted for {device_info}")
        logger.info(f"==== RequestAuthorization END ====")
        return

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        """Cancel a pending pairing operation."""
        logger.info("Pairing cancelled")
        # Mark all pending devices as failed
        failed_addresses = []
        for address, info in list(self.pending_devices.items()):
            if info["state"] in ("pairing_request", "pairing"):
                info["state"] = "failed"
                failed_addresses.append(address)
                logger.info(f"Device {address} marked as failed due to cancellation")
        if failed_addresses:
            logger.info(f"Cancelled pairing for devices: {failed_addresses}")

    def is_paired(self, address: str) -> bool:
        """Check if a device is in paired state."""
        return (
            address in self.pending_devices
            and self.pending_devices[address].get("state") == "paired"
        )

    def reset_device_state(self, address: str) -> bool:
        """Clear a device from pending state (for cleanup)."""
        if address in self.pending_devices:
            state = self.pending_devices[address].get("state")
            del self.pending_devices[address]
            logger.info(f"Device {address} state cleared (was {state})")
            return True
        return False

    def cleanup_failed_pairings(self) -> list[str]:
        """Remove devices in failed/timeout state and returns their addresses.
        
        Returns:
            List of MAC addresses for devices that were in failed or timeout state.
            These addresses can be used to remove devices from bluetoothctl.
        """
        failed_or_timeout = []
        now = time.time()
        
        # First, check for timeouts
        for address, info in list(self.pending_devices.items()):
            age = now - info.get("timestamp", now)
            if (
                info.get("state") in ("pairing_request", "pairing")
                and age > PAIRING_TIMEOUT_SECONDS
            ):
                info["state"] = "timeout"
                logger.info(f"Device {address} marked as timeout (age={age:.1f}s)")
        
        # Then collect and remove failed/timeout devices
        for address in list(self.pending_devices.keys()):
            state = self.pending_devices[address].get("state")
            if state in ("failed", "timeout"):
                failed_or_timeout.append(address)
                del self.pending_devices[address]
                logger.info(f"Removed {address} from pending devices (state={state})")
        
        if failed_or_timeout:
            logger.info(f"Cleaned up {len(failed_or_timeout)} failed/timeout pairings")
        
        return failed_or_timeout


def get_adapter_path(bus):
    """Get the path of the first available Bluetooth adapter."""
    manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, "/"),
        "org.freedesktop.DBus.ObjectManager"
    )
    
    objects = manager.GetManagedObjects()
    for path, interfaces in objects.items():
        if ADAPTER_INTERFACE in interfaces:
            return path
    
    return None


def configure_adapter(bus, adapter_path) -> bool:
    """Configure the Bluetooth adapter for pairing."""
    try:
        adapter = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE, adapter_path),
            "org.freedesktop.DBus.Properties"
        )
        
        # Set adapter properties
        adapter.Set(ADAPTER_INTERFACE, "Powered", True)
        adapter.Set(ADAPTER_INTERFACE, "Discoverable", True)
        adapter.Set(ADAPTER_INTERFACE, "DiscoverableTimeout", dbus.UInt32(0))  # Never timeout
        adapter.Set(ADAPTER_INTERFACE, "Pairable", True)
        adapter.Set(ADAPTER_INTERFACE, "PairableTimeout", dbus.UInt32(0))  # Never timeout
        
        logger.info(f"Adapter {adapter_path} configured for discoverable/pairable mode")
        return True
    except Exception as e:
        logger.error(f"Error configuring adapter: {e}")
        return False


def register_agent(bus, agent_path, capability="NoInputNoOutput"):
    """Register the agent with BlueZ."""
    manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, "/org/bluez"),
        "org.bluez.AgentManager1"
    )
    
    manager.RegisterAgent(agent_path, capability)
    manager.RequestDefaultAgent(agent_path)
    logger.info(f"Agent registered with capability: {capability}")
    return manager


def unregister_agent(bus, agent_path):
    """Unregister the agent from BlueZ."""
    try:
        manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE, "/org/bluez"),
            "org.bluez.AgentManager1"
        )
        manager.UnregisterAgent(agent_path)
        logger.info("Agent unregistered")
    except Exception as e:
        logger.error(f"Error unregistering agent: {e}")


def _maybe_connect_fast_path_queue(force: bool = False):
    global _fast_path_queue, _fast_path_connect_next
    if not FAST_PATH_QUEUE_ENABLED:
        return None
    now = time.time()
    if not force and now < _fast_path_connect_next and _fast_path_queue is None:
        return None
    with _fast_path_lock:
        if _fast_path_queue is not None:
            return _fast_path_queue
        try:
            queue = connect_to_queue()
            _fast_path_queue = queue
            logger.info("Fast-path queue connected")
            return queue
        except Exception as exc:
            _fast_path_queue = None
            _fast_path_connect_next = now + max(1, FAST_PATH_QUEUE_RETRY_SECONDS)
            logger.warning("Fast-path queue unavailable: %s", exc)
            return None


def _publish_fast_path_event(mac: str, name: str | None = None, source: str = "bluetooth_agent"):
    if not FAST_PATH_QUEUE_ENABLED:
        return
    payload = {
        "mac": mac,
        "name": name or "",
        "ts": time.time(),
        "source": source,
    }
    queue = _maybe_connect_fast_path_queue()
    if queue is None:
        return
    try:
        queue.put(payload, block=False)
        logger.info("Enqueued fast-path presence event for %s", mac)
    except Exception as exc:
        logger.warning("Failed to enqueue fast-path event for %s: %s", mac, exc)
        with _fast_path_lock:
            _fast_path_queue = None


def _emit_connected_event(device_path: str, props: dict | None = None):
    if not FAST_PATH_QUEUE_ENABLED:
        return
    if _agent_singleton is None:
        return
    try:
        device_props = props or _agent_singleton._get_device_props(device_path)
    except Exception as exc:
        logger.debug("Unable to fetch device props for %s: %s", device_path, exc)
        return
    if not device_props or not bool(device_props.get("Connected")):
        return
    mac = device_props.get("Address")
    if not isinstance(mac, str) or not mac:
        logger.debug("Fast-path event missing MAC for %s", device_path)
        return
    
    # Only emit if device is in paired state
    if mac in _agent_singleton.pending_devices:
        state = _agent_singleton.pending_devices[mac].get("state")
        if state != "paired":
            logger.debug(
                "Skipping fast-path event for %s (state=%s, not paired)",
                mac,
                state,
            )
            return
    
    name = device_props.get("Name")
    _publish_fast_path_event(mac.upper(), name)


def _interfaces_added_handler(object_path: str, interfaces: dict):
    device_props = interfaces.get(DEVICE_INTERFACE)
    if not device_props:
        return
    
    # Track device state when Paired property is present
    if _agent_singleton is not None:
        address = device_props.get("Address")
        is_paired = bool(device_props.get("Paired", False))
        if address:
            existing_state = _agent_singleton.pending_devices.get(address, {}).get("state")
            if is_paired and existing_state in ("pairing_request", "pairing"):
                _agent_singleton.pending_devices[address]["state"] = "paired"
                logger.info(f"Device {address} marked as paired (InterfacesAdded)")
            elif not is_paired and existing_state:
                _agent_singleton.pending_devices[address]["state"] = "failed"
                logger.info(f"Device {address} marked as failed (InterfacesAdded)")
    
    if bool(device_props.get("Connected")):
        _emit_connected_event(object_path, device_props)


def _properties_changed_handler(interface: str, changed: dict, invalidated, path=None):
    if interface != DEVICE_INTERFACE or not changed:
        return
    
    if _agent_singleton is not None:
        # Track pairing success/failure when Paired property changes
        if "Paired" in changed:
            try:
                device = dbus.Interface(
                    _agent_singleton.bus.get_object(BLUEZ_SERVICE, path),
                    "org.freedesktop.DBus.Properties"
                )
                props = device.GetAll(DEVICE_INTERFACE)
                address = props.get("Address")
                is_paired = bool(changed.get("Paired"))
                
                if address and address in _agent_singleton.pending_devices:
                    if is_paired:
                        _agent_singleton.pending_devices[address]["state"] = "paired"
                        logger.info(f"Device {address} marked as paired")
                    else:
                        _agent_singleton.pending_devices[address]["state"] = "failed"
                        logger.info(f"Device {address} marked as failed (unpaired)")
            except Exception as e:
                logger.warning(f"Failed to track pairing state change: {e}")
    
    # Only emit connected event after device is paired
    if "Connected" in changed and bool(changed.get("Connected")):
        _emit_connected_event(path)


def signal_handler(signum, frame):
    """Handle termination signals."""
    logger.info(f"Received signal {signum}, shutting down...")
    mainloop.quit()


def _get_adapter_properties(bus, adapter_path):
    adapter = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE, adapter_path),
        "org.freedesktop.DBus.Properties"
    )
    return adapter.GetAll(ADAPTER_INTERFACE)


def _nudge_le_advertising():
    command = ADVERTISE_NUDGE_COMMAND.strip()
    if not command:
        return
    try:
        args = shlex.split(command)
    except ValueError as exc:
        logger.error(f"Invalid ADVERTISE_NUDGE_COMMAND '{command}': {exc}")
        return

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        logger.info(
            "Reissued advertising command (%s). stdout='%s' stderr='%s'",
            command,
            result.stdout.strip(),
            result.stderr.strip(),
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Advertising command failed (%s): %s",
            command,
            exc,
        )
    except FileNotFoundError:
        logger.error("Advertising command not found: %s", args[0])
    except Exception as exc:
        logger.error("Unexpected error running advertising command (%s): %s", command, exc)


def _pulse_discovery_scan(duration_seconds: int) -> None:
    duration = max(0, duration_seconds)
    if duration == 0:
        return
    try:
        subprocess.run(
            ["bluetoothctl", "--timeout", str(duration), "scan", "on"],
            capture_output=True,
            text=True,
            timeout=duration + 2,
            check=True,
        )
        logger.debug("Triggered bluetoothctl scan pulse for %ss", duration)
    except subprocess.CalledProcessError as exc:
        logger.warning("bluetoothctl scan pulse failed: %s", exc)
    except FileNotFoundError:
        logger.error("bluetoothctl command not found while pulsing scan")
    except Exception as exc:
        logger.error("Unexpected error during scan pulse: %s", exc)


def _adapter_watchdog_callback(context):
    bus, adapter_path = context
    try:
        props = _get_adapter_properties(bus, adapter_path)
    except Exception as exc:
        logger.error(f"Adapter watchdog failed to read properties: {exc}")
        return True

    powered = bool(props.get("Powered"))
    discoverable = bool(props.get("Discoverable"))
    pairable = bool(props.get("Pairable"))
    discoverable_timeout = int(props.get("DiscoverableTimeout", 0))
    pairable_timeout = int(props.get("PairableTimeout", 0))

    healthy = (
        powered
        and discoverable
        and pairable
        and discoverable_timeout == 0
        and pairable_timeout == 0
    )

    logger.debug(
        "Adapter watchdog state -> powered=%s discoverable=%s pairable=%s disc_to=%s pair_to=%s",
        powered,
        discoverable,
        pairable,
        discoverable_timeout,
        pairable_timeout,
    )

    if healthy:
        return True

    logger.warning(
        "Adapter watchdog detected drift (powered=%s discoverable=%s pairable=%s disc_to=%s pair_to=%s)",
        powered,
        discoverable,
        pairable,
        discoverable_timeout,
        pairable_timeout,
    )

    configure_adapter(bus, adapter_path)

    try:
        props = _get_adapter_properties(bus, adapter_path)
    except Exception as exc:
        logger.error(f"Adapter watchdog recheck failed: {exc}")
        return True

    powered = bool(props.get("Powered"))
    discoverable = bool(props.get("Discoverable"))
    pairable = bool(props.get("Pairable"))
    discoverable_timeout = int(props.get("DiscoverableTimeout", 0))
    pairable_timeout = int(props.get("PairableTimeout", 0))

    healthy_after_reconfigure = (
        powered
        and discoverable
        and pairable
        and discoverable_timeout == 0
        and pairable_timeout == 0
    )

    if healthy_after_reconfigure:
        logger.info("Adapter watchdog restored discoverable mode")
        return True

    logger.warning(
        "Adapter watchdog still sees degraded state after reconfigure (powered=%s discoverable=%s pairable=%s)",
        powered,
        discoverable,
        pairable,
    )

    _nudge_le_advertising()
    _pulse_discovery_scan(ADVERTISE_SCAN_DURATION_SECONDS)

    return True


def main():
    """Main entry point for the Bluetooth agent."""
    global mainloop
    
    logger.info("Starting Presence Tracker Bluetooth Agent")
    
    # Initialize D-Bus main loop
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    
    # Get the system bus
    bus = dbus.SystemBus()
    
    # Find the Bluetooth adapter
    adapter_path = get_adapter_path(bus)
    if not adapter_path:
        logger.error("No Bluetooth adapter found!")
        sys.exit(1)
    
    logger.info(f"Found Bluetooth adapter: {adapter_path}")
    
    # Configure the adapter
    configure_adapter(bus, adapter_path)
    
    # Create and register the agent
    agent = BluetoothAgent(bus, AGENT_PATH)
    global _agent_singleton
    _agent_singleton = agent
    register_agent(bus, AGENT_PATH)

    if FAST_PATH_QUEUE_ENABLED:
        manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE, "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        manager.connect_to_signal("InterfacesAdded", _interfaces_added_handler)
        bus.add_signal_receiver(
            _properties_changed_handler,
            signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            path_keyword="path",
        )
        logger.info("Fast-path listeners armed for BlueZ connection events")

    if ADAPTER_WATCHDOG_INTERVAL_SECONDS > 0:
        GLib.timeout_add_seconds(
            max(1, ADAPTER_WATCHDOG_INTERVAL_SECONDS),
            _adapter_watchdog_callback,
            (bus, adapter_path),
        )
        logger.info(
            "Adapter watchdog armed (interval=%ss)",
            ADAPTER_WATCHDOG_INTERVAL_SECONDS,
        )
    else:
        logger.info("Adapter watchdog disabled (interval=%s)", ADAPTER_WATCHDOG_INTERVAL_SECONDS)
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("Bluetooth agent is running. Waiting for pairing requests...")
    logger.info("Press Ctrl+C to stop.")
    
    # Run the main loop
    mainloop = GLib.MainLoop()
    
    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        unregister_agent(bus, AGENT_PATH)
        logger.info("Bluetooth agent stopped")


def is_paired(address: str) -> bool:
    """Check if a device is in paired state.

    Args:
        address: The MAC address of the device to check.

    Returns:
        True if device is paired, False otherwise or if agent is not initialized.
    """
    if _agent_singleton is None:
        return False
    return _agent_singleton.is_paired(address)


def reset_device_state(address: str) -> bool:
    """Clear a device from pending state (for cleanup).

    Args:
        address: The MAC address of the device to reset.

    Returns:
        True if device was removed from pending state, False otherwise
        or if agent is not initialized.
    """
    if _agent_singleton is None:
        return False
    return _agent_singleton.reset_device_state(address)


def cleanup_failed_pairings() -> list[str]:
    """Remove devices in failed/timeout state and returns their addresses.

    Returns:
        List of MAC addresses for devices that were in failed or timeout state.
        Returns empty list if agent is not initialized.
    """
    if _agent_singleton is None:
        return []
    return _agent_singleton.cleanup_failed_pairings()


if __name__ == "__main__":
    main()
