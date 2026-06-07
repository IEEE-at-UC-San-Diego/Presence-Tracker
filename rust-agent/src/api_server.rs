use crate::bluetooth_probe::{forget_device, is_valid_mac, normalize_mac, CommandRunner};
use crate::logging;
use anyhow::Result;
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

pub async fn run(runner: Arc<dyn CommandRunner>, port: u16, command_timeout_seconds: u64) -> Result<()> {
    let listener = TcpListener::bind(format!("0.0.0.0:{port}")).await?;
    logging::info(
        "api_server",
        "listening",
        None,
        Some("ok"),
        &format!("Agent API on 0.0.0.0:{port}"),
    );

    loop {
        let (mut socket, _) = listener.accept().await?;
        let runner = runner.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(&mut socket, runner.as_ref(), command_timeout_seconds).await {
                logging::warn("api_server", "request", None, Some("error"), &err.to_string());
            }
        });
    }
}

async fn handle_connection(
    socket: &mut tokio::net::TcpStream,
    runner: &dyn CommandRunner,
    command_timeout_seconds: u64,
) -> Result<()> {
    let mut buffer = vec![0u8; 8192];
    let n = socket.read(&mut buffer).await?;
    let request = String::from_utf8_lossy(&buffer[..n]);

    let (status_code, status_text, body) = if !request.starts_with("POST /api/forget-device") {
        (404, "Not Found", r#"{"error":"Not Found"}"#.to_string())
    } else {
        let payload = request.split("\r\n\r\n").nth(1).unwrap_or("");
        let mac = serde_json::from_str::<serde_json::Value>(payload)
            .ok()
            .and_then(|v| v.get("macAddress").and_then(|m| m.as_str()).map(str::to_string));

        match mac {
            Some(mac) if is_valid_mac(&mac) => {
                let mac = normalize_mac(&mac);
                let ok = forget_device(runner, &mac, command_timeout_seconds);
                logging::info(
                    "api_server",
                    "forget_device",
                    Some(&mac),
                    if ok { Some("ok") } else { Some("error") },
                    if ok { "Device removed from Bluetooth" } else { "Bluetooth remove failed" },
                );
                if ok {
                    (200, "OK", r#"{"success":true}"#.to_string())
                } else {
                    (500, "Internal Server Error", r#"{"error":"Bluetooth remove failed"}"#.to_string())
                }
            }
            _ => (400, "Bad Request", r#"{"error":"Invalid macAddress"}"#.to_string()),
        }
    };

    let response = format!(
        "HTTP/1.1 {status_code} {status_text}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    socket.write_all(response.as_bytes()).await?;
    Ok(())
}
