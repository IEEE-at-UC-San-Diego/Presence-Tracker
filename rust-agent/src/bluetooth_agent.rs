use crate::bluetooth_probe::{configure_adapter, get_device_name, is_valid_mac, normalize_mac, trust_device, CommandRunner};
use crate::config::Config;
use crate::convex_client::ConvexClient;
use crate::logging;
use anyhow::Result;
use bluer::agent::{Agent, AgentHandle, ReqError};
use bluer::{AdapterEvent, Address, DeviceEvent, DeviceProperty};
use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio_stream::StreamExt;

type UnitFuture = Pin<Box<dyn Future<Output = Result<(), ReqError>> + Send>>;
pub type PairingGuard = Arc<Mutex<HashSet<String>>>;

pub struct AgentRuntime {
    _session: bluer::Session,
    _handle: AgentHandle,
}

pub async fn start_agent(
    config: &Config,
    runner: Arc<dyn CommandRunner>,
    convex: Arc<ConvexClient>,
    pairing_guard: PairingGuard,
) -> Result<AgentRuntime> {
    configure_adapter(runner.as_ref());
    let session = bluer::Session::new().await?;
    let adapter = session.default_adapter().await?;
    let blocked = to_blocked_uuid_set(&config.bluetooth.audio_block_uuids);
    let command_timeout_seconds = config.bluetooth.command_timeout_seconds;

    let runner_for_auth = runner.clone();
    let blocked_for_auth = blocked.clone();
    let authorize_service = Box::new(move |req: bluer::agent::AuthorizeService| -> UnitFuture {
        let runner = runner_for_auth.clone();
        let blocked = blocked_for_auth.clone();
        Box::pin(async move {
            let mac = req.device.to_string().to_ascii_uppercase();
            let uuid = req.service.to_string().to_ascii_lowercase();

            if blocked.contains(&uuid) {
                logging::warn(
                    "bluetooth_agent",
                    "authorize_service",
                    Some(&mac),
                    Some("rejected"),
                    &format!("Rejected blocked audio UUID {uuid}"),
                );
                return Err(ReqError::Rejected);
            }

            let _ = trust_device(runner.as_ref(), &mac, command_timeout_seconds);
            logging::info(
                "bluetooth_agent",
                "authorize_service",
                Some(&mac),
                Some("accepted"),
                &format!("Accepted UUID {uuid}"),
            );
            Ok(())
        })
    });

    let runner_for_confirmation = runner.clone();
    let convex_for_confirmation = convex.clone();
    let pairing_guard_for_confirmation = pairing_guard.clone();
    let request_confirmation = Box::new(move |req: bluer::agent::RequestConfirmation| -> UnitFuture {
        let runner = runner_for_confirmation.clone();
        let convex = convex_for_confirmation.clone();
        let pairing_guard = pairing_guard_for_confirmation.clone();
        Box::pin(async move {
            let mac = req.device.to_string().to_ascii_uppercase();
            register_paired_device(
                runner.as_ref(),
                convex.as_ref(),
                &mac,
                command_timeout_seconds,
                "request_confirmation",
                pairing_guard.as_ref(),
            )
            .await;
            Ok(())
        })
    });

    let runner_for_authorization = runner.clone();
    let convex_for_authorization = convex.clone();
    let pairing_guard_for_authorization = pairing_guard.clone();
    let request_authorization = Box::new(move |req: bluer::agent::RequestAuthorization| -> UnitFuture {
        let runner = runner_for_authorization.clone();
        let convex = convex_for_authorization.clone();
        let pairing_guard = pairing_guard_for_authorization.clone();
        Box::pin(async move {
            let mac = req.device.to_string().to_ascii_uppercase();
            register_paired_device(
                runner.as_ref(),
                convex.as_ref(),
                &mac,
                command_timeout_seconds,
                "request_authorization",
                pairing_guard.as_ref(),
            )
            .await;
            Ok(())
        })
    });

    let agent = Agent {
        request_default: true,
        authorize_service: Some(authorize_service),
        request_confirmation: Some(request_confirmation),
        request_authorization: Some(request_authorization),
        ..Default::default()
    };

    let handle = session.register_agent(agent).await?;

    logging::info(
        "bluetooth_agent",
        "agent_started",
        None,
        Some("ok"),
        "BlueZ Agent1 registered with audio service blocking",
    );

    let runner_for_events = runner.clone();
    let convex_for_events = convex.clone();
    let pairing_guard_for_events = pairing_guard.clone();
    let adapter_name = adapter.name().to_string();
    tokio::spawn(async move {
        if let Err(e) = monitor_device_events(
            adapter_name,
            runner_for_events,
            convex_for_events,
            pairing_guard_for_events,
            command_timeout_seconds,
        )
        .await
        {
            logging::warn("bluetooth_agent", "monitor_events", None, Some("error"), &e.to_string());
        }
    });

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
    pairing_guard: PairingGuard,
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
        if let AdapterEvent::DeviceAdded(addr) = event {
            let adapter_name = adapter_name.clone();
            let runner = runner.clone();
            let convex = convex.clone();
            let pairing_guard = pairing_guard.clone();
            tokio::spawn(async move {
                watch_for_pairing(adapter_name, addr, runner, convex, pairing_guard, command_timeout_seconds).await;
            });
        }
    }

    Ok(())
}

