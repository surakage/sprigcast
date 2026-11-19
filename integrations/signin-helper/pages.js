// The public pages at signin.slothbloom.com: the home page, the privacy policy and the terms. Google's
// OAuth verification needs a home page and a privacy policy on SprigCast's own domain, both open without
// signing in. Keep them true to what the app does: the YouTube calls live in
// src/sprigcast/output/service_signin.py and src/sprigcast/output/stream_details.py.

import { LOGO } from "./logo.js";

export const UPDATED = "November 18, 2026";
const REPO = "https://github.com/surakage/sprigcast";
const CONTACT = `${REPO}/issues`;

const STYLE = `
:root { color-scheme: dark; --bg: #0c1812; --bg2: #10211a; --card: #142a20; --card2: #183226; --line: #22402f;
  --text: #ecf5ef; --dim: #a7bfb0; --faint: #7d9787; --mint: #4fd88f; --mint-dark: #0c1812; }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.65 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
  -webkit-font-smoothing: antialiased; }
a { color: var(--mint); text-decoration: none; }
a:hover { text-decoration: underline; }
p, li { color: var(--dim); }
strong { color: var(--text); font-weight: 600; }
code { color: var(--text); background: var(--card2); border: 1px solid var(--line); border-radius: 6px;
  padding: 1px 6px; font-size: .88em; overflow-wrap: anywhere; }
.wrap { max-width: 1200px; margin: 0 auto; padding-left: 32px; padding-right: 32px; }

/* Top bar */
.top { position: sticky; top: 0; z-index: 10; background: rgba(12, 24, 18, .86); backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--line); }
.top .wrap { display: flex; align-items: center; gap: 24px; height: 64px; }
.brand { display: flex; align-items: center; gap: 10px; color: var(--text); font-weight: 700; font-size: 1.1rem; }
.brand:hover { text-decoration: none; }
.brand svg { width: 32px; height: 32px; }
.top nav { margin-left: auto; display: flex; align-items: center; gap: 24px; }
.top nav a { color: var(--dim); font-size: .95rem; }
.top nav a:hover, .top nav a.on { color: var(--text); text-decoration: none; }

/* Buttons */
.btn { display: inline-flex; align-items: center; gap: 8px; padding: 11px 20px; border-radius: 999px;
  font-weight: 600; font-size: .98rem; border: 1px solid var(--line); color: var(--text); background: var(--card); }
.btn:hover { text-decoration: none; border-color: var(--mint); }
.btn .glyph { width: 18px; height: 18px; flex: none; }
.icon { flex: none; }
.btn.main { background: var(--mint); border-color: var(--mint); color: var(--mint-dark); }
.btn.main:hover { filter: brightness(1.08); }
.top .btn { padding: 7px 16px; font-size: .9rem; }
.top nav a.btn.main { color: var(--mint-dark); }

/* Home */
.hero { background: radial-gradient(900px 420px at 78% 30%, rgba(79, 216, 143, .14), transparent 70%), var(--bg); }
.hero .wrap { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr); gap: 48px; align-items: center;
  padding-top: 88px; padding-bottom: 88px; }
.eyebrow { display: inline-block; color: var(--mint); font-weight: 600; font-size: .85rem; letter-spacing: .08em;
  text-transform: uppercase; margin: 0 0 16px; }
.hero h1 { font-size: clamp(2.2rem, 4.6vw, 3.6rem); line-height: 1.08; letter-spacing: -.02em; margin: 0 0 20px; }
.hero p.sub { font-size: 1.2rem; max-width: 560px; margin: 0 0 32px; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; }
.meta { margin: 24px 0 0; font-size: .9rem; color: var(--faint); }
.art { display: flex; justify-content: center; }
.art svg { width: min(320px, 100%); height: auto; filter: drop-shadow(0 24px 60px rgba(79, 216, 143, .22)); }
section { padding: 72px 0; border-top: 1px solid var(--line); }
section.alt { background: var(--bg2); }
.head { max-width: 680px; margin-bottom: 36px; }
.head h2 { font-size: 1.9rem; line-height: 1.2; letter-spacing: -.01em; margin: 0 0 12px; }
.head p { font-size: 1.08rem; margin: 0; }
.grid { display: grid; gap: 20px; }
.grid.four { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 24px; }
.tile h3 { font-size: 1.08rem; margin: 16px 0 8px; }
.tile p { margin: 0; font-size: .97rem; }
.tile .where { margin-top: 14px; font-size: .88rem; color: var(--faint); }
.icon { width: 44px; height: 44px; border-radius: 12px; display: grid; place-items: center;
  background: rgba(79, 216, 143, .12); color: var(--mint); }
.icon svg { width: 22px; height: 22px; }
.never { margin-top: 20px; display: flex; gap: 16px; align-items: flex-start; background: var(--card);
  border: 1px solid var(--line); border-radius: 16px; padding: 20px 24px; }
.never p { margin: 0; }

/* Policy pages */
.pagehead { padding: 64px 0 40px; border-bottom: 1px solid var(--line);
  background: radial-gradient(700px 260px at 85% 0%, rgba(79, 216, 143, .10), transparent 70%), var(--bg); }
.pagehead h1 { font-size: clamp(2rem, 4vw, 2.8rem); letter-spacing: -.02em; margin: 0 0 8px; line-height: 1.15; }
.pagehead p { margin: 0; max-width: 720px; font-size: 1.05rem; }
.pagehead .date { display: inline-block; margin-top: 16px; font-size: .85rem; color: var(--faint); }
.doc { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 64px; align-items: start; padding-top: 48px;
  padding-bottom: 72px; }
.doc aside { position: sticky; top: 96px; border-left: 1px solid var(--line); padding-left: 16px; font-size: .92rem; }
.doc aside p { margin: 0 0 10px; color: var(--faint); font-size: .78rem; font-weight: 600; letter-spacing: .08em;
  text-transform: uppercase; }
.doc aside a { display: block; padding: 5px 0; color: var(--dim); }
.doc aside a:hover { color: var(--mint); text-decoration: none; }
.doc article { max-width: 820px; }
.doc article > :first-child { margin-top: 0; }
.doc h2 { font-size: 1.4rem; margin: 48px 0 12px; padding-top: 4px; scroll-margin-top: 88px; }
.doc h3 { font-size: 1.06rem; margin: 28px 0 6px; color: var(--text); }
.doc ul { padding-left: 22px; }
.doc li { margin: 6px 0; }
.summary { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 8px 24px; }

/* Footer */
footer { border-top: 1px solid var(--line); background: var(--bg2); }
footer .wrap { display: grid; grid-template-columns: minmax(0, 2fr) repeat(2, minmax(0, 1fr)); gap: 32px;
  padding-top: 48px; padding-bottom: 32px; }
footer h4 { margin: 0 0 12px; font-size: .8rem; letter-spacing: .08em; text-transform: uppercase; color: var(--faint); }
footer a { display: block; color: var(--dim); padding: 3px 0; font-size: .95rem; }
footer a:hover { color: var(--text); text-decoration: none; }
footer p { margin: 12px 0 0; font-size: .92rem; max-width: 360px; }
.legal { border-top: 1px solid var(--line); }
.legal .wrap { display: block; padding-top: 18px; padding-bottom: 24px; }
.legal p { margin: 0; max-width: none; font-size: .82rem; color: var(--faint); }

@media (max-width: 980px) {
  .grid.four { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .doc { grid-template-columns: 1fr; gap: 24px; }
  .doc aside { position: static; }
}
@media (max-width: 760px) {
  .wrap { padding-left: 16px; padding-right: 16px; }
  .top nav a:not(.btn) { display: none; }
  .hero .wrap { grid-template-columns: 1fr; padding-top: 48px; padding-bottom: 56px; }
  .art { order: -1; justify-content: flex-start; }
  .art svg { width: 120px; }
  section { padding: 48px 0; }
  .grid.four, .grid.two, footer .wrap { grid-template-columns: 1fr; }
}
`;

