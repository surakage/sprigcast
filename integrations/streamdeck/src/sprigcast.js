// A small client for the SprigCast API (ws://127.0.0.1:<port>/sprigcast). See docs/control-api.md.
//
// It connects only while some Stream Deck key needs SprigCast (want(true)), and lets go when none does,
// so an idle Stream Deck costs SprigCast nothing. Events arrive as SprigCast sends them: no polling.
// Nothing to type: while SprigCast's API runs, SprigCast keeps its port and password in api.json next to its
// settings (%LOCALAPPDATA%\Slothbloom\SprigCast), and the plugin reads them there, like OBS's plugin reads
// obs-websocket's config. It looks for that file every 2 seconds, so it connects as soon as SprigCast opens.
// A port or password typed in the key settings wins; with a password and no port it looks on 4455 and 4456 and
// steps past OBS if OBS answers.

import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import WebSocket from "ws";

const RETRY_MS = [1000, 2000, 5000, 10000]; // then every 10 s: a refused local connection costs nothing
export const AUTO_PORTS = [4455, 4456];
export const EVENTS = ["bloom", "sound", "output", "item", "plugin", "media", "filter", "transition", "garden", "path", "sprig"];
const HELLO_MS = 5000; // SprigCast says hello at once; anything slower isn't SprigCast
const PING_MS = 15000;
const WATCH_FILE_MS = 2000; // how often to look for api.json while SprigCast is closed (a file check: nothing)

export function infoFile(env = process.env) {
	const base = env.LOCALAPPDATA || (env.HOME ? join(env.HOME, "AppData", "Local") : "");
	return base ? join(base, "Slothbloom", "SprigCast", "api.json") : "";
}

/** SprigCast's api.json: {port, password} while its API runs, else null. */
export function readInfo(path) {
	try {
		const info = JSON.parse(readFileSync(path, "utf8"));
		return Number(info.port) > 0 ? { port: Number(info.port), password: String(info.password ?? "") } : null;
	} catch {
		return null;
	}
} // a quiet connection is checked this often (a sleeping PC drops it without a close)

function freshState() {
	return {
		onAir: {}, sounds: {}, volumes: {}, items: {}, media: {}, filters: {}, sprigs: {},
		places: {}, // place id -> "connecting", "live" or "reconnecting" while it's sent to (none: stopped)
		stream: false, record: false, recordPaused: false, clips: false, camera: false,
		garden: null, path: null, transition: null, deaths: null, timer: null,
	};
}

/** m:ss, or h:mm:ss from an hour on (as SprigCast writes it). */
export function clockText(seconds) {
	const s = Math.max(0, Math.floor(seconds));
	const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
	const two = (n) => String(n).padStart(2, "0");
	return h ? `${h}:${two(m)}:${two(r)}` : `${m}:${two(r)}`;
}

/** The time a timer key shows now: counted on from SprigCast's last word (a countdown down, the rest up). */
export function timerText(timer, now = Date.now()) {
	if (!timer) return "";
	if (!timer.running) return timer.text || clockText(timer.seconds);
	const gone = Math.max(0, (now - timer.at) / 1000);
	if (timer.mode === "countdown") return clockText(Math.ceil(timer.seconds - gone - 1e-6));
	return clockText(timer.seconds + gone);
}

/** Seconds a +time key adds (60 unless set; below 0 takes time off). */
export function addSeconds(settings) {
	const seconds = Number(String(settings?.seconds ?? "").trim() || 60);
	return Number.isFinite(seconds) && seconds !== 0 ? seconds : 60;
}

/** One word for a place that's sent to: SprigCast's own state, or (an older SprigCast) from reconnecting. */
export function placeState(place) {
	return place?.state && place.state !== "stopped" ? place.state : place?.reconnecting ? "reconnecting" : "live";
}

/** What a Place key shows under its picture: nothing while it's fine. */
export function placeTitle(state) {
	return { connecting: "Connecting", reconnecting: "Retrying" }[state];
}

export function authAnswer(password, salt, challenge) {
	const sha = (text) => createHash("sha256").update(text, "utf8").digest("base64");
	return sha(sha(password + salt) + challenge);
}

