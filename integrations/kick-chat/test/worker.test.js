import assert from "node:assert/strict";
import { test } from "node:test";

import worker, { Channel, CLEANUP_MS, EVENTS, forgetToken, setKickKey, signedByKick } from "../worker.js";

// Workers' globals, small enough for these tests ----------------------------------------------------------

class FakeSocket {
  constructor() {
    this.sent = [];
    this.closed = null;
  }
  accept() {}
  send(text) {
    this.sent.push(JSON.parse(text));
  }
  close(code, why) {
    this.closed = { code, why };
  }
}

globalThis.WebSocketPair = class {
  constructor() {
    this[0] = new FakeSocket();
    this[1] = new FakeSocket();
  }
};

// Node's Response refuses status 101; Workers allow it with a webSocket.
const RealResponse = globalThis.Response;
globalThis.Response = class extends RealResponse {
  constructor(body, init = {}) {
    super(body, init.status === 101 ? { ...init, status: 200 } : init);
    if (init.status === 101) {
      Object.defineProperty(this, "status", { value: 101 });
      this.webSocket = init.webSocket;
    }
  }
};

function fakeCtx() {
  const store = new Map();
  let alarm = null;
  const sockets = [];
  return {
    sockets,
    store,
    get alarmAt() {
      return alarm;
    },
    acceptWebSocket: (socket) => sockets.push(socket),
    getWebSockets: () => sockets.filter((s) => !s.closed),
    setWebSocketAutoResponse() {},
    storage: {
      get: async (key) => store.get(key),
      put: async (key, value) => store.set(key, value),
      deleteAll: async () => store.clear(),
      getAlarm: async () => alarm,
      setAlarm: async (at) => {
        alarm = at;
      },
      deleteAlarm: async () => {
        alarm = null;
      },
    },
  };
}

function fakeKick(routes) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || "GET", body: init.body });
    for (const [match, reply] of routes) {
      if (String(url).includes(match)) {
        const [body, status] = typeof reply === "function" ? reply(init) : reply;
        return new RealResponse(JSON.stringify(body), { status: status || 200 });
      }
    }
    return new RealResponse("{}", { status: 404 });
  };
  return calls;
}

const env = (channels) => ({
  KICK_CLIENT_ID: "k-id", KICK_CLIENT_SECRET: "k-secret", CHANNELS: channels,
  LIMITER: { limit: async () => ({ success: true }) },
});

function oneChannel() {
  const ctx = fakeCtx();
  const objects = new Map();
  const channels = {
    idFromName: (name) => name,
    get: (id) => {
      if (!objects.has(id)) objects.set(id, new Channel(ctx, channels.env));
      return objects.get(id);
    },
  };
  const e = env(channels);
  channels.env = e;
  return { ctx, env: e, objects };
}

const upgrade = (path) => new Request(`https://kickchat.example${path}`, { headers: { upgrade: "websocket" } });

// Reading a channel -------------------------------------------------------------------------------------

test("a reader gets 'ready' and the channel is subscribed once, with the app's own token", async () => {
  forgetToken();
  const calls = fakeKick([
    ["oauth/token", [{ access_token: "app", expires_in: 3600 }]],
    ["/channels?slug=moss", [{ data: [{ broadcaster_user_id: 77, slug: "moss" }] }]],
    ["/events/subscriptions", [{ data: [{ subscription_id: "s1", name: "chat.message.sent" }, { subscription_id: "s2" }] }]],
  ]);
  const { ctx, env: e } = oneChannel();
  const first = await worker.fetch(upgrade("/v1/chat?channel=Moss"), e);
  assert.equal(first.status, 101);
  assert.deepEqual(ctx.sockets[0].sent, [{ type: "ready", user_id: 77, slug: "moss" }]);
  await worker.fetch(upgrade("/v1/chat?channel=moss"), e);
  const subscribes = calls.filter((c) => c.url.endsWith("/events/subscriptions") && c.method === "POST");
  assert.equal(subscribes.length, 1);
  assert.deepEqual(JSON.parse(subscribes[0].body), { broadcaster_user_id: 77, events: EVENTS, method: "webhook" });
  assert.equal(calls.filter((c) => c.url.includes("oauth/token")).length, 1);
  assert.deepEqual(ctx.store.get("subscriptions"), ["s1", "s2"]);
});

