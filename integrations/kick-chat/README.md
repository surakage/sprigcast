# SprigCast Kick chat relay

A small Cloudflare Worker at **https://kickchat.slothbloom.com** that lets SprigCast read Kick chat through
Kick's **official** developer API.

Kick's official API sends chat only to a web server, by webhook. A desktop app can't take webhooks, so this
Worker takes them for it and passes each channel's chat down a WebSocket to the SprigCast reading it
(`src/sprigcast/chat/kick.py`):

1. SprigCast opens `wss://kickchat.slothbloom.com/v1/chat?channel=<kick name>`.
2. The Worker looks the name up (`GET /public/v1/channels?slug=`) with SprigCast's own app token
   (client credentials), so the streamer never signs in.
3. That channel's object subscribes once to `chat.message.sent`, `moderation.banned` and the three
   `channel.subscription.*` events (`POST /public/v1/events/subscriptions`, `method: "webhook"`) and says
   `{"type":"ready"}`.
4. Kick posts each event to `https://kickchat.slothbloom.com/v1/kick/webhook`. The Worker checks Kick's
   signature (RSA SHA-256 over `<message id>.<timestamp>.<body>`, Kick's public key from
   `/public/v1/public-key`), drops replays and repeats, and sends `{"type":"event","event":...,"data":...}`
   to every SprigCast reading that channel.
5. Ten minutes after the last reader leaves, the object deletes the subscription and forgets the channel.

Light: objects sleep between messages (hibernation), and SprigCast's keep-alive `ping` is answered without
waking them. It keeps no chat, logs no tokens, and stores only a channel's subscription ids while it's being
read. The IP is read only for the rate limit (20 connections a minute per address).

It's separate from the sign-in helper, so a problem here can't touch sign-in. Cost: Workers' free plan
(Durable Objects with SQLite storage are included).

## Deploy (once)

**Easiest:** double-click `Set up Kick chat.bat` in this folder. It checks Node, signs you in to Cloudflare if
needed, deploys, asks for the Kick client secret, then opens Kick's developer settings and shows the webhook
URL and scope to set there (step 2 below). The steps it runs:

Needs the Cloudflare account that has slothbloom.com, Node, and SprigCast's Kick app
(kick.com → Settings → Developer, the same app as sign-in, Client ID in `wrangler.toml`).

1. Deploy and add the secret (the same Kick client secret as the sign-in helper):
   ```
   cd integrations\kick-chat
   npx wrangler login
   npx wrangler deploy
   npx wrangler secret put KICK_CLIENT_SECRET
   ```
2. In the Kick app's settings: turn **Enable webhooks** on, set the webhook URL to
   `https://kickchat.slothbloom.com/v1/kick/webhook`, and make sure the app has the `events:subscribe`
   scope. Save.
3. Check it: https://kickchat.slothbloom.com/v1/health shows `{"ok":true}`. Then put a Kick name in
   SprigCast's Chat → Channels… and watch a live Kick channel's chat come in.

## Changing it

**New Kick secret?** After regenerating it on kick.com, double-click `integrations\Update Kick secret.bat`: it asks
for the secret once and puts it in both this relay and the sign-in helper.

`npm test` runs the tests (Node 20+). `npx wrangler deploy` again after a change. SprigCast connects to
`RELAY` in `src/sprigcast/chat/kick.py` (`SPRIGCAST_KICK_CHAT_URL` or the `kick/chat_relay` setting point it
elsewhere for testing, e.g. `npx wrangler dev` on `ws://127.0.0.1:8787/v1/chat`).

The field names follow Kick's developer docs (docs.kick.com, "Events"). If Kick changes a payload, the
Worker passes it on unchanged, so the fix is usually in `message_from_event` in the app.
