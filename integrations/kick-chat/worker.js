// SprigCast Kick chat relay: reads a Kick channel's chat through Kick's official developer API and passes it
// to SprigCast on the streamer's PC.
//
// Kick's official API only sends chat to a web server, by webhook. A desktop app can't take webhooks, so
// this Worker takes them for it:
//
//   SprigCast  --wss /v1/chat?channel=<name>-->  Worker  --(one Channel object per Kick channel)
//   Kick       --POST /v1/kick/webhook (signed)-->  Worker  --> that channel's object --> every SprigCast reading it
//
// While someone reads a channel, its object holds a Kick event subscription for that channel (made with the
// app's own token, so nobody signs in). When the last reader has been gone for CLEANUP_MS, it deletes the
// subscription and forgets the channel. Objects sleep between messages (hibernation), so a quiet chat costs
// nothing. Webhooks are checked against Kick's public key before anything is passed on.
//
// It keeps no chat: messages go straight through. The only things stored are a channel's subscription ids
// while it's being read. No logs of tokens. The IP is read only for the rate limit.

export const KICK_API = "https://api.kick.com/public/v1";
export const KICK_TOKEN = "https://id.kick.com/oauth/token";
export const EVENTS = [ // what SprigCast shows: chat lines, plus bans and subs as notices
  { name: "chat.message.sent", version: 1 },
  { name: "moderation.banned", version: 1 },
  { name: "channel.subscription.new", version: 1 },
  { name: "channel.subscription.renewal", version: 1 },
  { name: "channel.subscription.gifts", version: 1 },
];
export const CLEANUP_MS = 10 * 60 * 1000; // keep the subscription this long after the last reader leaves
const SLUG = /^[A-Za-z0-9_-]{2,30}$/;
const MAX_WEBHOOK = 64 * 1024;
const MAX_AGE_MS = 10 * 60 * 1000; // a webhook older than this is refused (replays)

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

// The app's own token (client credentials), reused until shortly before it runs out ----------------------

let appToken = { value: "", until: 0 };

export function forgetToken() {
  appToken = { value: "", until: 0 };
}

export async function getAppToken(env) {
  if (appToken.value && Date.now() < appToken.until) return appToken.value;
  const reply = await fetch(KICK_TOKEN, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials", client_id: env.KICK_CLIENT_ID, client_secret: env.KICK_CLIENT_SECRET,
    }),
  });
  if (!reply.ok) throw new Error(`Kick token ${reply.status}`);
  const data = await reply.json();
  if (!data.access_token) throw new Error("Kick sent no token");
  const life = Number(data.expires_in) || 3600;
  appToken = { value: data.access_token, until: Date.now() + Math.max(60, life - 300) * 1000 };
  return appToken.value;
}

async function kick(env, path, init = {}) {
  const token = await getAppToken(env);
  const headers = { authorization: `Bearer ${token}`, accept: "application/json", ...(init.headers || {}) };
  const reply = await fetch(`${KICK_API}${path}`, { ...init, headers });
  if (reply.status === 401) forgetToken(); // next call fetches a fresh one
  return reply;
}

/** 'moss' -> { user_id, slug } of that Kick channel, or null if there's none. */
export async function findChannel(env, slug) {
  const reply = await kick(env, `/channels?slug=${encodeURIComponent(slug)}`);
  if (reply.status === 404) return null;
  if (!reply.ok) throw new Error(`Kick channel lookup ${reply.status}`);
  const channel = ((await reply.json()).data || [])[0];
  const id = channel && Number(channel.broadcaster_user_id);
  return id ? { user_id: id, slug: String(channel.slug || slug) } : null;
}

// Webhook signatures --------------------------------------------------------------------------------------

let kickKey = null; // Kick's public key, fetched once per Worker instance

function pemToDer(pem) {
  const body = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  return Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
}

export function setKickKey(key) {
  kickKey = key;
}

