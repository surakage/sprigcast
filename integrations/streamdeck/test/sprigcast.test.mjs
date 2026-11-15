import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { SprigCast, authAnswer } from "../src/sprigcast.js";

test("password answer matches SprigCast's (obs-websocket 5 scheme)", () => {
	assert.equal(authAnswer("leafy", "salt1", "chal2"), "0CjBF+kyqOBOjAwldo0zscggufE/eEKk/jy6hl6VImM=" /* sprigcast.control.protocol.auth_string */);
});

class FakeSocket extends EventEmitter {
	static last = null;
	constructor(url) {
		super();
		this.url = url;
		this.sent = [];
		FakeSocket.last = this;
	}
	send(text) {
		this.sent.push(JSON.parse(text));
	}
	close() {}
}

test("connects only while wanted, and tracks what's on air", async () => {
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	assert.equal(FakeSocket.last, null);
	sc.want(true);
	const socket = FakeSocket.last;
	assert.equal(socket.url, "ws://127.0.0.1:4456/sprigcast");
	socket.emit("message", JSON.stringify({ hello: { auth: null } }));
	assert.deepEqual(socket.sent[0].auth, null);
	socket.emit("message", JSON.stringify({ welcome: {} }));
	assert.equal(sc.ready, true);
	const first = socket.sent[1];
	assert.equal(first.do, "bloom.current");
	socket.emit("message", JSON.stringify({ id: first.id, ok: true, result: { onAir: { horizontal: { name: "BRB" } } } }));
	await new Promise((r) => setImmediate(r));
	assert.equal(sc.isOnAir("BRB"), true);
	socket.emit("message", JSON.stringify({ event: "bloom.shown", bloom: "Gaming", shape: "horizontal" }));
	assert.equal(sc.isOnAir("Gaming"), true);
	socket.emit("message", JSON.stringify({ event: "sound.changed", name: "Mic", muted: true }));
	assert.equal(sc.state.sounds.Mic, true);
	sc.want(false);
	assert.equal(sc.socket, null);
});