const logo = (size) => LOGO.replace('width="72" height="72"', `width="${size}" height="${size}"`);

const ICONS = {
  scenes: '<path d="M4 5h16v11H4z"/><path d="M8 20h8M12 16v4"/><path d="M8 9h4v3H8z"/>',
  multi: '<circle cx="5" cy="12" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 12h10M7 11l10-5M7 13l10 5"/>',
  canvas: '<rect x="3" y="6" width="11" height="8" rx="1.5"/><rect x="16" y="4" width="5" height="16" rx="1.5"/>',
  light: '<path d="M12 3c-4 4-6 7-6 10a6 6 0 0 0 12 0c0-3-2-6-6-10z"/><path d="M12 21v-7"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
};
const icon = (name) => `<span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg></span>`;

const slug = (text) => text.toLowerCase().replace(/<[^>]+>/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

// A policy page: a heading band, then the text with a contents list down the side on wide screens.
function policy(title, intro, body) {
  const heads = [];
  const marked = body.replace(/<h2>(.*?)<\/h2>/g, (_, text) => {
    heads.push([slug(text), text]);
    return `<h2 id="${slug(text)}">${text}</h2>`;
  });
  const links = heads.map(([id, text]) => `<a href="#${id}">${text}</a>`).join("\n");
  return `<div class="pagehead"><div class="wrap"><h1>${title}</h1><p>${intro}</p>
<span class="date">Last updated ${UPDATED}</span></div></div>
<div class="wrap doc"><aside><p>On this page</p>
${links}
</aside><article>${marked}</article></div>`;
}

function page(title, path, body) {
  const on = (href) => (href === path ? ' class="on"' : "");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<meta name="description" content="SprigCast is a free, open-source streaming and recording app for Windows.">
<meta name="theme-color" content="#0c1812">
<style>${STYLE}</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a class="brand" href="/">${logo(32)}SprigCast</a>
  <nav>
    <a href="/privacy"${on("/privacy")}>Privacy</a>
    <a href="/terms"${on("/terms")}>Terms</a>
    <a href="${REPO}">GitHub</a>
    <a class="btn main" href="${REPO}/releases/latest">Download</a>
  </nav>
</div></header>
<main>
${body}
</main>
<footer>
  <div class="wrap">
    <div><a class="brand" href="/">${logo(32)}SprigCast</a>
      <p>Free, open-source streaming and recording for Windows. Made by
      <a href="https://slothbloom.com" style="display:inline">Sloth Bloom</a>.</p></div>
    <div><h4>Product</h4><a href="${REPO}/releases/latest">Download</a><a href="${REPO}">Source code</a>
      <a href="${CONTACT}">Contact and support</a></div>
    <div><h4>Legal</h4><a href="/privacy">Privacy policy</a><a href="/terms">Terms</a>
      <a href="${REPO}/blob/main/LICENSE">License (GPL-3.0)</a></div>
  </div>
  <div class="legal"><div class="wrap"><p>© 2026 Sloth Bloom. YouTube is a trademark of Google LLC. SprigCast isn't
  made or endorsed by Google, YouTube, Kick or Twitch.</p></div></div>
</footer>
</body>
</html>
`;
}

export const HOME = page("SprigCast: free streaming and recording for Windows", "/", `
<div class="hero"><div class="wrap">
  <div>
    <p class="eyebrow">Free and open source</p>
    <h1>Go live everywhere, light on your PC.</h1>
    <p class="sub">SprigCast puts your camera, game, screen, pictures and sound into scenes, then streams them and
    records them on your PC. Easy to set up, with no accounts and no ads.</p>
    <div class="actions">
      <a class="btn main" href="${REPO}/releases/latest"><svg class="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS.download}</svg>Download for Windows</a>
      <a class="btn" href="${REPO}">View source code</a>
    </div>
    <p class="meta">Windows 10 and 11 · 64-bit · GPL-3.0</p>
  </div>
  <div class="art">${logo(320)}</div>
</div></div>

<section><div class="wrap">
  <div class="head"><h2>Everything a stream needs</h2>
  <p>Built for streamers who want it simple and smooth, from a fast gaming PC to an older laptop.</p></div>
  <div class="grid four">
    <div class="tile">${icon("scenes")}<h3>Scenes and sources</h3><p>Cameras, games, windows, pictures, video,
    sound and overlays, arranged the way you like.</p></div>
    <div class="tile">${icon("multi")}<h3>Stream everywhere</h3><p>YouTube, Twitch, Kick and more at the same time,
    from one app.</p></div>
    <div class="tile">${icon("canvas")}<h3>Horizontal and vertical</h3><p>A wide canvas and a tall one side by
    side, for regular streams and phone viewers.</p></div>
    <div class="tile">${icon("light")}<h3>Light on your PC</h3><p>Uses your graphics card to encode, and anything
    you're not using goes to sleep.</p></div>
  </div>
</div></section>

<section class="alt"><div class="wrap">
  <div class="head"><h2>Signing in with YouTube</h2>
  <p>Signing in is optional. You can always paste your stream key instead. If you sign in with Google, SprigCast
  uses your YouTube account for two things, and only when you ask it to.</p></div>
  <div class="grid two">
    <div class="tile">${icon("key")}<h3>Fill in your stream key</h3>
    <p>Sign in reads your channel name and finds your reusable stream key. If you don't have one, SprigCast makes one
    called "SprigCast". Right after that, SprigCast cancels the sign-in with Google and keeps only the key, on your
    PC.</p>
    <p class="where">In SprigCast: Live → Destinations → + Add → YouTube → Sign in</p></div>
    <div class="tile">${icon("edit")}<h3>Set your stream details</h3>
    <p>Set your stream's title, description, privacy, start time, category, tags and thumbnail. SprigCast reads your
    live or upcoming YouTube stream, saves your changes when you press Save, and makes a new upcoming stream on your
    key if there isn't one. This sign-in stays on your PC, kept by Windows, until you press Sign out.</p>
    <p class="where">In SprigCast: Live → Destinations → Details</p></div>
  </div>
  <div class="never">${icon("shield")}<p><strong>What SprigCast never does.</strong> It never uploads videos,
  deletes anything on your channel, reads your comments or messages, or touches other Google services. Your YouTube
  data goes only between your PC and YouTube. Read the <a href="/privacy">privacy policy</a>.</p></div>
</div></section>
`);

export const PRIVACY = page("Privacy policy · SprigCast", "/privacy", policy("Privacy policy",
  `This policy covers SprigCast, the streaming and recording app for Windows, and its sign-in helper at
  signin.slothbloom.com. SprigCast is made by Sloth Bloom.`, `
<h2>The short version</h2>
<ul class="summary">
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
for the features you sign in for, such as your stream key and stream details, and writing in your chat or timing out
and banning an account there when you choose to. Chat from YouTube and Twitch is read with only your channel
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
`));

export const TERMS = page("Terms · SprigCast", "/terms", policy("Terms",
  "The terms for using SprigCast and its sign-in helper.", `
<h2>License</h2>
<p>SprigCast is free, open-source software under the
<a href="${REPO}/blob/main/LICENSE">GNU General Public License v3</a>. You can use, change and share it under that
license. It comes with no warranty: use it at your own risk.</p>

<h2>Your content</h2>
<p>You're responsible for what you stream and record, and for following the rules of the services you stream to,
including the <a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>.</p>

<h2>The sign-in helper</h2>
<p>The sign-in helper at signin.slothbloom.com is there only for SprigCast's own sign-ins. Please don't use it for
anything else.</p>

<h2>Privacy</h2>
<p>How SprigCast handles your data is in the <a href="/privacy">privacy policy</a>.</p>
`));

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
      "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
    },
  });
}