test("unknown names and bad names are refused with a reason, not a bare error", async () => {
  forgetToken();
  fakeKick([["oauth/token", [{ access_token: "app" }]], ["/channels?slug=", [{ data: [] }]]]);
  const { env: e } = oneChannel();
  const none = await worker.fetch(upgrade("/v1/chat?channel=nobody"), e);
  const nonsense = await worker.fetch(upgrade("/v1/chat?channel=a%20b"), e);
  for (const reply of [none, nonsense]) assert.ok(reply.status === 101 && reply.webSocket); // the reason goes down the socket
  const plain = await worker.fetch(new Request("https://kickchat.example/v1/chat?channel=moss"), e);
  assert.equal(plain.status, 426);
});

test("the last reader leaving tidies the Kick subscription away after a while", async () => {
  forgetToken();
  const calls = fakeKick([
    ["oauth/token", [{ access_token: "app" }]],
    ["/channels?slug=fern", [{ data: [{ broadcaster_user_id: 5, slug: "fern" }] }]],
    ["/events/subscriptions", [{ data: [{ subscription_id: "a" }, { subscription_id: "b" }] }]],
  ]);
  const { ctx, env: e, objects } = oneChannel();
  await worker.fetch(upgrade("/v1/chat?channel=fern"), e);
  const channel = objects.get("5");
  const socket = ctx.sockets[0];
  await channel.webSocketClose(socket);
  assert.ok(ctx.alarmAt >= Date.now() + CLEANUP_MS - 1000);
  await channel.alarm();
  const removed = calls.find((c) => c.method === "DELETE");
  assert.ok(removed && removed.url.endsWith("/events/subscriptions?id=a&id=b"));
  assert.equal(ctx.store.size, 0);
});

// Webhooks ----------------------------------------------------------------------------------------------

async function keys() {
  return crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]),
    hash: "SHA-256" }, true, ["sign", "verify"]);
}

async function signed(pair, body, { id = "m1", at = new Date().toISOString(), type = "chat.message.sent" } = {}) {
  const raw = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(`${id}.${at}.${body}`));
  const signature = btoa(String.fromCharCode(...new Uint8Array(raw)));
  return new Request("https://kickchat.example/v1/kick/webhook", {
    method: "POST", body,
    headers: { "kick-event-message-id": id, "kick-event-message-timestamp": at, "kick-event-signature": signature,
               "kick-event-type": type, "kick-event-version": "1" },
  });
}

test("only webhooks Kick signed are passed on, once each, to everyone reading that channel", async () => {
  forgetToken();
  fakeKick([
    ["oauth/token", [{ access_token: "app" }]],
    ["/channels?slug=moss", [{ data: [{ broadcaster_user_id: 77, slug: "moss" }] }]],
    ["/events/subscriptions", [{ data: [{ subscription_id: "s1" }] }]],
  ]);
  const pair = await keys();
  setKickKey(pair.publicKey);
  const { ctx, env: e } = oneChannel();
  await worker.fetch(upgrade("/v1/chat?channel=moss"), e);
  await worker.fetch(upgrade("/v1/chat?channel=moss"), e);
  const body = JSON.stringify({ message_id: "x", broadcaster: { user_id: 77 }, sender: { username: "fern" }, content: "hi" });
  assert.equal((await worker.fetch(await signed(pair, body), e)).status, 200);
  assert.equal((await worker.fetch(await signed(pair, body), e)).status, 200); // Kick's retry: not shown twice
  for (const socket of ctx.sockets) {
    const events = socket.sent.filter((f) => f.type === "event");
    assert.equal(events.length, 1);
    assert.equal(events[0].event, "chat.message.sent");
    assert.equal(events[0].data.content, "hi");
  }
  const forged = await signed(pair, body, { id: "m2" });
  const tampered = new Request(forged, { body: body.replace("hi", "buy followers") });
  assert.equal((await worker.fetch(tampered, e)).status, 401);
  const old = await signed(pair, body, { id: "m3", at: new Date(Date.now() - 3600_000).toISOString() });
  assert.equal(await signedByKick(old.headers, body), false);
});
