// SprigCast sign-in helper: finishes a YouTube or Kick sign-in for the app, adding the client secret,
// so the secret never ships inside SprigCast. It swaps a sign-in code (plus the app's PKCE verifier) for a
// token, only for SprigCast's own redirect back to the PC (127.0.0.1 / localhost). With keep=1 (Stream
// details stays signed in) it also hands back the refresh token, and grant_type=refresh_token swaps one of
// those for a new access token. It keeps nothing: no logs of codes or tokens, no storage. The IP is read
// only for the rate limit.

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
    const { code, code_verifier: verifier, redirect_uri: redirect, refresh_token: refreshToken } = fields;
    for (const value of refresh ? [refreshToken] : [code, verifier, redirect]) {
      if (typeof value !== "string" || !value || value.length > MAX_FIELD) {
        return json({ error: "invalid_request" }, 400);
      }
    }
    if (!refresh && !service.redirect.test(redirect)) {
      return json({ error: "invalid_request", error_description: "Only SprigCast's own sign-in." }, 400);
    }
    const id = service.id(env);
    const secret = service.secret(env);
    if (!id || !secret) return json({ error: "not_set_up", error_description: "Sign-in isn't set up yet." }, 503);

    const ask = refresh
      ? { grant_type: "refresh_token", client_id: id, client_secret: secret, refresh_token: refreshToken }
      : { grant_type: "authorization_code", client_id: id, client_secret: secret, code, code_verifier: verifier,
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
    // Hand back only what the app uses.
    const out = reply.ok
      ? { access_token: body.access_token, token_type: body.token_type, expires_in: body.expires_in, scope: body.scope }
      : { error: body.error || "sign_in_failed", error_description: body.error_description || "" };
    // Only a sign-in that asked to stay signed in (or a refresh, when the service hands out a new one) gets it.
    if (reply.ok && body.refresh_token && (refresh || fields.keep === "1")) out.refresh_token = body.refresh_token;
    return json(out, reply.ok ? 200 : reply.status);
  },
};
