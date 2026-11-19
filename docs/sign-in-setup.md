# Setting up YouTube and Kick sign-in

SprigCast can sign in to YouTube and Kick to fill in the stream key, so nobody pastes one. Each service needs
SprigCast registered with it once. Code: `src/sprigcast/output/service_signin.py`.

The Client IDs are public and sit in the app. The client secrets never do: they're kept only in the sign-in
helper (`integrations/signin-helper`, at signin.slothbloom.com), which finishes each sign-in. Its README has
the deploy and secret commands.

For the stream key SprigCast only reads the key and throws the sign-in away right after.

**Stream details** (Live → Destinations → Details) is the one place that stays signed in, because it changes
your title, category and so on whenever you save. Its sign-in asks for offline access (Google) or
`channel:write` (Kick), keeps the tokens in Windows Credential Manager (`youtube/oauth`, `kick/oauth`) and
renews them through the helper (`grant_type=refresh_token`). Sign out on the card throws them away.
Twitch reuses its connected account, which now also asks for `channel:manage:broadcast`.
Code: `src/sprigcast/output/stream_details.py`.

While the Google app is in **Testing**, Google lets a sign-in stay for only 7 days, so Stream details asks you
to sign in to YouTube again once a week until the app is published.

## YouTube (Google)

1. Go to <https://console.cloud.google.com/>, sign in, and create a project named **SprigCast**.
2. **APIs & Services → Library**: turn on **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding / Audience):
   - App name SprigCast, your support email, the sloth logo.
   - Audience: **External**. Leave it in **Testing** for now.
   - **Data access**: add the scope `https://www.googleapis.com/auth/youtube`.
   - **Test users**: add the Google accounts that will sign in (yours first).
4. **Clients → Create client**: type **Desktop app**, name SprigCast. Copy the **Client ID** and **Client secret**.
5. The Client ID goes in `DEFAULTS` in `service_signin.py` and in the helper's `wrangler.toml`. The secret goes
   only into the helper: `npx wrangler secret put YOUTUBE_CLIENT_SECRET`.

While the app is in Testing, Google shows an "unverified app" screen and only the test users can sign in.
To open it to everyone, press **Publish app** and complete Google's verification. Verification needs a home
page, a privacy policy page and a short video of the sign-in, and takes days to weeks. The pages are served by
the sign-in helper (`integrations/signin-helper/pages.js`): home page `https://signin.slothbloom.com/`, privacy
policy `https://signin.slothbloom.com/privacy`, terms `https://signin.slothbloom.com/terms`. Authorized
domain: `slothbloom.com` (verify it in Google Search Console first).

## Kick

1. Turn on two-factor sign-in on your Kick account (Settings → Security).
2. Go to <https://kick.com/settings/developer> and **Create app**:
   - Name SprigCast, the sloth logo.
   - Redirect URL exactly: `http://localhost:51587/callback`
   - Scopes: **Read user information**, **Read channel information**, **Read stream URL and stream key**,
     **Update livestream metadata** (`user:read channel:read streamkey:read channel:write`; the last one is
     for Stream details).
   - For chat back and moderation from the Chat panel, also tick **Write to chat** and the ban permission
     (`chat:write moderation:ban`). SprigCast asks for these two only after someone chooses to chat or moderate
     on Kick, so a sign-in for the stream key never depends on them.
3. The **Client ID** goes in `DEFAULTS` and `wrangler.toml`. The **Client Secret** goes only into the helper:
   `npx wrangler secret put KICK_CLIENT_SECRET`.

## X

X has no public way for apps to get the stream server and key, so X stays paste-your-key. The keys are in
X Media Studio → Producer → Sources.
