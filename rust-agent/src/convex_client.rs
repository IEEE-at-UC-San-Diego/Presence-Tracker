use anyhow::{anyhow, Context, Result};
use reqwest::header::{HeaderMap, HeaderValue, CONTENT_TYPE};
use serde::Deserialize;
use serde_json::{json, Value};
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct ConvexClient {
    base_url: String,
    admin_key: Option<String>,
    http: reqwest::Client,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DeviceRecord {
    pub id: Option<String>,
    pub mac_address: String,
    pub first_name: Option<String>,
    pub last_name: Option<String>,
    pub name: Option<String>,
    pub status: String,
    pub last_seen: Option<u64>,
    pub connected_since: Option<u64>,
    pub pending_registration: bool,
    pub first_seen: Option<u64>,
    pub grace_period_end: Option<u64>,
}

impl DeviceRecord {
    pub fn display_name(&self) -> String {
        match (&self.first_name, &self.last_name) {
            (Some(first), Some(last)) if !first.is_empty() || !last.is_empty() => format!("{} {}", first, last).trim().to_string(),
            _ => self.name.clone().filter(|n| !n.trim().is_empty()).unwrap_or_else(|| self.mac_address.clone()),
        }
    }
}

impl ConvexClient {
    pub fn new(base_url: impl Into<String>, admin_key: Option<String>) -> Result<Self> {
        let base_url = base_url.into().trim_end_matches('/').to_string();
        if base_url.is_empty() {
            return Err(anyhow!("Convex base URL cannot be empty"));
        }

        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(15))
            .build()
            .context("failed building HTTP client")?;

        Ok(Self { base_url, admin_key, http })
    }

    pub async fn get_devices(&self) -> Result<Vec<DeviceRecord>> {
        let value = self.call("query", "devices:getDevices", json!({})).await?;
        let arr = value
            .as_array()
            .ok_or_else(|| anyhow!("devices:getDevices did not return an array"))?;
        Ok(arr.iter().filter_map(value_to_device).collect())
    }

    pub async fn register_pending_device(&self, mac: &str, name: Option<&str>) -> Result<Option<DeviceRecord>> {
        let args = json!({
            "macAddress": mac,
            "name": name.unwrap_or("")
        });

        let value = self.call("mutation", "devices:registerPendingDevice", args).await?;
        if value.is_null() {
            return Ok(None);
        }
        Ok(value_to_device(&value))
    }

    pub async fn update_device_status(&self, mac: &str, status: &str) -> Result<()> {
        let args = json!({
            "macAddress": mac,
            "status": status
        });
        let _ = self.call("mutation", "devices:updateDeviceStatus", args).await?;
        Ok(())
    }

    pub async fn delete_device(&self, id: &str) -> Result<()> {
        let args = json!({ "id": id });
        let _ = self.call("mutation", "devices:deleteDevice", args).await?;
        Ok(())
    }

    pub async fn log_attendance(&self, mac: &str, user_name: &str, status: &str) -> Result<()> {
        let args = json!({
            "userId": mac,
            "userName": user_name,
            "status": status,
            "deviceId": mac
        });
        let _ = self.call("mutation", "devices:logAttendance", args).await?;
        Ok(())
    }

    async fn call(&self, endpoint: &str, path: &str, args: Value) -> Result<Value> {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));

        if let Some(key) = &self.admin_key {
            let auth_value = format!("Convex {key}");
            let header = HeaderValue::from_str(&auth_value)
                .context("invalid admin key for Authorization header")?;
            headers.insert("Authorization", header);
        }

        let url = format!("{}/api/{}", self.base_url, endpoint);
        let payload = json!({
            "path": path,
            "args": args,
            "format": "json"
        });

        let response = self.http.post(url).headers(headers).json(&payload).send().await.with_context(|| format!("Convex call failed for {path}"))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            return Err(anyhow!("Convex {} {} failed: {} {}", endpoint, path, status, body));
        }

        let body: Value = response.json().await.with_context(|| format!("invalid JSON in Convex response for {path}"))?;

        if let Some(value) = body.get("value") {
            Ok(value.clone())
        } else {
            Ok(body)
        }
    }
}

fn value_to_device(v: &Value) -> Option<DeviceRecord> {
    let obj = v.as_object()?;
    let mac = get_str(obj, "macAddress")?;
    let status = get_str(obj, "status").unwrap_or_else(|| "absent".to_string());
    Some(DeviceRecord {
        id: get_str(obj, "_id"),
        mac_address: mac,
        first_name: get_str(obj, "firstName"),
        last_name: get_str(obj, "lastName"),
        name: get_str(obj, "name"),
        status,
        last_seen: obj.get("lastSeen").and_then(|v| v.as_u64()),
        connected_since: obj.get("connectedSince").and_then(|v| v.as_u64()),
        pending_registration: obj.get("pendingRegistration").and_then(|v| v.as_bool()).unwrap_or(false),
        first_seen: obj.get("firstSeen").and_then(|v| v.as_u64()),
        grace_period_end: obj.get("gracePeriodEnd").and_then(|v| v.as_u64()),
    })
}

fn get_str(obj: &serde_json::Map<String, Value>, key: &str) -> Option<String> {
    obj.get(key).and_then(|v| v.as_str()).map(|s| s.to_string())
}
