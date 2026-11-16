// SprigCast report inbox: a Cloudflare Worker that takes the crash and problem reports SprigCast sends
// (Help → Report a problem, and the card after a crash) and keeps them in an R2 bucket for 30 days.
//
//   POST /v1/report   body: the report .zip (application/zip, at most 8 MB)
//                     headers: X-SprigCast-Version, X-SprigCast-Kind (crash | problem), X-SprigCast-Cause
//                     reply 201 {"id": "SC-7F3K"}: the number the tester can quote
//   GET  /v1/health   {"ok": true}
//
// The zip is scrubbed on the tester's PC before it is sent (no keys, passwords, names or IPs inside), and
// this Worker stores nothing else: not the IP address, not cookies. It only reads the IP to rate-limit.
// Optional: a Discord webhook (secret DISCORD_WEBHOOK) gets one line per new report.

export const MAX_BYTES = 8 * 1024 * 1024;
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
  const version = clean(request.headers.get("x-sprigcast-version"), 20) || "?";
  const kind = clean(request.headers.get("x-sprigcast-kind"), 10) === "crash" ? "crash" : "problem";
  const cause = clean(request.headers.get("x-sprigcast-cause"), 160);
  const id = reportId();
  const day = new Date().toISOString().slice(0, 10);
  await env.REPORTS.put(`${day}/${id}.zip`, body, {
    httpMetadata: { contentType: "application/zip" },
    customMetadata: { version, kind, cause },
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

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (pathname === "/v1/report") {
      if (request.method !== "POST") return reply({ error: "use POST" }, 405);
      return upload(request, env, ctx);
    }
    if (pathname === "/v1/health") return reply({ ok: true });
    return reply({ error: "not found" }, 404);
  },
};