export class SprigCast extends EventEmitter {
	constructor({ port = 0, password = "", host = "127.0.0.1", WebSocketClass = WebSocket, info = infoFile() } = {}) {
		super();
		this.setPort = Number(port) || 0; // 0: look on AUTO_PORTS
		this.port = this.setPort || AUTO_PORTS[0];
		this.password = password; // typed in the key settings ("" = from api.json), or the key pairing gave
		this.pairCode = ""; // a code from SprigCast → Settings → Connections → Pair a device, used once
		this.info = info; // api.json's path ("" = don't look)
		this.host = host;
		this.WebSocketClass = WebSocketClass;
		this.socket = null;
		this.ready = false;
		this.wanted = false;
		this.state = freshState();
		this.status = "Not connected";
		this._nextId = 0;
		this._pending = new Map();
		this._tries = 0;
		this._timer = null;
		this._watch = null; // hello timeout, then the ping
		this._alive = true;
		this._roundStart = null; // the port a look round started on (no port set: each port once, then wait)
	}

	_say(status) {
		if (status === this.status) return;
		this.status = status;
		this.emit("status", status);
	}

	/** Connect (or stay connected) while true; close when false. */
	want(on) {
		this.wanted = on;
		if (on && !this.socket && !this._timer) this._open();
		if (!on) this.close();
	}

	/** port 0 or blank: look on AUTO_PORTS. */
	configure({ port, password, pair }) {
		const wanted = Number(port) || 0;
		const code = String(pair ?? "").replace(/\D/g, "");
		const changed = wanted !== this.setPort || (password ?? "") !== this.password || (code && code !== this.pairCode);
		this.setPort = wanted;
		if (wanted) this.port = wanted;
		this.password = password ?? "";
		this.pairCode = code;
		if (changed && this.wanted) {
			this.close();
			this._tries = 0;
			this._open();
		}
	}

	close() {
		clearTimeout(this._timer);
		this._timer = null;
		clearInterval(this._watch);
		clearTimeout(this._watch);
		this._watch = null;
		const socket = this.socket;
		this.socket = null;
		this.ready = false;
		if (socket) {
			socket.removeAllListeners();
			socket.on("error", () => {});
			socket.close();
		}
		for (const { reject } of this._pending.values()) reject(new Error("SprigCast closed"));
		this._pending.clear();
		if (!this.wanted) this._say("Not connected");
	}

	/** Run a SprigCast action: resolves with its result, rejects with its error message. */
	run(action, fields = {}) {
		if (!this.ready) return Promise.reject(new Error("SprigCast isn't connected"));
		const id = String(++this._nextId);
		return new Promise((resolve, reject) => {
			const timer = setTimeout(() => {
				if (this._pending.delete(id)) reject(new Error(`${action} took too long`));
			}, 15000);
			const done = (fn) => (value) => {
				clearTimeout(timer);
				fn(value);
			};
			this._pending.set(id, { resolve: done(resolve), reject: done(reject) });
			this.socket.send(JSON.stringify({ id, do: action, ...fields }));
		});
	}

	_open() {
		this._timer = null;
		if (!this.wanted) return;
		this._found = this.setPort || this.password || !this.info ? null : readInfo(this.info);
		if (this._found && !this.setPort) this.port = this._found.port;
		if (this.info && !this.setPort && !this.password && !this._found) {
			this._say("Waiting for SprigCast to open");
			this._timer = setTimeout(() => this._open(), WATCH_FILE_MS);
			return;
		}
		if (this._roundStart === null) this._roundStart = this.port;
		const socket = (this.socket = new this.WebSocketClass(`ws://${this.host}:${this.port}/sprigcast`));
		socket.on("message", (data) => this._received(String(data)));
		socket.on("close", (code, reason) => this._lost(code, String(reason ?? "")));
		socket.on("error", () => {}); // "close" follows
		socket.on("pong", () => { this._alive = true; });
		this._watch = setTimeout(() => this._drop(socket), HELLO_MS);
	}

	/** Give up on a socket that went quiet (or never said hello): "close" follows and we look again. */
	_drop(socket) {
		if (socket !== this.socket) return;
		if (typeof socket.terminate === "function") socket.terminate();
		else this._lost(1006, "");
	}

	_nextPort() {
		if (this.setPort) return;
		this.port = AUTO_PORTS[(AUTO_PORTS.indexOf(this.port) + 1) % AUTO_PORTS.length];
	}

