import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { AUTO_PORTS, EVENTS, SprigCast, authAnswer } from "../src/sprigcast.js";

test("password answer matches SprigCast's (obs-websocket 5 scheme)", () => {
	assert.equal(authAnswer("leafy", "salt1", "chal2"), "0CjBF+kyqOBOjAwldo0zscggufE/eEKk/jy6hl6VImM=" /* sprigcast.control.protocol.auth_string */);
});

class FakeSocket extends EventEmitter {
	static all = [];
	constructor(url) {
		super();
		this.url = url;
		this.sent = [];
		this.closed = false;
		FakeSocket.all.push(this);
	}
	static get last() {
		return FakeSocket.all.at(-1) ?? null;
	}
	send(text) {
		this.sent.push(JSON.parse(text));
	}
	close() {
		this.closed = true;
	}
	say(message) {
		this.emit("message", JSON.stringify(message));
	}
}

const tick = () => new Promise((r) => setImmediate(r));

// What SprigCast answers to the catch-up, as in tests/test_streamdeck_contract.py.
const ANSWERS = {
	"bloom.current": { onAir: { horizontal: { name: "BRB" }, vertical: null } },
	"sounds.list": { sounds: [{ name: "Mic", muted: false, volumeDb: -6 }] },
	"stream.status": { on: true },
	"record.status": { on: false },
	"clips.status": { on: true },
	"camera.status": { on: false },
	"gardens.list": { garden: "Main", gardens: ["Main"] },
	"paths.list": { path: "Main", paths: ["Main"] },
	"transition.get": { kind: "fade", ms: 300, kinds: ["cut", "fade"] },
	"media.list": { media: [{ media: "Intro", state: "playing" }] },
	"sprigs.list": { sprigs: [{ name: "Frame", state: "Quiet", states: ["Quiet"] }] },
};

async function welcome(socket) {
	socket.say({ hello: { auth: null } });
	socket.say({ welcome: {} });
	for (const request of socket.sent.filter((m) => m.do)) {
		const result = ANSWERS[request.do];
		socket.say(result ? { id: request.id, ok: true, result } : { id: request.id, ok: false, error: { code: "unknown_action", message: "no" } });
	}
	await tick();
}

test("connects only while wanted, catches up, and tracks what's on air", async () => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	assert.equal(FakeSocket.last, null);
	sc.want(true);
	const socket = FakeSocket.last;
	assert.equal(socket.url, "ws://127.0.0.1:4456/sprigcast");
	await welcome(socket);
	assert.deepEqual(socket.sent[0], { auth: null, events: EVENTS });
	assert.equal(sc.ready, true);
	assert.match(sc.status, /Connected to SprigCast on port 4456/);
	assert.equal(sc.isOnAir("BRB"), true);
	assert.equal(sc.state.stream, true);
	assert.equal(sc.state.clips, true);
	assert.equal(sc.state.media.Intro, "playing");
	assert.equal(sc.state.sprigs.Frame, "Quiet");
	assert.equal(sc.state.deaths, null); // deaths.get failed (no Death count): the rest still caught up
	socket.say({ event: "bloom.shown", bloom: "Gaming", shape: "horizontal" });
	assert.equal(sc.isOnAir("Gaming"), true);
	socket.say({ event: "sound.changed", name: "Mic", muted: true });
	assert.equal(sc.state.sounds.Mic, true);
	sc.want(false);
	assert.equal(sc.socket, null);
	assert.equal(sc.status, "Not connected");
});

test("an item in a bloom's own sprig lights a key set to the bloom", async () => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	sc.want(true);
	await welcome(FakeSocket.last);
	FakeSocket.last.say({ event: "item.changed", sprig: "Chatting 2", blooms: ["Chatting"], item: "Cam", visible: false });
	assert.equal(sc.state.items["Chatting/Cam"], false);
	assert.equal(sc.state.items["Chatting 2/Cam"], false);
	sc.want(false);
});

test("looks on 4455 then 4456 with no port set, and steps past OBS", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
	FakeSocket.all = [];
	assert.deepEqual(AUTO_PORTS, [4455, 4456]);
	const sc = new SprigCast({ WebSocketClass: FakeSocket });
	sc.want(true);
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4455/sprigcast");
	FakeSocket.last.say({ op: 0, d: { obsWebSocketVersion: "5.5.0" } }); // OBS holds 4455
	assert.equal(FakeSocket.all[0].closed, true);
	t.mock.timers.tick(250);
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4456/sprigcast");
	FakeSocket.last.emit("close", 1006, ""); // nothing there yet
	assert.match(sc.status, /4455 or 4456/);
	t.mock.timers.tick(1000);
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4455/sprigcast"); // and round again
	FakeSocket.last.emit("close", 1006, "");
	t.mock.timers.tick(100); // each port once a round, without waiting between them
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4456/sprigcast");
	sc.want(false);
});

test("SprigCast restarting: keys go dark, then it looks again within seconds", async (t) => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	sc.want(true);
	await welcome(FakeSocket.last);
	assert.equal(sc.state.stream, true);
	t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
	const seen = [];
	sc.on("connection", (up) => seen.push(up));
	FakeSocket.last.say({ event: "app.closing" });
	FakeSocket.last.emit("close", 1001, "");
	assert.deepEqual(seen, [false]);
	assert.equal(sc.state.stream, false); // not a Live key still lit for a closed SprigCast
	assert.equal(sc.ready, false);
	t.mock.timers.tick(1000);
	assert.equal(FakeSocket.all.length, 2);
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4456/sprigcast"); // a set port stays put
	sc.want(false);
});

test("a wrong password says so and waits for a new one", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, password: "nope", WebSocketClass: FakeSocket });
	const refused = [];
	sc.on("refused", (why) => refused.push(why));
	sc.want(true);
	FakeSocket.last.say({ hello: { auth: { salt: "s", challenge: "c" } } });
	assert.equal(FakeSocket.last.sent[0].auth, authAnswer("nope", "s", "c"));
	FakeSocket.last.emit("close", 4009, "Wrong password.");
	assert.deepEqual(refused, ["Wrong password."]);
	assert.match(sc.status, /Wrong password/);
	sc.configure({ port: 4456, password: "leafy" }); // a new password: try at once
	assert.equal(FakeSocket.all.length, 2);
	sc.want(false);
});

test("a connection that never says hello, or stops answering pings, is dropped", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	sc.want(true);
	t.mock.timers.tick(5000); // no hello
	t.mock.timers.tick(1000);
	assert.equal(FakeSocket.all.length, 2);
	sc.want(false);
});
