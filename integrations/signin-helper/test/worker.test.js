import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../worker.js";

const env = (over = {}) => ({
  YOUTUBE_CLIENT_ID: "yt-id", YOUTUBE_CLIENT_SECRET: "yt-secret",
  KICK_CLIENT_ID: "k-id", KICK_CLIENT_SECRET: "k-secret",
  LIMITER: { limit: async () => ({ success: true }) }, ...over,
});

function post(fields) {
  return new Request("https://signin.example/v1/token", { method: "POST", body: new URLSearchParams(fields) });
}

function fakeProvider(reply, status = 200) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, body: Object.fromEntries(new URLSearchParams(init.body.toString())) });
    return new Response(JSON.stringify(reply), { status });
  };
  return calls;
}

const good = { service: "youtube", code: "c", code_verifier: "v", redirect_uri: "http://127.0.0.1:50123/callback" };

test("swaps a code for a token, adding the secret, and hands back only the token", async () => {
  const calls = fakeProvider({ access_token: "tok", refresh_token: "keep-me-out", expires_in: 3599 });
  const response = await worker.fetch(post(good), env());
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.access_token, "tok");
  assert.equal(body.refresh_token, undefined);
  assert.equal(calls[0].url, "https://oauth2.googleapis.com/token");
  assert.deepEqual(calls[0].body, { grant_type: "authorization_code", client_id: "yt-id", client_secret: "yt-secret",
                                    code: "c", code_verifier: "v", redirect_uri: good.redirect_uri });
});

test("kick goes to Kick with its own secret, only on its registered redirect", async () => {
  const calls = fakeProvider({ access_token: "ktok" });
  const ok = await worker.fetch(post({ ...good, service: "kick", redirect_uri: "http://localhost:51587/callback" }), env());
  assert.equal(ok.status, 200);
  assert.equal(calls[0].url, "https://id.kick.com/oauth/token");
  assert.equal(calls[0].body.client_secret, "k-secret");
  const elsewhere = await worker.fetch(post({ ...good, service: "kick", redirect_uri: "https://evil.example/cb" }), env());
  assert.equal(elsewhere.status, 400);
  assert.equal(calls.length, 1);
});

test("refuses anything that isn't SprigCast's own sign-in", async () => {
  fakeProvider({ access_token: "x" });
  for (const fields of [{ ...good, service: "twitch" }, { ...good, redirect_uri: "https://evil.example/callback" },
                        { ...good, redirect_uri: "http://127.0.0.1.evil.example:80/callback" },
                        { ...good, code: "" }, { ...good, code: "x".repeat(5000) }]) {
    assert.equal((await worker.fetch(post(fields), env())).status, 400, JSON.stringify(fields).slice(0, 80));
  }
  assert.equal((await worker.fetch(new Request("https://signin.example/v1/token"), env())).status, 405);
  assert.equal((await worker.fetch(new Request("https://signin.example/nope"), env())).status, 404);
  assert.equal((await worker.fetch(new Request("https://signin.example/v1/health"), env())).status, 200);
});

test("no secret set yet, too many tries, and provider errors are passed on plainly", async () => {
  fakeProvider({ error: "invalid_grant", error_description: "Bad code" }, 400);
  assert.equal((await worker.fetch(post(good), env({ YOUTUBE_CLIENT_SECRET: "" }))).status, 503);
  const slow = env({ LIMITER: { limit: async () => ({ success: false }) } });
  assert.equal((await worker.fetch(post(good), slow)).status, 429);
  const failed = await worker.fetch(post(good), env());
  assert.equal(failed.status, 400);
  assert.deepEqual(await failed.json(), { error: "invalid_grant", error_description: "Bad code" });
});

test("keep=1 hands back the refresh token so Stream details stays signed in", async () => {
  fakeProvider({ access_token: "tok", refresh_token: "rt", expires_in: 3599 });
  const body = await (await worker.fetch(post({ ...good, keep: "1" }), env())).json();
  assert.equal(body.refresh_token, "rt");
});

test("swaps a refresh token for a new access token, adding the secret, with no redirect needed", async () => {
  const calls = fakeProvider({ access_token: "new", refresh_token: "rt2", expires_in: 3599 });
  const response = await worker.fetch(post({ service: "kick", grant_type: "refresh_token", refresh_token: "rt" }), env());
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.access_token, "new");
  assert.equal(body.refresh_token, "rt2");
  assert.equal(calls[0].url, "https://id.kick.com/oauth/token");
  assert.deepEqual(calls[0].body, { grant_type: "refresh_token", client_id: "k-id", client_secret: "k-secret",
                                    refresh_token: "rt" });
  for (const bad of ["", "x".repeat(5000)]) {
    const refused = await worker.fetch(post({ service: "youtube", grant_type: "refresh_token", refresh_token: bad }), env());
    assert.equal(refused.status, 400);
  }
  assert.equal(calls.length, 1);
});

test("serves the home page, privacy policy and terms that Google's verification links to", async () => {
  for (const [path, words] of [["/", "Signing in with YouTube"], ["/privacy", "Limited Use"], ["/terms", "GNU"]]) {
    const response = await worker.fetch(new Request(`https://signin.example${path}`), env());
    assert.equal(response.status, 200, path);
    assert.match(response.headers.get("content-type"), /^text\/html/);
    const html = await response.text();
    assert.ok(html.includes(words), path);
    assert.ok(html.includes('href="/privacy"'), `${path} links the privacy policy`);
  }
  const privacy = await (await worker.fetch(new Request("https://signin.example/privacy"), env())).text();
  assert.ok(privacy.includes("https://www.googleapis.com/auth/youtube"));
  assert.ok(privacy.includes("https://www.youtube.com/t/terms"));
  assert.ok(privacy.includes("https://policies.google.com/privacy"));
});

test("pages answer HEAD without a body and refuse POST", async () => {
  const head = await worker.fetch(new Request("https://signin.example/privacy", { method: "HEAD" }), env());
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  const posted = await worker.fetch(new Request("https://signin.example/", { method: "POST", body: "x" }), env());
  assert.equal(posted.status, 405);
});

test("anything else is still not found", async () => {
  const response = await worker.fetch(new Request("https://signin.example/nope"), env());
  assert.equal(response.status, 404);
});
