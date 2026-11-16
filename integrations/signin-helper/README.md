# SprigCast sign-in helper

A small Cloudflare Worker at **https://signin.slothbloom.com** that finishes YouTube and Kick sign-ins for
SprigCast, so the client secrets live only here and never inside the app or this repo.

What it does: SprigCast signs you in through your browser with PKCE. The browser comes back to SprigCast
on your PC with a one-time code. SprigCast sends that code (plus its PKCE verifier) here, and the Worker
adds the secret, asks Google or Kick for a token, and hands back only the access token. SprigCast reads
your stream key with it and throws it away.

It refuses anything that isn't SprigCast's own sign-in: only `youtube` or `kick`, and only with the
redirect back to the PC (`http://127.0.0.1:<port>/callback` for YouTube, exactly
`http://localhost:51587/callback` for Kick). It stores nothing and logs no codes or tokens. It reads the IP
only to allow at most 10 sign-ins a minute from one address.

It's separate from the website and the report inbox, so deploying one never touches the others.
Cost: free (Workers' free plan).

## Deploy (once)

Needs the Cloudflare account that has slothbloom.com, and Node (already on the PC).

```
cd integrations\signin-helper
npx wrangler login
npx wrangler deploy
npx wrangler secret put YOUTUBE_CLIENT_SECRET
npx wrangler secret put KICK_CLIENT_SECRET
```

Each `secret put` asks for the value: paste it (the Google one starts with `GOCSPX-`, Kick's is the long
one from kick.com/settings/developer). Secrets are kept encrypted by Cloudflare and never show again.

Check it: https://signin.slothbloom.com/v1/health shows `{"ok":true}`.

## Changing it

`npm test` runs the tests (Node 20+). `npx wrangler deploy` again after a change. To swap a secret
(after rotating it in Google Cloud or Kick), run its `secret put` again. The Client IDs are public and sit in
`wrangler.toml` and in `src/sprigcast/output/service_signin.py`. SprigCast sends to `HELPER_URL` there
(`SPRIGCAST_SIGNIN_URL` overrides it for testing).