	_lost(code = 0, reason = "") {
		if (!this.socket) return; // already let go
		const was = this.ready;
		clearInterval(this._watch);
		clearTimeout(this._watch);
		this._watch = null;
		let wait = null;
		if (code === 4011) {
			this.pairCode = ""; // used up or ran out: a new one is needed
			this.emit("pairFailed");
			this._say("That pairing code didn't work: press Pair a device in SprigCast for a new one");
		} else if (code === 4009) {
			this.emit("refused", reason || "Wrong password.");
			this._say("Wrong password: check it in SprigCast → Settings → Connections");
			this._tries = RETRY_MS.length - 1; // wait long: it won't work until the password changes
		} else if (code === 4900) {
			wait = 200; // OBS answered on this port: try the next one now
		} else {
			if (!was && !this.setPort && !this._found) {
				this._nextPort();
				if (this.port !== this._roundStart) wait = 100; // the other port at once; a full round, then wait
			}
			this._say(this.setPort ? `Looking for SprigCast on port ${this.setPort}` : "Looking for SprigCast on port 4455 or 4456");
		}
		this.socket = null;
		this.ready = false;
		this.state = freshState(); // dark keys rather than keys that say something old
		for (const { reject } of this._pending.values()) reject(new Error("SprigCast closed"));
		this._pending.clear();
		if (was) this.emit("connection", false);
		if (!this.wanted) return;
		if (wait === null) {
			wait = RETRY_MS[Math.min(this._tries++, RETRY_MS.length - 1)];
			this._roundStart = null;
		}
		this._timer = setTimeout(() => this._open(), wait);
	}

	_watchAlive(socket) {
		clearTimeout(this._watch);
		if (typeof socket.ping !== "function") return;
		this._alive = true;
		this._watch = setInterval(() => {
			if (!this._alive) return this._drop(socket);
			this._alive = false;
			try {
				socket.ping();
			} catch {
				this._drop(socket);
			}
		}, PING_MS);
	}

	_received(text) {
		let message;
		try {
			message = JSON.parse(text);
		} catch {
			return;
		}
		if (message.op !== undefined && !this.ready) {
			// OBS (obs-websocket) holds this port: SprigCast isn't here
			this._nextPort();
			const socket = this.socket;
			this._lost(4900, "OBS");
			socket.removeAllListeners();
			socket.on("error", () => {});
			socket.close();
		} else if (message.hello && this.pairCode) {
			this.socket.send(JSON.stringify({ pair: this.pairCode, name: "Stream Deck", events: EVENTS }));
		} else if (message.paired) {
			this.password = String(message.paired.key ?? ""); // this device's own key from now on
			this.pairCode = "";
			this.emit("paired", this.password);
		} else if (message.hello) {
			const auth = message.hello.auth;
			const password = this.password || this._found?.password || "";
			const answer = auth ? authAnswer(password, auth.salt, auth.challenge) : null;
			this.socket.send(JSON.stringify({ auth: answer, name: "Stream Deck", events: EVENTS })); // name: for SprigCast's log
		} else if (message.welcome) {
			this.ready = true;
			this._tries = 0;
			this._roundStart = null;
			this._watchAlive(this.socket);
			this._say(`Connected to SprigCast on port ${this.port}`);
			this._catchUp().finally(() => this.emit("connection", true));
		} else if (message.id !== undefined && this._pending.has(String(message.id))) {
			const { resolve, reject } = this._pending.get(String(message.id));
			this._pending.delete(String(message.id));
			if (message.ok) resolve(message.result ?? {});
			else reject(new Error(message.error?.message ?? "SprigCast said no"));
		} else if (message.event) {
			this._event(message);
		}
	}

