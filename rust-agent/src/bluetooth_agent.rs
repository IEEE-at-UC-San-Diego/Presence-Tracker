use crate::bluetooth_probe::{configure_adapter, get_device_name, trust_device, CommandRunner};
use crate::config::Config;
use crate::convex_client::ConvexClient;
use crate::logging;
use anyhow::Result;
use bluer::agent::{Agent, AgentHandle, ReqError};
use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

type UnitFuture = Pin<Box<dyn Future<Output = Result<(), ReqError>> + Send>>;

pub struct AgentRuntime {
    _session: bluer::Session,
    _handle: AgentHandle,
}

pub async fn start_agent(config: &Config, runner: Arc<dyn CommandRunner>, convex: Arc<ConvexClient>) -> Result<AgentRuntime> {
    configure_adapter(runner.as_ref());
    let session = bluer::Session::new().await?;
    let _adapter = session.default_adapter().await?;
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
                    &format!("Rejected blocked audio service {uuid}"),
                );
                return Err(ReqError::Rejected);
            }

            let _ = trust_device(runner.as_ref(), &mac, command_timeout_seconds);
            logging::info(
                "bluetooth_agent",
                "authorize_service",
                Some(&mac),
                Some("accepted"),
                &format!("Accepted service {uuid}"),
            );
            Ok(())
        })
    });

    let runner_for_confirmation = runner.clone();
    let convex_for_confirmation = convex.clone();
    let request_confirmation = Box::new(move |req: bluer::agent::RequestConfirmation| -> UnitFuture {
        let runner = runner_for_confirmation.clone();
        let convex = convex_for_confirmation.clone();
        Box::pin(async move {
            let mac = req.device.to_string().to_ascii_uppercase();
            let _ = trust_device(runner.as_ref(), &mac, command_timeout_seconds);
            logging::info(
                "bluetooth_agent",
                "request_confirmation",
                Some(&mac),
                Some("accepted"),
                "Accepted pair confirmation",
            );
            register_pending(&convex, runner.as_ref(), &mac, command_timeout_seconds).await;
            Ok(())
        })
    });

    let runner_for_authorization = runner.clone();
    let convex_for_authorization = convex.clone();
    let request_authorization = Box::new(move |req: bluer::agent::RequestAuthorization| -> UnitFuture {
        let runner = runner_for_authorization.clone();
        let convex = convex_for_authorization.clone();
        Box::pin(async move {
            let mac = req.device.to_string().to_ascii_uppercase();
            let _ = trust_device(runner.as_ref(), &mac, command_timeout_seconds);
            logging::info(
                "bluetooth_agent",
                "request_authorization",
                Some(&mac),
                Some("accepted"),
                "Accepted pairing authorization",
            );
            register_pending(&convex, runner.as_ref(), &mac, command_timeout_seconds).await;
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
        "BlueZ Agent1 registered (NoInputNoOutput capability)",
    );

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

async fn register_pending(
    convex: &ConvexClient,
    runner: &dyn CommandRunner,
    mac: &str,
    command_timeout_seconds: u64,
) {
    let name = get_device_name(runner, mac, command_timeout_seconds);
    match convex.register_pending_device(mac, name.as_deref()).await {
        Ok(_) => logging::info("bluetooth_agent", "register_pending", Some(mac), Some("ok"), "Registered pending device"),
        Err(e) => logging::warn("bluetooth_agent", "register_pending", Some(mac), Some("error"), &e.to_string()),
    }
}

pub fn to_blocked_uuid_set(uuids: &[String]) -> HashSet<String> {
    uuids
        .iter()
        .map(|u| u.trim().to_ascii_lowercase())
        .filter(|u| !u.is_empty())
        .collect()
}
