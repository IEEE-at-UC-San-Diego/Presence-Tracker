#!/usr/bin/env bun

const http = require("http");
const path = require("path");
const fs = require("fs/promises");

const FRONTEND_DIR = __dirname;
const PORT = Number(process.env.PORT || 3132);
const HOST = "0.0.0.0";

function resolveConvexUrl() {
  const mode = (process.env.CONVEX_URL_MODE || "convex").toLowerCase();
  if (mode === "selfhosted") {
    return process.env.CONVEX_SELF_HOSTED_URL || "";
  }
  return process.env.CONVEX_DEPLOYMENT_URL || "";
}

const convexUrl = resolveConvexUrl();
const deploymentMode = process.env.DEPLOYMENT_MODE || process.env.CONVEX_URL_MODE || "convex";
const organizationName = process.env.ORGANIZATION_NAME || "Presence Tracker";
const agentApiUrl = (process.env.AGENT_API_URL || "http://127.0.0.1:3133").replace(/\/$/, "");

const MAC_ADDRESS_RE = /^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/;

function isValidMac(mac) {
  return typeof mac === "string" && MAC_ADDRESS_RE.test(mac.trim());
}

function readRequestBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

async function forgetBluetoothDevice(macAddress) {
  const mac = macAddress.trim().toUpperCase();
  const commands = [
    ["bluetoothctl", ["disconnect", mac]],
    ["bluetoothctl", ["untrust", mac]],
    ["bluetoothctl", ["remove", mac]],
  ];

  for (const [program, args] of commands) {
    const proc = Bun.spawn([program, ...args], {
      stdout: "pipe",
      stderr: "pipe",
    });
    const exitCode = await proc.exited;
    if (program === "bluetoothctl" && args[0] === "remove" && exitCode !== 0) {
      return false;
    }
  }

  return true;
}

async function handleForgetDevice(req, res) {
  let payload;
  try {
    payload = JSON.parse(await readRequestBody(req));
  } catch {
    sendJson(res, 400, { error: "Invalid JSON body" });
    return;
  }

  const macAddress = payload?.macAddress;
  if (!isValidMac(macAddress)) {
    sendJson(res, 400, { error: "Invalid macAddress" });
    return;
  }

  try {
    const response = await fetch(`${agentApiUrl}/api/forget-device`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ macAddress: macAddress.trim().toUpperCase() }),
    });

    const text = await response.text();
    let body;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      body = { error: text || response.statusText };
    }

    if (!response.ok) {
      sendJson(res, response.status, body.error ? body : { error: text || response.statusText });
      return;
    }

    sendJson(res, 200, body.success === undefined ? { success: true } : body);
  } catch (error) {
    if (process.env.BLUETOOTH_FORGET_LOCAL === "1") {
      const ok = await forgetBluetoothDevice(macAddress);
      if (!ok) {
        sendJson(res, 500, { error: "Bluetooth remove failed" });
        return;
      }
      sendJson(res, 200, { success: true });
      return;
    }

    console.error("[frontend] forget-device proxy failed", error);
    sendJson(res, 502, {
      error: `Agent API unavailable at ${agentApiUrl}. Set AGENT_API_URL or BLUETOOTH_FORGET_LOCAL=1.`,
    });
  }
}

const configScript = [
  `window.CONVEX_URL = ${JSON.stringify(convexUrl)};`,
  `window.DEPLOYMENT_MODE = ${JSON.stringify(deploymentMode)};`,
  `window.ORGANIZATION_NAME = ${JSON.stringify(organizationName)};`,
].join("\n");

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".gif": "image/gif",
  ".txt": "text/plain; charset=utf-8",
};

function getMimeType(filePath) {
  const ext = path.extname(filePath);
  return MIME_TYPES[ext] || "application/octet-stream";
}

function toFilesystemPath(requestPath) {
  const normalized = path.normalize(requestPath);
  const resolved = path.resolve(FRONTEND_DIR, `.${normalized}`);
  if (!resolved.startsWith(FRONTEND_DIR)) {
    return null;
  }
  return resolved;
}

async function readFileMaybe(resolvedPath) {
  try {
    const stats = await fs.stat(resolvedPath);
    if (stats.isDirectory()) {
      const indexPath = path.join(resolvedPath, "index.html");
      return readFileMaybe(indexPath);
    }
    const data = await fs.readFile(resolvedPath);
    return { data, path: resolvedPath };
  } catch (error) {
    if (error.code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

const server = http.createServer(async (req, res) => {
  try {
    if (!req || !req.url) {
      res.writeHead(400, { "Content-Type": "text/plain" });
      res.end("Bad Request");
      return;
    }

    const method = req.method || "GET";
    const parsedUrl = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    const pathname = parsedUrl.pathname;

    if (method === "GET" && pathname === "/config.js") {
      res.writeHead(200, {
        "Content-Type": "application/javascript; charset=utf-8",
        "Cache-Control": "no-store",
      });
      res.end(configScript);
      return;
    }

    if (method === "POST" && pathname === "/api/forget-device") {
      await handleForgetDevice(req, res);
      return;
    }

    const targetPath = pathname === "/" ? "/index.html" : pathname;
    const resolvedPath = toFilesystemPath(targetPath);

    if (!resolvedPath) {
      res.writeHead(403, { "Content-Type": "text/plain" });
      res.end("Forbidden");
      return;
    }

    const fileResult = await readFileMaybe(resolvedPath);

    if (!fileResult) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("Not Found");
      return;
    }

    if (method === "HEAD") {
      res.writeHead(200, { "Content-Type": getMimeType(fileResult.path) });
      res.end();
      return;
    }

    res.writeHead(200, {
      "Content-Type": getMimeType(fileResult.path),
      "Cache-Control": pathname.startsWith("/assets") ? "public, max-age=86400" : "no-cache",
    });
    res.end(fileResult.data);
  } catch (error) {
    console.error("[frontend] server error", error);
    res.writeHead(500, { "Content-Type": "text/plain" });
    res.end("Internal Server Error");
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Frontend server listening on http://${HOST}:${PORT}`);
});