	async _catchUp() {
		// Each step on its own: one that fails (an older SprigCast, a plugin not added) leaves the rest.
		const st = this.state;
		const ask = (action) => this.run(action).catch(() => null);
		const [current, sounds, stream, record, clips, camera, gardens, paths, transition, media, sprigs, deaths, timer] =
			await Promise.all(["bloom.current", "sounds.list", "stream.status", "record.status", "clips.status",
				"camera.status", "gardens.list", "paths.list", "transition.get", "media.list", "sprigs.list", "deaths.get", "timer.get"].map(ask));
		for (const [shape, bloom] of Object.entries(current?.onAir ?? {})) st.onAir[shape] = bloom?.name ?? null;
		for (const s of sounds?.sounds ?? []) {
			st.sounds[s.name] = s.muted;
			st.volumes[s.name] = s.volumeDb;
		}
		st.stream = Boolean(stream?.on);
		this.setPlaces(stream?.places);
		st.record = Boolean(record?.on);
		st.recordPaused = Boolean(record?.on && record?.paused);
		st.clips = Boolean(clips?.on);
		st.camera = Boolean(camera?.on);
		st.garden = gardens?.garden ?? null;
		st.path = paths?.path ?? null;
		st.transition = transition;
		for (const m of media?.media ?? []) st.media[m.media] = m.state;
		for (const p of sprigs?.sprigs ?? []) st.sprigs[p.name] = p.state ?? null;
		st.deaths = typeof deaths?.count === "number" ? deaths.count : null;
		this.setTimer(timer);
	}

	/** Each place as SprigCast last listed them (stream.status, place.start, place.stop): only those sent to now. */
	setPlaces(places) {
		if (!Array.isArray(places)) return;
		this.state.places = {};
		for (const p of places) {
			if (p?.on) this.state.places[p.id] = placeState(p);
		}
	}

	/** The Stream timer as SprigCast last said (timer.* answer or timer.changed), stamped so keys count on from it. */
	setTimer(answer) {
		this.state.timer = typeof answer?.seconds === "number"
			? { mode: answer.mode, seconds: answer.seconds, running: Boolean(answer.running), done: Boolean(answer.done),
				text: String(answer.text ?? ""), at: Date.now() }
			: null;
	}

	/** Ask where the timer is again (a countdown ended, or going live started or stopped the uptime). */
	_askTimer() {
		this.run("timer.get").then((answer) => this.setTimer(answer), () => { this.state.timer = null; })
			.finally(() => this.emit("changed"));
	}

	_event(message) {
		const { event } = message;
		const st = this.state;
		if (event === "bloom.shown") st.onAir[message.shape] = message.bloom;
		else if (event === "sound.changed") {
			st.sounds[message.name] = message.muted;
			st.volumes[message.name] = message.volumeDb;
		} else if (event === "stream.changed") {
			st.stream = Boolean(message.on);
			if (!st.stream) st.places = {};
			else this.run("stream.status").then((status) => this.setPlaces(status?.places), () => {}).finally(() => this.emit("changed"));
			if (st.timer?.mode === "uptime") this._askTimer();
		} else if (event === "place.changed") {
			if (message.state === "stopped") delete st.places[message.place];
			else st.places[message.place] = message.state;
		}
		else if (event === "record.changed") {
			st.record = Boolean(message.on);
			if (!st.record) st.recordPaused = false;
		} else if (event === "record.paused") st.recordPaused = Boolean(message.paused);
		else if (event === "clips.changed") st.clips = Boolean(message.on);
		else if (event === "camera.changed") st.camera = Boolean(message.on);
		else if (event === "garden.changed") st.garden = message.garden;
		else if (event === "path.changed") st.path = message.path;
		else if (event === "transition.changed") st.transition = { kind: message.kind, ms: message.ms };
		else if (event === "media.changed") st.media[message.media] = message.state;
		else if (event === "filter.changed") st.filters[`${message.source}/${message.name}`] = message.on;
		else if (event === "deaths.changed") st.deaths = message.count;
		else if (event === "timer.changed") this.setTimer(message);
		else if (event === "timer.done") this._askTimer();
		else if (event === "item.changed") {
			// In a bloom, or in a sprig (and the blooms whose own sprig it is): a key names either
			for (const holder of [message.bloom, message.sprig, ...(message.blooms ?? [])]) {
				if (holder) st.items[`${holder}/${message.item}`] = message.visible;
			}
		} else if (event === "sprig.state") st.sprigs[message.sprig] = message.state ?? null;
		else if (event === "app.closing") this._tries = 0; // look again soon: it's likely a restart
		this.emit("event", message);
		this.emit("changed");
	}

	isOnAir(bloom) {
		return Object.values(this.state.onAir).some((name) => name === bloom);
	}
}
