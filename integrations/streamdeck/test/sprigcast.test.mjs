import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { AUTO_PORTS, EVENTS, SprigCast, addSeconds, authAnswer, clockText, infoFile, placeState, placeTitle, timerText } from "../src/sprigcast.js";

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
	assert.deepEqual(socket.sent[0], { auth: null, name: "Stream Deck", events: EVENTS });
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
	sc.state.items["Chatting/Cam|both"] = true; // a both-shapes key's answer from before
	FakeSocket.last.say({ event: "item.changed", sprig: "Chatting 2", blooms: ["Chatting"], item: "Cam", shape: "vertical", visible: false });
	assert.equal(sc.state.items["Chatting/Cam|vertical"], false);
	assert.equal(sc.state.items["Chatting 2/Cam|vertical"], false);
	assert.equal(sc.state.items["Chatting/Cam|both"], undefined); // asks again: still shown horizontally?
	sc.want(false);
});

test("looks on 4455 then 4456 with no port set, and steps past OBS", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
	FakeSocket.all = [];
	assert.deepEqual(AUTO_PORTS, [4455, 4456]);
	const sc = new SprigCast({ WebSocketClass: FakeSocket, info: "" });
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

test("connects by itself from SprigCast's api.json, as soon as SprigCast opens", async (t) => {
	t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
	FakeSocket.all = [];
	const dir = mkdtempSync(join(tmpdir(), "sprigcast-"));
	const info = join(dir, "api.json");
	assert.equal(infoFile({ LOCALAPPDATA: "C:\\Users\\me\\AppData\\Local" }).replace(/\\/g, "/"),
		"C:/Users/me/AppData/Local/Slothbloom/SprigCast/api.json");
	const sc = new SprigCast({ WebSocketClass: FakeSocket, info });
	sc.want(true);
	assert.equal(FakeSocket.all.length, 0); // SprigCast closed: no connection tried, just a look for the file
	assert.equal(sc.status, "Waiting for SprigCast to open");
	writeFileSync(info, JSON.stringify({ app: "SprigCast", port: 4457, path: "/sprigcast", password: "leafy" }));
	t.mock.timers.tick(2000);
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4457/sprigcast");
	FakeSocket.last.say({ hello: { auth: { salt: "s", challenge: "c" } } });
	assert.equal(FakeSocket.last.sent[0].auth, authAnswer("leafy", "s", "c"));
	sc.want(false);
	rmSync(dir, { recursive: true, force: true });
});

test("a password typed in the key settings wins over api.json", async () => {
	FakeSocket.all = [];
	const dir = mkdtempSync(join(tmpdir(), "sprigcast-"));
	const info = join(dir, "api.json");
	writeFileSync(info, JSON.stringify({ port: 4457, password: "leafy" }));
	const sc = new SprigCast({ WebSocketClass: FakeSocket, info, port: 4456, password: "typed" });
	sc.want(true);
	assert.equal(FakeSocket.last.url, "ws://127.0.0.1:4456/sprigcast");
	FakeSocket.last.say({ hello: { auth: { salt: "s", challenge: "c" } } });
	assert.equal(FakeSocket.last.sent[0].auth, authAnswer("typed", "s", "c"));
	sc.want(false);
	rmSync(dir, { recursive: true, force: true });
});

test("every key style has a picture for every key state", async () => {
	const { readFileSync, existsSync } = await import("node:fs");
	const root = new URL("../com.slothbloom.sprigcast.sdPlugin/", import.meta.url);
	const manifest = JSON.parse(readFileSync(new URL("manifest.json", root), "utf8"));
	const plugin = readFileSync(new URL("../src/plugin.js", import.meta.url), "utf8");
	const styles = [...plugin.match(/STYLES = new Set\(\[([^\]]+)\]/)[1].matchAll(/"(\w+)"/g)].map((m) => m[1]);
	assert.deepEqual(styles, ["canopy", "colour", "graphite"]);
	for (const action of manifest.Actions) {
		const suffix = action.UUID.split(".").pop();
		const states = Number(plugin.match(new RegExp(`super\\("${suffix}"(?:, (\\d))?\\)`))?.[1] ?? 2);
		assert.equal(states, action.States.length, `${suffix} states`);
		for (const style of styles.slice(1)) {
			for (let n = 0; n < states; n++) {
				assert.ok(existsSync(new URL(`imgs/styles/${style}/${suffix}/key${n}@2x.png`, root)), `${style}/${suffix}/key${n}`);
			}
		}
	}
});

test("a pairing code is sent once and the key it gets becomes the password", async () => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket, info: "" });
	let paired = null;
	sc.on("paired", (key) => (paired = key));
	sc.configure({ port: 4456, pair: "123 456" });
	sc.want(true);
	const socket = FakeSocket.last;
	socket.say({ hello: { auth: { salt: "s", challenge: "c" } } });
	assert.deepEqual(socket.sent[0], { pair: "123456", name: "Stream Deck", events: EVENTS });
	socket.say({ paired: { key: "sc_abc", name: "Stream Deck" } });
	assert.equal(paired, "sc_abc");
	assert.equal(sc.password, "sc_abc");
	assert.equal(sc.pairCode, "");
	sc.want(false);

	// A wrong or old code: dropped, and the line says what to do.
	FakeSocket.all = [];
	const again = new SprigCast({ port: 4456, WebSocketClass: FakeSocket, info: "" });
	let failed = false;
	again.on("pairFailed", () => (failed = true));
	again.configure({ port: 4456, pair: "999999" });
	again.want(true);
	FakeSocket.last.emit("close", 4011, Buffer.from("Wrong or expired pairing code."));
	assert.equal(failed, true);
	assert.equal(again.pairCode, "");
	assert.match(again.status, /Pair a device/);
	again.want(false);
});

