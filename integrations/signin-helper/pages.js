// The public pages at signin.slothbloom.com: the home page, the privacy policy and the terms. Google's
// OAuth verification needs a home page and a privacy policy on SprigCast's own domain, both open without
// signing in. Keep them true to what the app does: the YouTube calls live in
// src/sprigcast/output/service_signin.py and src/sprigcast/output/stream_details.py.

import { LOGO } from "./logo.js";

export const UPDATED = "November 18, 2026";
const REPO = "https://github.com/surakage/sprigcast";
const CONTACT = `${REPO}/issues`;

const STYLE = `
:root { color-scheme: dark; --bg: #0f1d17; --card: #16291f; --line: #24402f; --text: #e7f2ea; --dim: #a9c2b1;
  --mint: #4fd88f; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.6 "Segoe UI", system-ui, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 40px 20px 64px; }
header { display: flex; align-items: center; gap: 16px; margin-bottom: 24px; }
header svg { flex: none; border-radius: 16px; }
h1 { font-size: 2rem; margin: 0; line-height: 1.2; }
h2 { font-size: 1.25rem; margin: 32px 0 8px; }
h3 { font-size: 1.05rem; margin: 20px 0 4px; }
p, li { color: var(--dim); }
strong { color: var(--text); }
a { color: var(--mint); }
.lead { font-size: 1.15rem; color: var(--text); margin: 0; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 4px 20px 12px;
  margin: 16px 0; }
nav { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 24px 0; }
footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--line); font-size: .9rem; color: var(--dim); }
code { color: var(--text); }
`;

function page(title, body) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<meta name="description" content="SprigCast is a free, open-source streaming and recording app for Windows.">
<style>${STYLE}</style>
</head>
<body>
<main>
${body}
<footer>
  <p>SprigCast is made by Sloth Bloom (<a href="https://slothbloom.com">slothbloom.com</a>).
  <a href="/">Home</a> · <a href="/privacy">Privacy policy</a> · <a href="/terms">Terms</a> ·
  <a href="${REPO}">Source code</a> · <a href="${CONTACT}">Contact</a></p>
  <p>YouTube is a trademark of Google LLC. SprigCast isn't made or endorsed by Google, YouTube, Kick or Twitch.</p>
</footer>
</main>
</body>
</html>
`;
}

export const HOME = page("SprigCast", `
<header>${LOGO}<div><h1>SprigCast</h1>
<p class="lead">Free, open-source streaming and recording for Windows.</p></div></header>

<p>SprigCast puts your camera, game, screen, pictures and sound into scenes, then streams them live and records
them on your PC. It streams to YouTube, Twitch, Kick and other services at the same time, with a horizontal and a
vertical canvas. It's built to be easy to set up and light on your PC. There are no accounts and no ads.</p>

<nav>
  <a href="${REPO}/releases/latest">Download for Windows</a>
  <a href="${REPO}">Source code</a>
  <a href="/privacy">Privacy policy</a>
  <a href="/terms">Terms</a>
</nav>

<h2>Signing in with YouTube</h2>
<p>Signing in is optional. You can always paste your stream key instead. If you sign in with Google, SprigCast
uses your YouTube account for two things, and only when you ask it to:</p>
<div class="card">
<h3>Fill in your stream key</h3>
<p>When you add YouTube under <strong>Live → Destinations</strong>, <strong>Sign in</strong> reads your channel
name and finds your reusable stream key. If you don't have one, SprigCast makes one called "SprigCast". Right after that,
SprigCast cancels the sign-in with Google and keeps only the key, on your PC.</p>
</div>
<div class="card">
<h3>Set your stream details</h3>
<p>In <strong>Live → Destinations → Details</strong>, you can set your stream's title, description, privacy,
start time, category, tags and thumbnail from SprigCast. It reads your live or upcoming YouTube stream, saves your
changes to it when you press Save, and makes a new upcoming stream on your key if there isn't one. This sign-in
stays on your PC, kept by Windows, until you press <strong>Sign out</strong>.</p>
</div>
<p>SprigCast never uploads videos, deletes anything on your channel, reads your comments or messages, or touches
other Google services. Your YouTube data goes only between your PC and YouTube. The full details are in the
<a href="/privacy">privacy policy</a>.</p>
`);

export const PRIVACY = page("SprigCast privacy policy", `
<h1>Privacy policy</h1>
<p>Last updated ${UPDATED}.</p>

<p>This policy covers SprigCast, the streaming and recording app for Windows, and its sign-in helper at
signin.slothbloom.com. SprigCast is made by Sloth Bloom.</p>

<h2>The short version</h2>
<ul>
  <li>SprigCast runs on your PC. There are no SprigCast accounts, ads, analytics or tracking.</li>
  <li>We don't sell, rent or share your data, and we don't use it for advertising or to train AI models.</li>
  <li>Your YouTube data goes only between your PC and YouTube. We don't keep a copy of it.</li>
</ul>