async function publicKey() {
  if (kickKey) return kickKey;
  const reply = await fetch(`${KICK_API}/public-key`, { headers: { accept: "application/json" } });
  if (!reply.ok) throw new Error(`Kick public key ${reply.status}`);
  const pem = (await reply.json())?.data?.public_key;
  if (!pem) throw new Error("Kick sent no public key");
  kickKey = await crypto.subtle.importKey("spki", pemToDer(pem), { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false, ["verify"]);
  return kickKey;
}

/** Kick signs "<message id>.<timestamp>.<body>" with its private key (RSA, SHA-256), base64 in a header. */
export async function signedByKick(headers, body) {
  const id = headers.get("kick-event-message-id");
  const at = headers.get("kick-event-message-timestamp");
  const signature = headers.get("kick-event-signature");
  if (!id || !at || !signature) return false;
  const when = Date.parse(at);
  if (Number.isFinite(when) && Math.abs(Date.now() - when) > MAX_AGE_MS) return false;
  let raw;
  try {
    raw = Uint8Array.from(atob(signature), (c) => c.charCodeAt(0));
  } catch {
    return false;
  }
  const signed = new TextEncoder().encode(`${id}.${at}.${body}`);
  return await crypto.subtle.verify("RSASSA-PKCS1-v1_5", await publicKey(), raw, signed);
}

// The Worker --------------------------------------------------------------------------------------------

/** Accept a WebSocket only to say why there's no chat, then close it (SprigCast shows the reason). */
function refuse(why, code = 4000) {
  const [client, server] = Object.values(new WebSocketPair());
  server.accept();
  server.send(JSON.stringify({ type: "error", why }));
  server.close(code, why.slice(0, 120));
  return new Response(null, { status: 101, webSocket: client });
}

async function openChat(request, env, url) {
  if (request.headers.get("upgrade")?.toLowerCase() !== "websocket") return json({ error: "websocket_only" }, 426);
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  if (env.LIMITER && !(await env.LIMITER.limit({ key: ip })).success) return refuse("too many tries, wait a minute", 4029);
  const slug = (url.searchParams.get("channel") || "").toLowerCase();
  if (!SLUG.test(slug)) return refuse("that isn't a Kick channel name", 4004);
  let channel;
  try {
    channel = await findChannel(env, slug);
  } catch (error) {
    return refuse("Kick didn't answer, trying again soon", 4500);
  }
  if (!channel) return refuse("no Kick channel by that name", 4004);
  const stub = env.CHANNELS.get(env.CHANNELS.idFromName(String(channel.user_id)));
  const inner = new Request(`https://channel/connect?user_id=${channel.user_id}&slug=${encodeURIComponent(channel.slug)}`,
    { headers: request.headers });
  return stub.fetch(inner);
}

async function webhook(request, env) {
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const body = await request.text();
  if (body.length > MAX_WEBHOOK) return json({ error: "too_big" }, 413);
  let good = false;
  try {
    good = await signedByKick(request.headers, body);
  } catch {
    return json({ error: "no_key" }, 503); // Kick will try again
  }
  if (!good) return json({ error: "bad_signature" }, 401);
  let data;
  try {
    data = JSON.parse(body);
  } catch {
    return json({ error: "bad_json" }, 400);
  }
  const id = Number(data?.broadcaster?.user_id);
  if (!id) return json({ ok: true }); // nothing to pass on
  const stub = env.CHANNELS.get(env.CHANNELS.idFromName(String(id)));
  await stub.fetch(new Request("https://channel/event", {
    method: "POST",
    headers: {
      "kick-event-type": request.headers.get("kick-event-type") || "",
      "kick-event-message-id": request.headers.get("kick-event-message-id") || "",
    },
    body,
  }));
  return json({ ok: true });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/v1/health") return json({ ok: true });
    if (url.pathname === "/v1/chat") return openChat(request, env, url);
    if (url.pathname === "/v1/kick/webhook") return webhook(request, env);
    return json({ error: "not_found" }, 404);
  },
};

// One Kick channel ----------------------------------------------------------------------------------------

export class Channel {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.recent = []; // webhook ids just passed on: Kick may send one twice
    if (typeof WebSocketRequestResponsePair !== "undefined") {
      // SprigCast's keep-alive "ping" is answered without waking this object
      ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
    }
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/connect") return this.connect(url);
    if (url.pathname === "/event") return this.event(request);
    return json({ error: "not_found" }, 404);
  }

  async connect(url) {
    const userId = Number(url.searchParams.get("user_id"));
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server);
    await this.ctx.storage.deleteAlarm();
    try {
      await this.subscribe(userId);
    } catch (error) {
      server.send(JSON.stringify({ type: "error", why: "Kick didn't let SprigCast read this chat right now" }));
      server.close(4500, "subscribe failed");
      return new Response(null, { status: 101, webSocket: client });
    }
    server.send(JSON.stringify({ type: "ready", user_id: userId, slug: url.searchParams.get("slug") || "" }));
    return new Response(null, { status: 101, webSocket: client });
  }

  async subscribe(userId) {
    const held = await this.ctx.storage.get("subscriptions");
    if (held && held.length) return held;
    const reply = await kick(this.env, "/events/subscriptions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ broadcaster_user_id: userId, events: EVENTS, method: "webhook" }),
    });
    if (!reply.ok) throw new Error(`Kick subscribe ${reply.status}`);
    const ids = ((await reply.json()).data || []).map((row) => row.subscription_id).filter(Boolean);
    if (!ids.length) throw new Error("Kick made no subscription");
    await this.ctx.storage.put("subscriptions", ids);
    return ids;
  }

  async event(request) {
    const id = request.headers.get("kick-event-message-id") || "";
    if (id && this.recent.includes(id)) return json({ ok: true });
    if (id) this.recent = [...this.recent, id].slice(-200);
    const sockets = this.ctx.getWebSockets();
    if (!sockets.length) {
      await this.later(); // nobody is reading: tidy up soon
      return json({ ok: true });
    }
    let data;
    try {
      data = JSON.parse(await request.text());
    } catch {
      return json({ ok: true });
    }
    const frame = JSON.stringify({ type: "event", event: request.headers.get("kick-event-type") || "", data });
    for (const socket of sockets) {
      try {
        socket.send(frame);
      } catch {
        // a reader that just went away
      }
    }
    return json({ ok: true });
  }

  async later() {
    if (!(await this.ctx.storage.getAlarm())) await this.ctx.storage.setAlarm(Date.now() + CLEANUP_MS);
  }

  async webSocketMessage() {
    // Readers only listen (their "ping" is answered by the auto-response above).
  }

  async webSocketClose(socket) {
    try {
      socket.close(1000, "bye");
    } catch {
      // already closed
    }
    if (this.ctx.getWebSockets().filter((s) => s !== socket).length === 0) await this.later();
  }

  async webSocketError(socket) {
    await this.webSocketClose(socket);
  }

  async alarm() {
    if (this.ctx.getWebSockets().length) return; // someone came back
    const ids = (await this.ctx.storage.get("subscriptions")) || [];
    if (ids.length) {
      const query = ids.map((id) => `id=${encodeURIComponent(id)}`).join("&");
      try {
        await kick(this.env, `/events/subscriptions?${query}`, { method: "DELETE" });
      } catch {
        await this.ctx.storage.setAlarm(Date.now() + CLEANUP_MS); // try again later
        return;
      }
    }
    await this.ctx.storage.deleteAll();
  }
}