test("every key has a dark offline picture for when SprigCast is closed", async () => {
	const { readFileSync, existsSync } = await import("node:fs");
	const root = new URL("../com.slothbloom.sprigcast.sdPlugin/", import.meta.url);
	const manifest = JSON.parse(readFileSync(new URL("manifest.json", root), "utf8"));
	for (const action of manifest.Actions) {
		const suffix = action.UUID.split(".").pop();
		assert.ok(existsSync(new URL(`imgs/offline/${suffix}.png`, root)), suffix);
	}
});

test("the timer key's time: as SprigCast writes it, counted on between its words", () => {
	assert.equal(clockText(0), "0:00");
	assert.equal(clockText(65.9), "1:05");
	assert.equal(clockText(3723), "1:02:03");
	assert.equal(timerText(null), "");
	const at = 1_000_000;
	const down = { mode: "countdown", seconds: 300, running: true, done: false, text: "5:00", at };
	assert.equal(timerText(down, at), "5:00");
	assert.equal(timerText(down, at + 400), "5:00"); // 4:59.6 still shows 5:00, as SprigCast's own
	assert.equal(timerText(down, at + 1000), "4:59");
	assert.equal(timerText(down, at + 400_000), "0:00"); // never below 0 while timer.done is on its way
	const up = { mode: "stopwatch", seconds: 59.5, running: true, done: false, text: "0:59", at };
	assert.equal(timerText(up, at + 600), "1:00");
	assert.equal(timerText({ ...up, mode: "uptime" }, at + 3_600_000), "1:00:59");
	assert.equal(timerText({ ...down, running: false, done: true, text: "Starting!" }, at + 99_000), "Starting!"); // Done text
	assert.equal(timerText({ ...down, running: false, text: "" }, at + 99_000), "5:00");
});

test("the add-time key adds a minute unless set", () => {
	assert.equal(addSeconds({}), 60);
	assert.equal(addSeconds({ seconds: "" }), 60);
	assert.equal(addSeconds({ seconds: "30" }), 30);
	assert.equal(addSeconds({ seconds: " -15 " }), -15);
	assert.equal(addSeconds({ seconds: "lots" }), 60);
	assert.equal(addSeconds({ seconds: "0" }), 60);
});

