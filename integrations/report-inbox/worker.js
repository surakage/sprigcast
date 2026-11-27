// SprigCast report inbox: a Cloudflare Worker that takes the crash and problem reports SprigCast sends
// (Help → Report a problem, and the card after a crash) and keeps them in an R2 bucket for 30 days.
//
//   POST /v1/report   body: the report .zip (application/zip, at most 8 MB)
//                     headers: X-SprigCast-Version, X-SprigCast-Kind (crash | problem | stream), X-SprigCast-Cause
//                     ("stream": the summary a tester chose to send after each stream, app/share_reports.py)
//                     reply 201 {"id": "SC-7F3K"}: the number the tester can quote
//                     X-SprigCast-Install: a random id made on that PC ("import": what an import couldn't carry
//                     over, sent when "Share logs automatically" is on; the install id lets a tester ask for
//                     their uploads to be deleted)
//   GET  /v1/health   {"ok": true}
//   GET  /v1/reports?since=YYYY-MM-DD   [{id, date, version, kind, cause, install, size}], newest first
//   GET  /v1/reports/SC-XXXXXX          the zip
//                     Both need "Authorization: Bearer <READ_TOKEN>" (a Worker secret); without it they answer
//                     404, as if they weren't there. For the team (and Claude threads) to pull reports.
//
// The zip is scrubbed on the tester's PC before it is sent (no keys, passwords, names or IPs inside), and
// this Worker stores nothing else: not the IP address, not cookies. It only reads the IP to rate-limit.
// Optional: a Discord webhook (secret DISCORD_WEBHOOK) gets one line per new report.

export const MAX_BYTES = 8 * 1024 * 1024;
const KINDS = ["crash", "problem", "stream", "import"];
const LIST_DAYS = 31; // reports live 30 days
const ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"; // no 0/O or 1/I: easy to read out loud

export function reportId(random = crypto.getRandomValues(new Uint8Array(6))) {
  let id = "";
  for (const byte of random) id += ALPHABET[byte % ALPHABET.length];
  return `SC-${id}`;
}

function reply(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

function clean(value, max) {
  return (value || "").replace(/[^\x20-\x7E]/g, "").slice(0, max);
}

async function upload(request, env, ctx) {
  if (env.LIMITER) {
    const ip = request.headers.get("cf-connecting-ip") || "?";
    const { success } = await env.LIMITER.limit({ key: ip });
    if (!success) return reply({ error: "too many reports, try again in a minute" }, 429);
  }
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BYTES) return reply({ error: "report too big" }, 413);
  const body = await request.arrayBuffer();
  if (body.byteLength === 0) return reply({ error: "empty report" }, 400);
  if (body.byteLength > MAX_BYTES) return reply({ error: "report too big" }, 413);
  const head = new Uint8Array(body, 0, Math.min(4, body.byteLength));
  if (!(head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04)) {
    return reply({ error: "not a zip" }, 415);
  }
  // Room for a test build's label: "0.2.0 test build 44932007527 (eafa358)".
  const version = clean(request.headers.get("x-sprigcast-version"), 60) || "?";
  const asked = clean(request.headers.get("x-sprigcast-kind"), 10);
  const kind = KINDS.includes(asked) ? asked : "problem";
  const cause = clean(request.headers.get("x-sprigcast-cause"), 160);
  const install = clean(request.headers.get("x-sprigcast-install"), 40).replace(/[^0-9a-f-]/gi, "");
  const id = reportId();
  const day = new Date().toISOString().slice(0, 10);
  await env.REPORTS.put(`${day}/${id}.zip`, body, {
    httpMetadata: { contentType: "application/zip" },
    customMetadata: install ? { version, kind, cause, install } : { version, kind, cause },
  });
  if (env.DISCORD_WEBHOOK) {
    const line = `New SprigCast ${kind} report **${id}** (${version})${cause ? `: ${cause}` : ""}`;
    ctx.waitUntil(
      fetch(env.DISCORD_WEBHOOK, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ content: line, allowed_mentions: { parse: [] } }),
      }).catch(() => {}),
    );
  }
  return reply({ id }, 201);
}

// Reading ------------------------------------------------------------------------------------------------

function same(a, b) {
  // constant time for equal lengths, so the token can't be guessed a letter at a time
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] || 0) ^ (y[i] || 0);
  return diff === 0;
}

export function allowed(request, env) {
  const token = env.READ_TOKEN || "";
  const given = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  return token.length >= 16 && same(given, token);
}

function days(since, today = new Date()) {
  const start = /^\d{4}-\d{2}-\d{2}$/.test(since || "") ? new Date(`${since}T00:00:00Z`) : today;
  const out = [];
  for (let d = new Date(today.toISOString().slice(0, 10) + "T00:00:00Z"); d >= start && out.length < LIST_DAYS;
       d.setUTCDate(d.getUTCDate() - 1)) {
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

async function list(url, env) {
  const rows = [];
  for (const day of days(url.searchParams.get("since"))) {
    let cursor;
    do {
      const page = await env.REPORTS.list({ prefix: `${day}/`, cursor, include: ["customMetadata"] });
      for (const object of page.objects) {
        const meta = object.customMetadata || {};
        rows.push({ id: object.key.slice(11, -4), date: day, version: meta.version || "?", kind: meta.kind || "?",
                    cause: meta.cause || "", install: meta.install || "", size: object.size });
      }
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor);
  }
  return reply(rows);
}

async function fetchOne(id, env) {
  if (!/^SC-[2-9A-HJ-NP-Z]{6}$/.test(id)) return reply({ error: "not found" }, 404);
  for (const day of days(new Date(Date.now() - LIST_DAYS * 864e5).toISOString().slice(0, 10))) {
    const object = await env.REPORTS.get(`${day}/${id}.zip`);
    if (object) {
      return new Response(object.body, {
        headers: { "content-type": "application/zip", "cache-control": "no-store",
                   "content-disposition": `attachment; filename="${id}.zip"` },
      });
    }
  }
  return reply({ error: "not found" }, 404);
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (pathname === "/v1/report") {
      if (request.method !== "POST") return reply({ error: "use POST" }, 405);
      return upload(request, env, ctx);
    }
    if (pathname === "/v1/health") return reply({ ok: true });
    if (pathname === "/v1/reports" || pathname.startsWith("/v1/reports/")) {
      if (request.method !== "GET" || !allowed(request, env)) return reply({ error: "not found" }, 404);
      const url = new URL(request.url);
      return pathname === "/v1/reports" ? list(url, env) : fetchOne(pathname.slice("/v1/reports/".length), env);
    }
    return reply({ error: "not found" }, 404);
  },
};
