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

test("a stream summary keeps its kind, and a test build's whole version label", async () => {
  const e = env();
  const version = "0.2.0 test build 44932007527 (eafa358)";
  const response = await worker.fetch(
    post(zip(), { "x-sprigcast-version": version, "x-sprigcast-kind": "stream",
                  "x-sprigcast-cause": "stream of 148 min" }), e, ctx);
  assert.equal(response.status, 201);
  assert.deepEqual(e.stored[0].options.customMetadata, { version, kind: "stream", cause: "stream of 148 min" });
  await worker.fetch(post(zip(), { "x-sprigcast-kind": "anything" }), e, ctx);
  assert.equal(e.stored[1].options.customMetadata.kind, "problem");
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

function readable(objects) {
  return {
    READ_TOKEN: "a-long-read-token-1234",
    REPORTS: {
      list: async ({ prefix }) => ({ objects: objects.filter((o) => o.key.startsWith(prefix)), truncated: false }),
      get: async (key) => (objects.some((o) => o.key === key) ? { body: "zipbytes" } : null),
    },
  };
}

test("an import report keeps its install id", async () => {
  const e = env();
  const response = await worker.fetch(post(zip(), { "x-sprigcast-kind": "import", "x-sprigcast-version": "0.4.0",
                                                    "x-sprigcast-install": "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90" }),
                                      e, ctx);
  assert.equal(response.status, 201);
  assert.equal(e.stored[0].options.customMetadata.kind, "import");
  assert.equal(e.stored[0].options.customMetadata.install, "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90");
});

test("reports can be listed and fetched only with the read token", async () => {
  const today = new Date().toISOString().slice(0, 10);
  const e = readable([{ key: `${today}/SC-ABCDEF.zip`, size: 1234,
                        customMetadata: { version: "0.4.0", kind: "crash", cause: "x" } }]);
  const get = (path, token) => new Request(`https://reports.example${path}`,
                                           { headers: token ? { authorization: `Bearer ${token}` } : {} });
  assert.equal((await worker.fetch(get("/v1/reports"), e, ctx)).status, 404);
  assert.equal((await worker.fetch(get("/v1/reports", "wrong-token-wrong-token"), e, ctx)).status, 404);
  const listed = await worker.fetch(get(`/v1/reports?since=${today}`, e.READ_TOKEN), e, ctx);
  assert.equal(listed.status, 200);
  assert.deepEqual(await listed.json(), [{ id: "SC-ABCDEF", date: today, version: "0.4.0", kind: "crash", cause: "x",
                                           install: "", size: 1234 }]);
  const one = await worker.fetch(get("/v1/reports/SC-ABCDEF", e.READ_TOKEN), e, ctx);
  assert.equal(one.status, 200);
  assert.equal(await one.text(), "zipbytes");
  assert.equal((await worker.fetch(get("/v1/reports/SC-ZZZZZZ", e.READ_TOKEN), e, ctx)).status, 404);
  assert.equal((await worker.fetch(get("/v1/reports/../x", e.READ_TOKEN), e, ctx)).status, 404);
});