test("tracks the stream timer: catch-up, timer.changed, and asks again when a countdown ends", async () => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	sc.want(true);
	const socket = FakeSocket.last;
	ANSWERS["timer.get"] = { source: "Timer", mode: "countdown", seconds: 300, running: false, done: false, text: "5:00" };
	try {
		await welcome(socket);
	} finally {
		delete ANSWERS["timer.get"];
	}
	assert.equal(sc.state.timer.text, "5:00");
	assert.equal(sc.state.timer.running, false);
	socket.say({ event: "timer.changed", source: "Timer", mode: "countdown", seconds: 240, running: true, done: false, text: "4:00" });
	assert.equal(sc.state.timer.seconds, 240);
	assert.equal(sc.state.timer.running, true);
	assert.ok(Math.abs(sc.state.timer.at - Date.now()) < 1000);
	const before = socket.sent.length;
	socket.say({ event: "timer.done", source: "Timer" });
	const ask = socket.sent.slice(before).find((m) => m.do === "timer.get");
	assert.ok(ask, "asked where the timer is");
	socket.say({ id: ask.id, ok: true, result: { source: "Timer", mode: "countdown", seconds: 0, running: false, done: true, text: "Live!" } });
	await tick();
	assert.equal(sc.state.timer.done, true);
	assert.equal(timerText(sc.state.timer), "Live!");
	sc.want(false);
});

test("no Stream timer added: the timer is unknown, and the rest still caught up", async () => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	sc.want(true);
	await welcome(FakeSocket.last);
	assert.equal(sc.state.timer, null);
	assert.equal(sc.state.stream, true);
	sc.want(false);
});

test("tracks a paused recording for the Pause recording key", async () => {
	FakeSocket.all = [];
	const sc = new SprigCast({ port: 4456, WebSocketClass: FakeSocket });
	sc.want(true);
	const socket = FakeSocket.last;
	ANSWERS["record.status"] = { on: true, seconds: 12, paused: true };
	try {
		await welcome(socket);
	} finally {
		ANSWERS["record.status"] = { on: false };
	}
	assert.equal(sc.state.record, true);
	assert.equal(sc.state.recordPaused, true);
	socket.say({ event: "record.paused", paused: false });
	assert.equal(sc.state.recordPaused, false);
	socket.say({ event: "record.paused", paused: true });
	socket.say({ event: "record.changed", on: false });
	assert.equal(sc.state.recordPaused, false); // stopped: not paused any more
	sc.want(false);
});

test("tracks each place for the Place key: catch-up, place.changed, and none once the stream ends", async () => {
	FakeSocket.all = [];
	const saved = ANSWERS["stream.status"];
	ANSWERS["stream.status"] = { on: true, places: [
		{ id: "out-1", on: true, state: "live" },
		{ id: "out-2", on: false, state: "stopped" },
		{ id: "out-3", on: true, reconnecting: true }, // an older SprigCast: no state
	] };
	const client = new SprigCast({ WebSocketClass: FakeSocket, info: "" });
	client.configure({ port: 4455, password: "x" });
	client.want(true);
	const socket = FakeSocket.last;
	await welcome(socket);
	ANSWERS["stream.status"] = saved;
	assert.equal(socket.sent.find((m) => "auth" in m).name, "Stream Deck"); // SprigCast's log names it
	assert.deepEqual(client.state.places, { "out-1": "live", "out-3": "reconnecting" });
	socket.say({ event: "place.changed", place: "out-1", state: "reconnecting", was: "live", reconnects: 1, dropped: true });
	assert.equal(client.state.places["out-1"], "reconnecting");
	assert.equal(placeTitle(client.state.places["out-1"]), "Retrying");
	socket.say({ event: "place.changed", place: "out-1", state: "live", was: "reconnecting", reconnects: 1 });
	assert.equal(placeTitle(client.state.places["out-1"]), undefined);
	socket.say({ event: "place.changed", place: "out-3", state: "stopped", was: "reconnecting" });
	assert.deepEqual(client.state.places, { "out-1": "live" });
	client.setPlaces([{ id: "out-2", on: true, state: "connecting" }, { id: "out-1", on: false }]); // a place.start answer
	assert.deepEqual(client.state.places, { "out-2": "connecting" });
	assert.equal(placeState({ on: true }), "live");
	socket.say({ event: "stream.changed", on: false });
	assert.deepEqual(client.state.places, {});
	client.want(false);
});
