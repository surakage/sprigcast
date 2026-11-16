import assert from "node:assert/strict";
import { test } from "node:test";

import worker, { MAX_BYTES, reportId } from "../worker.js";

function env({ limited = false } = {}) {
  const stored = [];
  return {
    stored,
    REPORTS: { put: async (key, body, options) => stored.push({ key, size: body.byteLength, options }) },
    LIMITER: { limit: async () => ({ success: !limited }) },
  };
}
const ctx = { waitUntil() {} };
const zip = () => new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3]);

function post(body, headers = {}) {
  return new Request("https://reports.example/v1/report", {
    method: "POST",
    body,
    headers: { "content-type": "application/zip", ...headers },
  });
}

test("a report is kept by day and number, with its version, kind and cause", async () => {
  const e = env();
  const response = await worker.fetch(
    post(zip(), { "x-sprigcast-version": "0.2.0", "x-sprigcast-kind": "crash",
                  "x-sprigcast-cause": "Access violation in nvcuda64.dll" }), e, ctx);
  assert.equal(response.status, 201);
  const { id } = await response.json();
  assert.match(id, /^SC-[2-9A-HJ-NP-Z]{6}$/);
  assert.equal(e.stored.length, 1);
  assert.match(e.stored[0].key, new RegExp(`^\\d{4}-\\d{2}-\\d{2}/${id}\\.zip$`));
  assert.deepEqual(e.stored[0].options.customMetadata,
                   { version: "0.2.0", kind: "crash", cause: "Access violation in nvcuda64.dll" });
});

test("not a zip, too big, too many or the wrong method is refused and nothing is kept", async () => {
  const e = env();
  assert.equal((await worker.fetch(post(new Uint8Array([1, 2, 3, 4])), e, ctx)).status, 415);
  assert.equal((await worker.fetch(post(new Uint8Array(MAX_BYTES + 1)), e, ctx)).status, 413);
  assert.equal((await worker.fetch(post(zip()), env({ limited: true }), ctx)).status, 429);
  assert.equal((await worker.fetch(new Request("https://reports.example/v1/report"), e, ctx)).status, 405);
  assert.equal(e.stored.length, 0);
});

test("health answers and ids avoid look-alike letters", async () => {
  const r = await worker.fetch(new Request("https://reports.example/v1/health"), env(), ctx);
  assert.deepEqual(await r.json(), { ok: true });
  assert.equal(reportId(new Uint8Array([0, 1, 31, 32, 8, 9])), "SC-23Z2AB");
});
