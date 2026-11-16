// SprigCast sign-in helper: finishes a YouTube or Kick sign-in for the app, adding the client secret,
// so the secret never ships inside SprigCast. It does one thing: swap a sign-in code (plus the app's
// PKCE verifier) for a token, and only for SprigCast's own redirect back to the PC (127.0.0.1 / localhost).
// It keeps nothing: no logs of codes or tokens, no storage. The IP is read only for the rate limit.

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
    const { code, code_verifier: verifier, redirect_uri: redirect } = fields;
    if (!service) return json({ error: "invalid_request", error_description: "Unknown service." }, 400);
    for (const value of [code, verifier, redirect]) {
      if (typeof value !== "string" || !value || value.length > MAX_FIELD) {
        return json({ error: "invalid_request" }, 400);
      }
    }
    if (!service.redirect.test(redirect)) {
      return json({ error: "invalid_request", error_description: "Only SprigCast's own sign-in." }, 400);
    }
    const id = service.id(env);
    const secret = service.secret(env);
    if (!id || !secret) return json({ error: "not_set_up", error_description: "Sign-in isn't set up yet." }, 503);

    const reply = await fetch(service.token, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: id,
        client_secret: secret,
        code,
        code_verifier: verifier,
        redirect_uri: redirect,
      }),
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
    return json(out, reply.ok ? 200 : reply.status);
  },
};
