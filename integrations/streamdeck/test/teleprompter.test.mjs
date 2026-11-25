import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { TELEPROMPTER_TITLES, teleprompterCommand, teleprompterPort, teleprompterSend, teleprompterUrl } from "../src/teleprompter.js";

test("every command the teleprompter app takes is on the key", () => {
	assert.deepEqual(Object.keys(TELEPROMPTER_TITLES).sort(),
		["bigger", "faster", "mirror", "next", "pause", "prev", "reset", "slower", "smaller", "start", "toggle"]);
	assert.equal(teleprompterCommand({}), "toggle");
	assert.equal(teleprompterCommand({ command: "faster" }), "faster");
	assert.equal(teleprompterCommand({ command: "constructor" }), "toggle");
});

test("port: 8765 unless a real port is set", () => {
	assert.equal(teleprompterPort({}), 8765);
	assert.equal(teleprompterPort({ tpPort: "" }), 8765);
	assert.equal(teleprompterPort({ tpPort: "9000" }), 9000);
	assert.equal(teleprompterPort({ tpPort: "70000" }), 8765);
	assert.equal(teleprompterPort({ tpPort: "abc" }), 8765);
	assert.equal(teleprompterUrl("next", 8765), "http://127.0.0.1:8765/control/next");
});

test("sends GET /control/<command> to the app", async () => {
	const seen = [];
	const server = http.createServer((req, res) => { seen.push(`${req.method} ${req.url}`); res.end("ok"); });
	await new Promise((r) => server.listen(0, "127.0.0.1", r));
	try {
		await teleprompterSend("faster", server.address().port);
		assert.deepEqual(seen, ["GET /control/faster"]);
	} finally {
		server.close();
	}
});

test("not running: it throws (the key shows an alert)", async () => {
	const server = http.createServer();
	await new Promise((r) => server.listen(0, "127.0.0.1", r));
	const port = server.address().port;
	await new Promise((r) => server.close(r));
	await assert.rejects(teleprompterSend("toggle", port), /not reached/);
});

test("an app that says no is an error too", async () => {
	const server = http.createServer((_req, res) => { res.statusCode = 404; res.end(); });
	await new Promise((r) => server.listen(0, "127.0.0.1", r));
	try {
		await assert.rejects(teleprompterSend("bogus", server.address().port), /answered 404/);
	} finally {
		server.close();
	}
});