async fn watch_for_pairing(
    adapter_name: String,
    addr: Address,
    runner: Arc<dyn CommandRunner>,
    convex: Arc<ConvexClient>,
    pairing_guard: PairingGuard,
    command_timeout_seconds: u64,
) {
    let mac = addr.to_string().to_ascii_uppercase();

    let session = match bluer::Session::new().await {
        Ok(session) => session,
        Err(e) => {
            logging::warn("bluetooth_agent", "watch_pairing", Some(&mac), Some("error"), &e.to_string());
            return;
        }
    };

    let adapter = match session.adapter(&adapter_name) {
        Ok(adapter) => adapter,
        Err(e) => {
            logging::warn("bluetooth_agent", "watch_pairing", Some(&mac), Some("error"), &e.to_string());
            return;
        }
    };

    let device = match adapter.device(addr) {
        Ok(device) => device,
        Err(e) => {
            logging::warn("bluetooth_agent", "watch_pairing", Some(&mac), Some("error"), &e.to_string());
            return;
        }
    };

    // Already-paired devices are handled by pair callbacks. Only register on a new
    // Paired=true transition so restarts do not re-add expired pending devices.
    if device.is_paired().await.unwrap_or(false) {
        return;
    }

    let mut device_events = match device.events().await {
        Ok(events) => events,
        Err(e) => {
            logging::warn("bluetooth_agent", "watch_pairing", Some(&mac), Some("error"), &e.to_string());
            return;
        }
    };

    let deadline = tokio::time::Instant::now() + Duration::from_secs(120);
    loop {
        tokio::select! {
            evt = device_events.next() => {
                match evt {
                    Some(DeviceEvent::PropertyChanged(DeviceProperty::Paired(true))) => {
                        register_paired_device(
                            runner.as_ref(),
                            convex.as_ref(),
                            &mac,
                            command_timeout_seconds,
                            "paired_event",
                            pairing_guard.as_ref(),
                        )
                        .await;
                        return;
                    }
                    None => return,
                    _ => {}
                }
            }
            _ = tokio::time::sleep_until(deadline) => return,
        }
    }
}

async fn register_paired_device(
    runner: &dyn CommandRunner,
    convex: &ConvexClient,
    mac: &str,
    command_timeout_seconds: u64,
    source: &str,
    pairing_guard: &Mutex<HashSet<String>>,
) {
    if !is_valid_mac(mac) {
        return;
    }

    let mac = normalize_mac(mac);
    if let Ok(mut guard) = pairing_guard.lock() {
        guard.insert(mac.clone());
    }
    let _ = trust_device(runner, &mac, command_timeout_seconds);
    logging::info(
        "bluetooth_agent",
        "device_paired",
        Some(&mac),
        Some("trusted"),
        &format!("Trusted newly paired device via {source}"),
    );

    let name = get_device_name(runner, &mac, command_timeout_seconds);
    match convex.register_pending_device(&mac, name.as_deref()).await {
        Ok(_) => logging::info(
            "bluetooth_agent",
            "register_pending",
            Some(&mac),
            Some("ok"),
            &format!("Registered pending device via {source}"),
        ),
        Err(e) => logging::warn(
            "bluetooth_agent",
            "register_pending",
            Some(&mac),
            Some("error"),
            &e.to_string(),
        ),
    }

    if let Ok(mut guard) = pairing_guard.lock() {
        guard.remove(&mac);
    }
}

pub fn to_blocked_uuid_set(uuids: &[String]) -> HashSet<String> {
    uuids
        .iter()
        .map(|u| u.trim().to_ascii_lowercase())
        .filter(|u| !u.is_empty())
        .collect()
}
