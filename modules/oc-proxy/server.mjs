// server.mjs — OpenAI-compat bridge for the local `opencode serve` instance.
//
// Exposes:
//   GET  /v1/models            → list of FREE models from the opencode gateway
//   POST /v1/chat/completions  → proxied to opencode session/message API
//                                (streaming SSE supported, tool-calls NOT supported)
//
// Auth: Bearer $OC_PROXY_KEY (default sk-justar-local-oc) — must match the
// apiKey registered in OmniRoute for this provider node.
//
// Upstream: $OPENCODE_SERVER_URL (auto-discovered by scanning local listeners
// owned by an "opencode" process, mirroring ~/.local/bin/ask-muse discovery).
// If no server is listening, `opencode serve` is spawned on $OC_UPSTREAM_PORT
// (default 8124) with OPENCODE_SERVER_PASSWORD = $OC_PROXY_KEY so auth matches.

import http from "node:http";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";

const PORT = parseInt(process.env.OC_PROXY_PORT || "8300", 10);
const HOST = process.env.OC_PROXY_HOST || "127.0.0.1";
const API_KEY = process.env.OC_PROXY_KEY || "sk-justar-local-oc";
const UPSTREAM_PORT = parseInt(process.env.OC_UPSTREAM_PORT || "8124", 10);
const LOG_DIR = process.env.OC_LOG_DIR || new URL("../logs", import.meta.url).pathname;

// FREE models on the opencode.ai gateway (verified 2026-09-10 via
// `opencode serve` → /provider: provider "opencode", api https://opencode.ai/zen/v1).
// muse-spark-1.3 is the ask-muse default; the rest are the other free-tier entries.
const FREE_MODELS = [
  { id: "muse-spark-1.3-contributor-free", name: "Muse Spark 1.3 Contributor (free)" },
  { id: "muse-spark-1.2-contributor-free", name: "Muse Spark 1.2 Contributor (free)" },
  { id: "nemotron-3-ultra-free", name: "Nemotron 3 Ultra (free)" },
  { id: "nemotron-3.5-lightning-free", name: "Nemotron 3.5 Lightning (free)" },
  { id: "mimo-v2.5-free", name: "MiMo v2.5 (free)" },
  { id: "ling-3.0-flash-fin-free", name: "Ling 3.0 Flash Fin (free)" },
  { id: "big-pickle", name: "Big Pickle" },
];
const DEFAULT_MODEL = process.env.OC_DEFAULT_MODEL || FREE_MODELS[0].id;

// ── upstream discovery (same strategy as ask-muse) ──
function discoverUpstreamUrl() {
  if (process.env.OPENCODE_SERVER_URL) return process.env.OPENCODE_SERVER_URL;
  try {
    const out = spawnSync("ss", ["-ltnp"], { encoding: "utf-8", timeout: 3000 });
    if (out.error || out.status !== 0) return null;
    for (const line of (out.stdout || "").split("\n")) {
      if (!line.includes("127.0.0.1:")) continue;
      if (!/opencode/i.test(line)) continue;
      const m = line.match(/127\.0\.0\.1:(\d+)/);
      if (m) return `http://127.0.0.1:${m[1]}`;
    }
  } catch {}
  return null;
}

async function probeUpstream(url, authHeader) {
  try {
    const res = await fetch(`${url}/doc`, { headers: { Authorization: authHeader } });
    return res.status === 200;
  } catch {
    return false;
  }
}

let upstreamUrl = null;
let upstreamAuth = null;

async function ensureUpstream() {
  if (upstreamUrl && upstreamAuth) return;
  const authHeader =
    "Basic " + Buffer.from(`opencode:${API_KEY}`).toString("base64");
  // 1. Prefer an existing opencode listener (desktop sidecar or previous spawn)
  const found = discoverUpstreamUrl();
  if (found && (await probeUpstream(found, authHeader))) {
    upstreamUrl = found;
    upstreamAuth = authHeader;
    return;
  }
  // 2. Also try our managed port (maybe a previous server run left it up)
  const managed = `http://127.0.0.1:${UPSTREAM_PORT}`;
  if (await probeUpstream(managed, authHeader)) {
    upstreamUrl = managed;
    upstreamAuth = authHeader;
    return;
  }
  // 3. Spawn `opencode serve` ourselves, secured with OC_PROXY_KEY
  const child = spawn(
    "opencode",
    ["serve", "--port", String(UPSTREAM_PORT), "--hostname", "127.0.0.1"],
    {
      env: {
        ...process.env,
        OPENCODE_SERVER_USERNAME: "opencode",
        OPENCODE_SERVER_PASSWORD: API_KEY,
      },
      stdio: ["ignore", "ignore", "ignore"],
      detached: true,
    },
  );
  child.unref();
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (await probeUpstream(managed, authHeader)) {
      upstreamUrl = managed;
      upstreamAuth = authHeader;
      return;
    }
  }
  throw new Error(`opencode serve did not become ready on ${managed}`);
}

