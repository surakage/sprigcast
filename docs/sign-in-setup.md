# Setting up YouTube and Kick sign-in

SprigCast can sign in to YouTube and Kick to fill in the stream key, so nobody pastes one. Each service needs
SprigCast registered with it once. Code: `src/sprigcast/output/service_signin.py`.

The Client IDs are public and sit in the app. The client secrets never do: they're kept only in the sign-in
helper (`integrations/signin-helper`, at signin.slothbloom.com), which finishes each sign-in. Its README has
the deploy and secret commands.

SprigCast only reads the key. It throws the sign-in away right after, so there are no tokens to keep or refresh.

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
page, a privacy policy page and a short video of the sign-in, and takes days to weeks.

## Kick

1. Turn on two-factor sign-in on your Kick account (Settings → Security).
2. Go to <https://kick.com/settings/developer> and **Create app**:
   - Name SprigCast, the sloth logo.
   - Redirect URL exactly: `http://localhost:51587/callback`
   - Scopes: **Read user information**, **Read channel information**, **Read stream URL and stream key**
     (`user:read channel:read streamkey:read`).
3. The **Client ID** goes in `DEFAULTS` and `wrangler.toml`. The **Client Secret** goes only into the helper:
   `npx wrangler secret put KICK_CLIENT_SECRET`.

## X

X has no public way for apps to get the stream server and key, so X stays paste-your-key. The keys are in
X Media Studio → Producer → Sources.