<h2>Signing in with Google (YouTube)</h2>
<p>Signing in with YouTube is optional. SprigCast asks Google for one permission, "Manage your YouTube account"
(<code>https://www.googleapis.com/auth/youtube</code>), and uses it only for what's below.</p>

<h3>What SprigCast reads and changes</h3>
<ul>
  <li><strong>Your channel name</strong>, to show which account you signed in with.</li>
  <li><strong>Your live stream keys</strong>, to find a reusable stream key. If you have none, SprigCast makes one
  called "SprigCast" (or "SprigCast Vertical" for a vertical destination).</li>
  <li><strong>Your live and upcoming streams</strong> (title, description, privacy, start time, category, tags and
  thumbnail), only when you open Stream details. SprigCast changes them only when you press Save. If you don't
  have an upcoming stream, it makes one and connects it to your stream key.</li>
  <li><strong>A thumbnail you pick</strong>, uploaded to your stream when you press Save.</li>
</ul>
<p>SprigCast doesn't upload or delete videos, read comments, messages or subscribers, see your Google password,
or use any other Google service.</p>

<h3>Where it's kept</h3>
<ul>
  <li><strong>Stream key sign-in:</strong> SprigCast keeps only the stream key, in your PC's Windows Credential
  Manager. It cancels the sign-in with Google straight away, so nothing is left that could reach your
  account.</li>
  <li><strong>Stream details sign-in:</strong> the sign-in tokens are kept in Windows Credential Manager on your PC,
  protected by Windows, so you don't have to sign in again each time. <strong>Sign out</strong> on the Stream
  details card deletes them and cancels them with Google.</li>
  <li>The title and other details you type stay in SprigCast's settings on your PC.</li>
</ul>

<h3>The sign-in helper</h3>
<p>Google gives each app a secret that mustn't ship inside the app. So the last step of signing in goes through
SprigCast's sign-in helper at signin.slothbloom.com, which adds that secret and passes Google's answer straight
back to your PC. The helper doesn't store or log your sign-in code, tokens or any YouTube data. It looks at your
IP address only to allow at most 10 sign-ins a minute from one address, and doesn't keep it. It runs on
Cloudflare, which handles the connection like any website host does.</p>

<h3>Google's Limited Use rules</h3>
<p>SprigCast's use and transfer of information received from Google APIs to any other app will adhere to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data
Policy</a>, including the Limited Use requirements. Your Google data is used only to provide the features above,
which you can see and control in the app. No person reads it, and it's never transferred to anyone else.</p>

<h3>YouTube API Services</h3>
<p>SprigCast uses YouTube API Services. By signing in with YouTube, you agree to the
<a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>. Google's handling of your data is covered
by the <a href="https://policies.google.com/privacy">Google Privacy Policy</a>.</p>

<h3>Removing access</h3>
<p>Press <strong>Sign out</strong> in SprigCast, or remove SprigCast at any time from your Google Account's
<a href="https://myaccount.google.com/connections">third-party connections</a> page
(<a href="https://security.google.com/settings/security/permissions">security settings</a>). Uninstalling SprigCast
and deleting its settings removes everything it kept on your PC.</p>

<h2>Other services</h2>
<p>Kick and Twitch sign-ins work the same way: they're optional, the tokens stay on your PC, and they're used only
for the features you sign in for, such as your stream key and stream details. Chat from YouTube and Twitch is read with only your channel
name, with no sign-in. Kick chat comes through Kick's official API by way of SprigCast's chat relay
(kickchat.slothbloom.com): it passes a channel's public chat on as it arrives and keeps none of it.</p>

<h2>Other things SprigCast sends</h2>
<ul>
  <li><strong>Your streams</strong> go to the services you add, using their stream keys.</li>
  <li><strong>Update check:</strong> SprigCast asks GitHub for the newest version number.</li>
  <li><strong>Problem reports:</strong> only when you press Send report. A report holds your note and, if you
  leave it ticked, SprigCast's logs and PC details with stream keys, tokens, passwords, email addresses, your
  user name and your PC's name hidden. Reports are kept for 30 days to fix bugs and then deleted.</li>
</ul>

<h2>Cookies</h2>
<p>SprigCast and these pages don't use cookies or similar tracking.</p>

<h2>Children</h2>
<p>SprigCast isn't aimed at children under 13 and doesn't knowingly collect anything from them.</p>

<h2>Changes</h2>
<p>If this policy changes, the new version will be posted here with a new date.</p>

<h2>Contact</h2>
<p>Questions or requests about your data: open an issue at <a href="${CONTACT}">${CONTACT}</a>, or use
the support email shown on the Google sign-in screen.</p>
`);

export const TERMS = page("SprigCast terms", `
<h1>Terms</h1>
<p>Last updated ${UPDATED}.</p>
<p>SprigCast is free, open-source software under the
<a href="${REPO}/blob/main/LICENSE">GNU General Public License v3</a>. You can use, change and share it under that
license. It comes with no warranty: use it at your own risk.</p>
<p>You're responsible for what you stream and record, and for following the rules of the services you stream to,
including the <a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>.</p>
<p>The sign-in helper at signin.slothbloom.com is there only for SprigCast's own sign-ins. Please don't use it for
anything else. How SprigCast handles your data is in the <a href="/privacy">privacy policy</a>.</p>
`);

export const PAGES = { "/": HOME, "/privacy": PRIVACY, "/terms": TERMS };

export function servePage(request, path) {
  const html = PAGES[path];
  if (!html) return null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { allow: "GET, HEAD" } });
  }
  return new Response(request.method === "HEAD" ? null : html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "public, max-age=3600",
      "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
    },
  });
}
