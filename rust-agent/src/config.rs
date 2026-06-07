use anyhow::{anyhow, Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Parser)]
#[command(name = "presence-tracker-rs")]
pub struct Cli {
    #[arg(long, default_value = "config/agent.toml")]
    pub config: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Config {
    #[serde(default)]
    pub convex: ConvexConfig,
    #[serde(default)]
    pub presence: PresenceConfig,
    #[serde(default)]
    pub bluetooth: BluetoothConfig,
    #[serde(default)]
    pub logging: LoggingConfig,
    #[serde(default)]
    pub paths: PathsConfig,
    #[serde(default)]
    pub api: ApiConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConvexConfig {
    #[serde(default)]
    pub deployment_url: String,
    pub admin_key: Option<String>,
}

impl Default for ConvexConfig {
    fn default() -> Self {
        Self {
            deployment_url: String::new(),
            admin_key: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresenceConfig {
    #[serde(default = "poll_interval_default")]
    pub polling_interval_seconds: u64,
    #[serde(default = "absent_threshold_default")]
    pub absent_threshold: u32,
    #[serde(default = "grace_period_default")]
    pub grace_period_seconds: u64,
}

impl Default for PresenceConfig {
    fn default() -> Self {
        Self {
            polling_interval_seconds: poll_interval_default(),
            absent_threshold: absent_threshold_default(),
            grace_period_seconds: grace_period_default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BluetoothConfig {
    #[serde(default = "l2ping_timeout_default")]
    pub l2ping_timeout_seconds: u64,
    #[serde(default = "l2ping_count_default")]
    pub l2ping_count: u32,
    #[serde(default = "connect_probe_timeout_default")]
    pub connect_probe_timeout_seconds: u64,
    #[serde(default = "command_timeout_default")]
    pub command_timeout_seconds: u64,
    #[serde(default = "default_audio_block_uuids")]
    pub audio_block_uuids: Vec<String>,
}

impl Default for BluetoothConfig {
    fn default() -> Self {
        Self {
            l2ping_timeout_seconds: l2ping_timeout_default(),
            l2ping_count: l2ping_count_default(),
            connect_probe_timeout_seconds: connect_probe_timeout_default(),
            command_timeout_seconds: command_timeout_default(),
            audio_block_uuids: default_audio_block_uuids(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoggingConfig {
    #[serde(default = "log_file_default")]
    pub log_file: PathBuf,
    #[serde(default = "max_lines_default")]
    pub max_lines: usize,
}

impl Default for LoggingConfig {
    fn default() -> Self {
        Self {
            log_file: log_file_default(),
            max_lines: max_lines_default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PathsConfig {
    #[serde(default = "state_file_default")]
    pub state_file: PathBuf,
}

impl Default for PathsConfig {
    fn default() -> Self {
        Self {
            state_file: state_file_default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiConfig {
    #[serde(default = "api_enabled_default")]
    pub enabled: bool,
    #[serde(default = "api_port_default")]
    pub port: u16,
}

impl Default for ApiConfig {
    fn default() -> Self {
        Self {
            enabled: api_enabled_default(),
            port: api_port_default(),
        }
    }
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        dotenvy::dotenv().ok();

        if !path.exists() {
            let mut cfg = Config::default();
            cfg.convex.deployment_url = infer_convex_url_from_env().unwrap_or_default();
            cfg.convex.admin_key = std::env::var("CONVEX_SELF_HOSTED_ADMIN_KEY").ok();
            cfg.write(path)?;
            return cfg.validate();
        }

        let raw = fs::read_to_string(path).with_context(|| format!("failed reading {}", path.display()))?;
        let cfg: Config = toml::from_str(&raw).with_context(|| format!("invalid TOML at {}", path.display()))?;
        cfg.validate()
    }

    pub fn write(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, toml::to_string_pretty(self)?)?;
        Ok(())
    }

    fn validate(mut self) -> Result<Self> {
        if self.convex.deployment_url.trim().is_empty() {
            self.convex.deployment_url = infer_convex_url_from_env().unwrap_or_default();
        }
        if self.convex.deployment_url.trim().is_empty() {
            return Err(anyhow!("convex.deployment_url is required"));
        }

        if self.convex.admin_key.as_deref().map(|v| v.trim().is_empty()).unwrap_or(false) {
            self.convex.admin_key = None;
        }

        self.convex.deployment_url = self.convex.deployment_url.trim_end_matches('/').to_string();
        self.logging.max_lines = self.logging.max_lines.max(1);
        Ok(self)
    }
}
fn infer_convex_url_from_env() -> Option<String> {
    std::env::var("CONVEX_SELF_HOSTED_URL")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| std::env::var("CONVEX_DEPLOYMENT_URL").ok().filter(|v| !v.trim().is_empty()))
}

fn poll_interval_default() -> u64 { 15 }
fn absent_threshold_default() -> u32 { 3 }
fn grace_period_default() -> u64 { 300 }
fn l2ping_timeout_default() -> u64 { 2 }
fn l2ping_count_default() -> u32 { 1 }
fn connect_probe_timeout_default() -> u64 { 3 }
fn command_timeout_default() -> u64 { 10 }
fn api_enabled_default() -> bool { true }
fn api_port_default() -> u16 { 3133 }
fn max_lines_default() -> usize { 1000 }
fn log_file_default() -> PathBuf { PathBuf::from("logs/presence_tracker.log") }
fn state_file_default() -> PathBuf { PathBuf::from("config/agent_state.json") }

fn default_audio_block_uuids() -> Vec<String> {
    ["00001108-0000-1000-8000-00805f9b34fb", "0000110a-0000-1000-8000-00805f9b34fb", "0000110b-0000-1000-8000-00805f9b34fb", "0000110c-0000-1000-8000-00805f9b34fb", "0000110d-0000-1000-8000-00805f9b34fb", "0000110e-0000-1000-8000-00805f9b34fb", "0000110f-0000-1000-8000-00805f9b34fb", "00001112-0000-1000-8000-00805f9b34fb", "0000111e-0000-1000-8000-00805f9b34fb", "0000111f-0000-1000-8000-00805f9b34fb"]
        .iter()
        .map(|v| v.to_string())
        .collect()
}
