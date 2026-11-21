// SprigCast sign-in helper: finishes a YouTube, Kick or TikTok sign-in for the app, adding the client secret,
// so the secret never ships inside SprigCast. It swaps a sign-in code (plus the app's PKCE verifier) for a
// token, only for SprigCast's own redirect back to the PC (127.0.0.1 / localhost). With keep=1 (Stream
// details stays signed in) it also hands back the refresh token, and grant_type=refresh_token swaps one of
// those for a new access token. It keeps nothing: no logs of codes or tokens, no storage. The IP is read
// only for the rate limit. It also serves SprigCast's home page, privacy policy and terms (pages.js), which
// Google's sign-in verification needs on SprigCast's own domain.

import { servePage } from "./pages.js";

const SERVICES = {
  youtube: {
    token: "https://oauth2.googleapis.com/token",
    id: (env) => env.YOUTUBE_CLIENT_ID,
    secret: (env) => env.YOUTUBE_CLIENT_SECRET,
    // Google desktop sign-in: any port on the PC's own address
    redirect: /^http:\/\/127\.0\.0\.1:\d{2,5}\/callback$/,
  },
  kick: {
    token: "https://id.kick.com/oauth/token",
    id: (env) => env.KICK_CLIENT_ID,
    secret: (env) => env.KICK_CLIENT_SECRET,
    redirect: /^http:\/\/localhost:51587\/callback$/, // exactly what's registered with Kick
  },
  tiktok: {
    token: "https://open.tiktokapis.com/v2/oauth/token/",
    revoke: "https://open.tiktokapis.com/v2/oauth/revoke/", // needs the secret, so the app can't call it itself
    id: (env) => env.TIKTOK_CLIENT_KEY,
    secret: (env) => env.TIKTOK_CLIENT_SECRET,
    idField: "client_key", // TikTok's name for the client id
    // Login Kit for Desktop: http://127.0.0.1:*/callback is registered (any port)
    redirect: /^http:\/\/127\.0\.0\.1:\d{2,5}\/callback$/,
  },
};

const MAX_FIELD = 2048;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export async function readFields(request) {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) return await request.json();
  const form = await request.formData();
  return Object.fromEntries(form.entries());
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/v1/health") return json({ ok: true });
    const shown = servePage(request, url.pathname); // the home page, privacy policy and terms
    if (shown) return shown;
    if (url.pathname !== "/v1/token") return json({ error: "not_found" }, 404);
    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);

    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    if (env.LIMITER && !(await env.LIMITER.limit({ key: ip })).success) {
      return json({ error: "slow_down", error_description: "Too many sign-ins. Wait a minute." }, 429);
    }

    let fields;
    try {
      fields = await readFields(request);
    } catch {
      return json({ error: "invalid_request" }, 400);
    }
    const service = SERVICES[fields.service];
    if (!service) return json({ error: "invalid_request", error_description: "Unknown service." }, 400);
    const refresh = fields.grant_type === "refresh_token";
    const revoke = fields.grant_type === "revoke";
    if (revoke && !service.revoke) return json({ error: "invalid_request" }, 400);
    const { code, code_verifier: verifier, redirect_uri: redirect, refresh_token: refreshToken, token } = fields;
    for (const value of revoke ? [token] : refresh ? [refreshToken] : [code, verifier, redirect]) {
      if (typeof value !== "string" || !value || value.length > MAX_FIELD) {
        return json({ error: "invalid_request" }, 400);
      }
    }
    if (!refresh && !revoke && !service.redirect.test(redirect)) {
      return json({ error: "invalid_request", error_description: "Only SprigCast's own sign-in." }, 400);
    }
    const id = service.id(env);
    const secret = service.secret(env);
    if (!id || !secret) return json({ error: "not_set_up", error_description: "Sign-in isn't set up yet." }, 503);

    const idField = service.idField || "client_id";
    if (revoke) {
      const done = await fetch(service.revoke, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
        body: new URLSearchParams({ [idField]: id, client_secret: secret, token }),
      });
      return json({ ok: done.ok }, done.ok ? 200 : 502);
    }
    const ask = refresh
      ? { grant_type: "refresh_token", [idField]: id, client_secret: secret, refresh_token: refreshToken }
      : { grant_type: "authorization_code", [idField]: id, client_secret: secret, code, code_verifier: verifier,
          redirect_uri: redirect };
    const reply = await fetch(service.token, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
      body: new URLSearchParams(ask),
    });
    let body;
    try {
      body = await reply.json();
    } catch {
      body = { error: "bad_reply" };
    }
    // Hand back only what the app uses. TikTok can answer 200 with an error in the body.
    const ok = reply.ok && !!body.access_token;
    const out = ok
      ? { access_token: body.access_token, token_type: body.token_type, expires_in: body.expires_in, scope: body.scope }
      : { error: body.error || "sign_in_failed", error_description: body.error_description || "" };
    // Only a sign-in that asked to stay signed in (or a refresh, when the service hands out a new one) gets it.
    if (ok && body.refresh_token && (refresh || fields.keep === "1")) out.refresh_token = body.refresh_token;
    return json(out, ok ? 200 : reply.ok ? 400 : reply.status);
  },
};