function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(" ")}\n`;
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(`${LOG_DIR}/oc-proxy.log`, line);
  } catch {}
}

function authOk(req) {
  const h = req.headers["authorization"] || "";
  return h === `Bearer ${API_KEY}`;
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

// Extract a plain text prompt from an OpenAI chat messages array.
function promptFromMessages(messages = []) {
  const parts = [];
  for (const m of messages) {
    if (typeof m.content === "string") {
      parts.push(m.content);
    } else if (Array.isArray(m.content)) {
      for (const p of m.content) {
        if (p && p.type === "text") parts.push(p.text || "");
      }
    }
  }
  return parts.join("\n").trim();
}

// Split "opencode/muse-spark-1.3-contributor-free" → gateway providerID/modelID.
// The bridge model ids deliberately drop the "opencode/" prefix.
function splitModel(model) {
  const m = model || DEFAULT_MODEL;
  const slash = m.indexOf("/");
  if (slash > 0) return { providerID: m.slice(0, slash), modelID: m.slice(slash + 1) };
  return { providerID: "opencode", modelID: m };
}

async function callUpstreamChat(model, prompt) {
  await ensureUpstream();
  const { providerID, modelID } = splitModel(model);
  const sres = await fetch(`${upstreamUrl}/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: upstreamAuth },
    body: JSON.stringify({
      title: "oc-proxy",
      model: { providerID, id: modelID },
    }),
  });
  if (!sres.ok) throw new Error(`upstream session ${sres.status}`);
  const session = await sres.json();
  const mres = await fetch(`${upstreamUrl}/session/${session.id}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: upstreamAuth },
    body: JSON.stringify({ parts: [{ type: "text", text: prompt }] }),
  });
  if (!mres.ok) throw new Error(`upstream message ${mres.status}`);
  const msg = await mres.json();
  const text = (msg.parts || [])
    .filter((p) => p.type === "text")
    .map((p) => p.text || "")
    .join("");
  // Best-effort cleanup: free-tier gateway sessions pile up otherwise.
  fetch(`${upstreamUrl}/session/${session.id}`, {
    method: "DELETE",
    headers: { Authorization: upstreamAuth },
  }).catch(() => {});
  return text;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const started = Date.now();

  if (url.pathname === "/healthz") {
    return sendJson(res, 200, { ok: true });
  }

  if (!authOk(req)) {
    return sendJson(res, 401, {
      error: { message: "Invalid API key", code: "invalid_api_key" },
    });
  }

  // ── GET /v1/models ──
  if (req.method === "GET" && (url.pathname === "/v1/models" || url.pathname === "/models")) {
    return sendJson(res, 200, {
      object: "list",
      data: FREE_MODELS.map((m) => ({
        id: m.id,
        object: "model",
        created: 1789032735,
        owned_by: "opencode",
        info: { id: m.id, name: m.name, meta: { description: `${m.name} — free via opencode.ai gateway` } },
      })),
    });
  }

  // ── POST /v1/chat/completions ──
  const isChat =
    req.method === "POST" &&
    (url.pathname === "/v1/chat/completions" || url.pathname === "/chat/completions");
  if (isChat) {
    let body = "";
    req.setEncoding("utf-8");
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      let payload;
      try {
        payload = JSON.parse(body || "{}");
      } catch {
        return sendJson(res, 400, { error: { message: "Invalid JSON", code: "invalid_json" } });
      }
      const model = payload.model || DEFAULT_MODEL;
      const prompt = promptFromMessages(payload.messages);
      if (!prompt) {
        return sendJson(res, 400, {
          error: { message: "messages must contain text content", code: "invalid_messages" },
        });
      }
      const id = `chatcmpl-oc-${Date.now().toString(36)}`;
      const stream = payload.stream === true;
      log(`chat model=${model} stream=${stream} promptLen=${prompt.length}`);
      try {
        const text = await callUpstreamChat(model, prompt);
        log(`chat done model=${model} ms=${Date.now() - started} outLen=${text.length}`);
        if (stream) {
          // Minimal OpenAI SSE: one content chunk + [DONE]
          res.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            Connection: "keep-alive",
          });
          const chunk = (delta) =>
            `data: ${JSON.stringify({
              id,
              object: "chat.completion.chunk",
              created: Math.floor(Date.now() / 1000),
              model,
              choices: [{ index: 0, delta, finish_reason: null }],
            })}\n\n`;
          res.write(chunk({ role: "assistant", content: text }));
          res.write(
            `data: ${JSON.stringify({
              id,
              object: "chat.completion.chunk",
              created: Math.floor(Date.now() / 1000),
              model,
              choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
            })}\n\n`,
          );
          res.end("data: [DONE]\n\n");
        } else {
          sendJson(res, 200, {
            id,
            object: "chat.completion",
            created: Math.floor(Date.now() / 1000),
            model,
            choices: [
              {
                index: 0,
                message: { role: "assistant", content: text },
                finish_reason: "stop",
              },
            ],
            usage: {
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
            },
          });
        }
      } catch (e) {
        log(`chat ERROR model=${model} ms=${Date.now() - started} ${e.message}`);
        sendJson(res, 502, {
          error: { message: `opencode upstream error: ${e.message}`, code: "upstream_error" },
        });
      }
    });
    return;
  }

  sendJson(res, 404, { error: { message: `Not found: ${req.method} ${url.pathname}`, code: "not_found" } });
});

server.listen(PORT, HOST, () => {
  log(`oc-proxy listening on http://${HOST}:${PORT}/v1 (key ${API_KEY.slice(0, 8)}…)`);
  console.log(`oc-proxy listening on http://${HOST}:${PORT}/v1`);
});

process.on("SIGTERM", () => process.exit(0));
process.on("SIGINT", () => process.exit(0));
