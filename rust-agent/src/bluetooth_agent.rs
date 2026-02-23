use crate::bluetooth_probe::{configure_adapter, get_device_name, is_valid_mac, normalize_mac, trust_device, CommandRunner};
use crate::config::Config;
use crate::convex_client::ConvexClient;
use crate::logging;
use anyhow::Result;
use bluer::agent::{Agent, AgentHandle};
use bluer::{AdapterEvent, Address};
use tokio_stream::StreamExt;
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
pub struct AgentRuntime {
    _session: bluer::Session,
    _handle: AgentHandle,
}

pub async fn start_agent(config: &Config, runner: Arc<dyn CommandRunner>, convex: Arc<ConvexClient>) -> Result<AgentRuntime> {
    configure_adapter(runner.as_ref());
    let session = bluer::Session::new().await?;
    let adapter = session.default_adapter().await?;
    let blocked = to_blocked_uuid_set(&config.bluetooth.audio_block_uuids);
    let command_timeout_seconds = config.bluetooth.command_timeout_seconds;

    // Register agent with NoInputNoOutput capability (no callbacks = auto-accept all)
    // This enables "Just Works" pairing without PIN prompts
    let agent = Agent {
        request_default: true,
        ..Default::default()
    };

    let handle = session.register_agent(agent).await?;

    logging::info(
        "bluetooth_agent",
        "agent_started",
        None,
        Some("ok"),
        "BlueZ Agent1 registered (NoInputNoOutput capability)",
    );

    // Monitor device events to trust and register newly paired devices
    let runner_for_events = runner.clone();
    let convex_for_events = convex.clone();
    let blocked_for_events = blocked.clone();
    let adapter_name = adapter.name().to_string();
    tokio::spawn(async move {
        if let Err(e) = monitor_device_events(
            adapter_name,
            runner_for_events,
            convex_for_events,
            blocked_for_events,
            command_timeout_seconds,
        ).await {
            logging::warn("bluetooth_agent", "monitor_events", None, Some("error"), &e.to_string());
        }
    });

    // Keep adapter mode refreshed periodically in case BlueZ drifts.
    let runner_for_watchdog = runner.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(60)).await;
            configure_adapter(runner_for_watchdog.as_ref());
        }
    });

    Ok(AgentRuntime {
        _session: session,
        _handle: handle,
    })
}

async fn monitor_device_events(
    adapter_name: String,
    runner: Arc<dyn CommandRunner>,
    convex: Arc<ConvexClient>,
    blocked_uuids: HashSet<String>,
    command_timeout_seconds: u64,
) -> Result<()> {
    let session = bluer::Session::new().await?;
    let adapter = session.adapter(&adapter_name)?;
    let mut events = adapter.discover_devices().await?;

    logging::info(
        "bluetooth_agent",
        "monitor_events",
        None,
        Some("started"),
        "Listening for device events",
    );

    while let Some(event) = events.next().await {
        match event {
            AdapterEvent::DeviceAdded(addr) => {
                handle_device_added(&adapter, addr, &runner, &convex, &blocked_uuids, command_timeout_seconds).await;
            }
            AdapterEvent::PropertyChanged(_) => {
                // Adapter property changed, ignore
            }
            AdapterEvent::DeviceRemoved(_) => {
                // Device removed, ignore
            }
        }
    }

    Ok(())
}

async fn handle_device_added(
    adapter: &bluer::Adapter,
    addr: Address,
    runner: &Arc<dyn CommandRunner>,
    convex: &Arc<ConvexClient>,
    blocked_uuids: &HashSet<String>,
    command_timeout_seconds: u64,
) {
    let mac = addr.to_string().to_ascii_uppercase();
    
    // Get the device to check its properties
    let device = match adapter.device(addr) {
        Ok(d) => d,
        Err(e) => {
            logging::warn("bluetooth_agent", "handle_device", Some(&mac), Some("error"), &format!("Failed to get device: {e}"));
            return;
        }
    };

    // Check if device is paired
    let is_paired = device.is_paired().await.unwrap_or(false);
    if !is_paired {
        return;
    }

    // Check if device is already trusted
    let is_trusted = device.is_trusted().await.unwrap_or(false);
    if is_trusted {
        return;
    }

    logging::info(
        "bluetooth_agent",
        "device_paired",
        Some(&mac),
        Some("detected"),
        "New paired device detected",
    );

    // Check for blocked UUIDs
    if let Ok(uuids) = device.uuids().await {
        if let Some(uuids) = uuids {
            for uuid in &uuids {
                let uuid_str = uuid.to_string().to_ascii_lowercase();
                if blocked_uuids.contains(&uuid_str) {
                    logging::warn(
                        "bluetooth_agent",
                        "device_paired",
                        Some(&mac),
                        Some("blocked"),
                        &format!("Device has blocked UUID {uuid_str}, not trusting"),
                    );
                    return;
                }
            }
        }
    }

    // Trust the device
    if is_valid_mac(&mac) {
        let _ = trust_device(runner.as_ref(), &normalize_mac(&mac), command_timeout_seconds);
        logging::info(
            "bluetooth_agent",
            "device_paired",
            Some(&mac),
            Some("trusted"),
            "Device trusted",
        );
    }

    // Register as pending device
    let name = get_device_name(runner.as_ref(), &mac, command_timeout_seconds);
    match convex.register_pending_device(&mac, name.as_deref()).await {
        Ok(_) => logging::info("bluetooth_agent", "register_pending", Some(&mac), Some("ok"), "Registered pending device"),
        Err(e) => logging::warn("bluetooth_agent", "register_pending", Some(&mac), Some("error"), &e.to_string()),
    }
}

pub fn to_blocked_uuid_set(uuids: &[String]) -> HashSet<String> {
    uuids
        .iter()
        .map(|u| u.trim().to_ascii_lowercase())
        .filter(|u| !u.is_empty())
        .collect()
}
