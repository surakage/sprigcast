# SprigCast report inbox

A small Cloudflare Worker at **https://reports.slothbloom.com** that receives the reports SprigCast sends
(the card after a crash, and Help → Report a problem). Each report is the scrubbed zip from
`app/problem_report.py` (no stream keys, passwords, tokens, names, PC name or IPs inside). The Worker keeps
it in the R2 bucket `sprigcast-reports` as `YYYY-MM-DD/SC-XXXXXX.zip` and answers with that number, which
the tester can quote. It stores nothing else (no IP address, no cookies); it reads the IP only to allow at
most 5 reports a minute from one address. Reports are deleted after 30 days.

It's separate from the slothbloom.com website Worker, so deploying one never touches the other.

Cost: free. Workers' free plan covers 100,000 requests a day; R2's free tier covers 10 GB stored and a
million uploads a month. Reports are 50-500 KB.

## Deploy (once)

Needs the Cloudflare account that has slothbloom.com, and Node (already on the PC).

```
cd integrations\report-inbox
npx wrangler login
npx wrangler r2 bucket create sprigcast-reports
npx wrangler r2 bucket lifecycle add sprigcast-reports delete-after-30-days --expire-days 30
npx wrangler deploy
```

`wrangler deploy` creates the Worker `sprigcast-reports` and the `reports.slothbloom.com` address (DNS and
certificate included; give it a minute). Check it: https://reports.slothbloom.com/v1/health shows
`{"ok":true}`.

Optional, a Discord message for each new report: make a webhook in a private channel
(Channel settings → Integrations → Webhooks → New webhook → Copy URL), then

```
npx wrangler secret put DISCORD_WEBHOOK
```

and paste the URL.

## Reading reports

Cloudflare dashboard → R2 → `sprigcast-reports`: one folder per day, one zip per report. Download it and
pass it to Claude like a File → Export report zip. Or from the command line:

```
npx wrangler r2 object get sprigcast-reports/2026-11-16/SC-7F3K2M.zip --file SC-7F3K2M.zip --remote
```

## Pulling reports with a token (for the team and Claude threads)

The Worker can list and hand out reports to anyone with its read token. Make one long random token, then:

```
cd integrations\report-inbox
npx wrangler secret put READ_TOKEN
npx wrangler deploy
```

Paste the same token into the Claude cloud environment's environment variables as `SPRIGCAST_REPORTS_TOKEN`,
and allow `reports.slothbloom.com` in that environment's network access. Then:

```
python integrations/report-inbox/pull_reports.py --since 2026-11-27          # list
python integrations/report-inbox/pull_reports.py SC-7F3K2M --out reports/    # download one zip
```

Without the token, `/v1/reports` answers 404, as if it weren't there.

## Changing it

`npm test` runs the tests (Node 20+). `npx wrangler deploy` again after a change. SprigCast sends to
`REPORT_URL` in `src/sprigcast/app/problem_report.py` (`SPRIGCAST_REPORT_URL` overrides it for testing).
