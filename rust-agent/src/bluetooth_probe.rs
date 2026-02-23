use anyhow::{anyhow, Result};
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct CommandOutput {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

pub trait CommandRunner: Send + Sync {
    fn run(&self, program: &str, args: &[&str], timeout: Duration) -> Result<CommandOutput>;
}

#[derive(Debug, Default)]
pub struct ProcessRunner;

impl CommandRunner for ProcessRunner {
    fn run(&self, program: &str, args: &[&str], timeout: Duration) -> Result<CommandOutput> {
        let mut command = Command::new("timeout");
        command
            .arg(format!("{}s", timeout.as_secs().max(1)))
            .arg(program)
            .args(args);

        let output = command
            .output()
            .map_err(|e| anyhow!("failed running {program}: {e}"))?;

        Ok(CommandOutput {
            code: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

pub fn normalize_mac(mac: &str) -> String {
    mac.trim().to_ascii_uppercase()
}

pub fn is_valid_mac(mac: &str) -> bool {
    let parts: Vec<&str> = mac.trim().split(':').collect();
    parts.len() == 6
        && parts
            .iter()
            .all(|part| part.len() == 2 && part.chars().all(|c| c.is_ascii_hexdigit()))
}

pub fn get_connected_devices(runner: &dyn CommandRunner, timeout_seconds: u64) -> Vec<String> {
    let output = runner
        .run(
            "bluetoothctl",
            &["devices", "Connected"],
            Duration::from_secs(timeout_seconds.max(1)),
        )
        .ok();

    match output {
        Some(out) if out.code == 0 => out
            .stdout
            .lines()
            .filter_map(|line| {
                let fields: Vec<&str> = line.split_whitespace().collect();
                if fields.len() >= 2 && fields[0] == "Device" && is_valid_mac(fields[1]) {
                    Some(normalize_mac(fields[1]))
                } else {
                    None
                }
            })
            .collect(),
        _ => Vec::new(),
    }
}

pub fn get_device_name(runner: &dyn CommandRunner, mac: &str, timeout_seconds: u64) -> Option<String> {
    if !is_valid_mac(mac) {
        return None;
    }
    let mac = normalize_mac(mac);
    let out = runner
        .run(
            "bluetoothctl",
            &["info", mac.as_str()],
            Duration::from_secs(timeout_seconds.max(1)),
        )
        .ok()?;
    if out.code != 0 {
        return None;
    }
    out.stdout
        .lines()
        .find_map(|line| line.trim().strip_prefix("Name:").map(|s| s.trim().to_string()))
}

pub fn is_device_paired(runner: &dyn CommandRunner, mac: &str, timeout_seconds: u64) -> bool {
    if !is_valid_mac(mac) {
        return false;
    }
    let mac = normalize_mac(mac);
    let out = match runner.run(
        "bluetoothctl",
        &["info", mac.as_str()],
        Duration::from_secs(timeout_seconds.max(1)),
    ) {
        Ok(out) => out,
        Err(_) => return false,
    };
    if out.code != 0 {
        return false;
    }

    out.stdout
        .lines()
        .any(|line| line.trim().eq_ignore_ascii_case("Paired: yes"))
}

pub fn disconnect_device(runner: &dyn CommandRunner, mac: &str, timeout_seconds: u64) -> bool {
    if !is_valid_mac(mac) {
        return false;
    }
    let mac = normalize_mac(mac);
    match runner.run(
        "bluetoothctl",
        &["disconnect", mac.as_str()],
        Duration::from_secs(timeout_seconds.max(1)),
    ) {
        Ok(out) => out.code == 0 || out.stdout.contains("Successful disconnected"),
        Err(_) => false,
    }
}

pub fn trust_device(runner: &dyn CommandRunner, mac: &str, timeout_seconds: u64) -> bool {
    if !is_valid_mac(mac) {
        return false;
    }
    let mac = normalize_mac(mac);
    runner
        .run(
            "bluetoothctl",
            &["trust", mac.as_str()],
            Duration::from_secs(timeout_seconds.max(1)),
        )
        .map(|out| out.code == 0)
        .unwrap_or(false)
}

pub fn configure_adapter(runner: &dyn CommandRunner) {
    let commands: &[(&str, &[&str])] = &[
        ("bluetoothctl", &["--timeout", "5", "power", "on"]),
        ("bluetoothctl", &["--timeout", "5", "discoverable", "on"]),
        ("bluetoothctl", &["--timeout", "5", "pairable", "on"]),
        ("bluetoothctl", &["--timeout", "5", "discoverable-timeout", "0"]),
        ("bluetoothctl", &["--timeout", "5", "pairable-timeout", "0"]),
        // Enable Secure Simple Pairing mode for "Just Works" auto-pairing
        ("hciconfig", &["hci0", "sspmode", "1"]),
    ];
    for (program, args) in commands {
        let _ = runner.run(program, args, Duration::from_secs(7));
    }
}

pub fn l2ping_device(runner: &dyn CommandRunner, mac: &str, count: u32, timeout_seconds: u64) -> bool {
    if !is_valid_mac(mac) {
        return false;
    }

    let mac = normalize_mac(mac);
    let count = count.max(1).to_string();
    let timeout = timeout_seconds.max(1).to_string();
    let args = ["-c", count.as_str(), "-t", timeout.as_str(), mac.as_str()];

    match runner.run("l2ping", &args, Duration::from_secs(timeout_seconds.max(1) + 1)) {
        Ok(out) => out.code == 0 && out.stdout.to_ascii_lowercase().contains("bytes from"),
        Err(_) => false,
    }
}

pub fn connect_probe(runner: &dyn CommandRunner, mac: &str, timeout_seconds: u64) -> bool {
    if !is_valid_mac(mac) {
        return false;
    }
    let mac = normalize_mac(mac);
    match runner.run(
        "bluetoothctl",
        &["connect", mac.as_str()],
        Duration::from_secs(timeout_seconds.max(1)),
    ) {
        Ok(out) => out.code == 0 || out.stdout.contains("Connected: yes") || out.stdout.contains("Connection successful"),
        Err(_) => false,
    }
}

pub fn probe_device(
    runner: &dyn CommandRunner,
    mac: &str,
    l2ping_count: u32,
    l2ping_timeout_seconds: u64,
    connect_probe_timeout_seconds: u64,
    command_timeout_seconds: u64,
) -> bool {
    let l2_ok = l2ping_device(runner, mac, l2ping_count, l2ping_timeout_seconds);
    let present = if l2_ok {
        true
    } else {
        connect_probe(runner, mac, connect_probe_timeout_seconds)
    };
    let _ = disconnect_device(runner, mac, command_timeout_seconds);
    present
}
