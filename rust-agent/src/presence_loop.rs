use crate::bluetooth_probe::{
    disconnect_device, forget_device, get_connected_devices, get_device_name, is_device_paired,
    normalize_mac, probe_device, CommandRunner,
};
use crate::config::Config;
use crate::convex_client::{ConvexClient, DeviceRecord};
use crate::logging;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::Arc;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct AgentState {
    #[serde(default)]
    pub consecutive_misses: HashMap<String, u32>,
    #[serde(default)]
    pub ignored_devices: HashSet<String>,
    #[serde(default)]
    pub pending_first_seen_epoch: HashMap<String, u64>,
}

pub struct PresenceLoop {
    config: Config,
    convex: Arc<ConvexClient>,
    runner: Arc<dyn CommandRunner>,
    state: AgentState,
}

impl PresenceLoop {
    pub fn new(config: Config, convex: Arc<ConvexClient>, runner: Arc<dyn CommandRunner>) -> Self {
        let state = load_state(&config.paths.state_file).unwrap_or_default();
        Self { config, convex, runner, state }
    }

    pub async fn run_forever(&mut self) -> Result<()> {
        logging::info("presence_loop", "startup", None, Some("ok"), "Presence loop started");
        loop {
            if let Err(err) = self.run_cycle().await {
                logging::error("presence_loop", "cycle", None, Some("error"), &err.to_string());
            }

            tokio::select! {
                _ = tokio::signal::ctrl_c() => {
                    logging::info("presence_loop", "shutdown", None, Some("ok"), "Received shutdown signal");
                    break;
                }
                _ = tokio::time::sleep(std::time::Duration::from_secs(self.config.presence.polling_interval_seconds.max(1))) => {}
            }
        }
        Ok(())
    }

    pub async fn run_cycle(&mut self) -> Result<()> {
        let now = now_epoch_seconds();
        let devices = self.convex.get_devices().await?;
        self.cleanup_expired_pending(&devices).await?;
        let devices = self.convex.get_devices().await?;
        let connected: HashSet<String> = get_connected_devices(
            self.runner.as_ref(),
            self.config.bluetooth.command_timeout_seconds,
        )
        .into_iter()
        .map(|m| normalize_mac(&m))
        .collect();

        let mut by_mac: HashMap<String, DeviceRecord> = devices
            .into_iter()
            .map(|d| (normalize_mac(&d.mac_address), d))
            .collect();

        self.apply_pending_grace(now, &by_mac);

        for mac in &connected {
            self.handle_connected(mac, now, &mut by_mac).await?;
            if is_device_paired(
                self.runner.as_ref(),
                mac,
                self.config.bluetooth.command_timeout_seconds,
            ) {
                let _ = disconnect_device(
                    self.runner.as_ref(),
                    mac,
                    self.config.bluetooth.command_timeout_seconds,
                );
            } else {
                logging::info(
                    "presence_loop",
                    "skip_disconnect_unpaired",
                    Some(mac),
                    Some("ok"),
                    "Skipping disconnect for unpaired connected device",
                );
            }
        }

        let mut targets: Vec<String> = by_mac
            .values()
            .filter(|d| !d.pending_registration)
            .map(|d| normalize_mac(&d.mac_address))
            .collect();
        targets.sort();

        for mac in targets {
            if connected.contains(&mac) || self.state.ignored_devices.contains(&mac) {
                continue;
            }

            let present = probe_device(
                self.runner.as_ref(),
                &mac,
                self.config.bluetooth.l2ping_count,
                self.config.bluetooth.l2ping_timeout_seconds,
                self.config.bluetooth.connect_probe_timeout_seconds,
                self.config.bluetooth.command_timeout_seconds,
            );

            if present {
                self.state.consecutive_misses.remove(&mac);
                if let Some(device) = by_mac.get(&mac) {
                    self.transition_status(device, "present").await?;
                }
            } else {
                let misses = self.state.consecutive_misses.entry(mac.clone()).or_insert(0);
                *misses += 1;
                if *misses >= self.config.presence.absent_threshold {
                    if let Some(device) = by_mac.get(&mac) {
                        self.transition_status(device, "absent").await?;
                    }
                }
            }
        }

        self.prune_state(&by_mac);
        save_state(&self.config.paths.state_file, &self.state)?;
        Ok(())
    }

    async fn handle_connected(
        &mut self,
        mac: &str,
        now: u64,
        by_mac: &mut HashMap<String, DeviceRecord>,
    ) -> Result<()> {
        let mac = normalize_mac(mac);

        match by_mac.get(&mac) {
            None => {
                let name = get_device_name(
                    self.runner.as_ref(),
                    &mac,
                    self.config.bluetooth.command_timeout_seconds,
                );
                if let Some(device) = self.convex.register_pending_device(&mac, name.as_deref()).await? {
                    by_mac.insert(mac.clone(), device);
                }
                self.state.pending_first_seen_epoch.entry(mac.clone()).or_insert(now);
                logging::info("presence_loop", "register_pending", Some(&mac), Some("ok"), "Registered pending device");
            }
            Some(device) if device.pending_registration => {
                self.state.pending_first_seen_epoch.entry(mac.clone()).or_insert(now);
            }
            Some(device) => {
                self.state.consecutive_misses.remove(&mac);
                self.transition_status(device, "present").await?;
            }
        }
        Ok(())
    }

    async fn transition_status(&self, device: &DeviceRecord, status: &str) -> Result<()> {
        if device.status == status {
            return Ok(());
        }

        let mac = normalize_mac(&device.mac_address);
        self.convex.update_device_status(&mac, status).await?;
        if !device.pending_registration {
            self.convex
                .log_attendance(&mac, &device.display_name(), status)
                .await?;
        }

        logging::info(
            "presence_loop",
            "status_update",
            Some(&mac),
            Some(status),
            &format!("{} -> {}", device.status, status),
        );
        Ok(())
    }

    fn apply_pending_grace(&mut self, now: u64, by_mac: &HashMap<String, DeviceRecord>) {
        for (mac, device) in by_mac {
            if device.pending_registration {
                let first_seen = self.state.pending_first_seen_epoch.entry(mac.clone()).or_insert(now);
                if now.saturating_sub(*first_seen) >= self.config.presence.grace_period_seconds {
                    self.state.ignored_devices.insert(mac.clone());
                }
            } else {
                self.state.pending_first_seen_epoch.remove(mac);
                self.state.ignored_devices.remove(mac);
            }
        }
    }

    fn prune_state(&mut self, by_mac: &HashMap<String, DeviceRecord>) {
        let known: HashSet<String> = by_mac.keys().cloned().collect();
        self.state.consecutive_misses.retain(|mac, _| known.contains(mac));
        self.state.pending_first_seen_epoch.retain(|mac, _| known.contains(mac));
        self.state.ignored_devices.retain(|mac| known.contains(mac));
    }

    async fn cleanup_expired_pending(&self, devices: &[DeviceRecord]) -> Result<()> {
        let now_ms = now_epoch_millis();
        let grace_ms = self.config.presence.grace_period_seconds.saturating_mul(1000);

        for device in devices {
            if !device.pending_registration {
                continue;
            }

            let grace_end = device.grace_period_end.unwrap_or_else(|| {
                device.first_seen.unwrap_or(now_ms).saturating_add(grace_ms)
            });
            if grace_end > now_ms {
                continue;
            }

            let mac = normalize_mac(&device.mac_address);
            let forgotten = forget_device(
                self.runner.as_ref(),
                &mac,
                self.config.bluetooth.command_timeout_seconds,
            );
            logging::info(
                "presence_loop",
                "forget_expired_pending",
                Some(&mac),
                if forgotten { Some("ok") } else { Some("error") },
                "Expired pending device removed from Bluetooth",
            );

            if let Some(id) = &device.id {
                if let Err(err) = self.convex.delete_device(id).await {
                    logging::warn(
                        "presence_loop",
                        "delete_expired_pending",
                        Some(&mac),
                        Some("error"),
                        &err.to_string(),
                    );
                } else {
                    logging::info(
                        "presence_loop",
                        "delete_expired_pending",
                        Some(&mac),
                        Some("ok"),
                        "Expired pending device removed from Convex",
                    );
                }
            }
        }

        Ok(())
    }
}

fn load_state(path: &Path) -> Result<AgentState> {
    if !path.exists() {
        return Ok(AgentState::default());
    }
    Ok(serde_json::from_str(&fs::read_to_string(path)?)?)
}

fn save_state(path: &Path, state: &AgentState) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, serde_json::to_string_pretty(state)?)?;
    Ok(())
}

fn now_epoch_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn now_epoch_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
