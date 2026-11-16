// A small client for the SprigCast API (ws://127.0.0.1:<port>/sprigcast). See docs/control-api.md.
//
// It connects only while some Stream Deck key needs SprigCast (want(true)), and lets go when none does,
// so an idle Stream Deck costs SprigCast nothing. Events arrive as SprigCast sends them: no polling.
// With no port set it looks on 4455 and 4456 (SprigCast's usual port, and the one people pick next to OBS),
// and steps past OBS if OBS answers. When SprigCast closes or restarts it looks again within a few seconds.

import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import WebSocket from "ws";

const RETRY_MS = [1000, 2000, 5000, 10000]; // then every 10 s: a refused local connection costs nothing
export const AUTO_PORTS = [4455, 4456];
export const EVENTS = ["bloom", "sound", "output", "item", "plugin", "media", "filter", "transition", "garden", "path", "sprig"];
const HELLO_MS = 5000; // SprigCast says hello at once; anything slower isn't SprigCast
const PING_MS = 15000; // a quiet connection is checked this often (a sleeping PC drops it without a close)

function freshState() {
	return {
		onAir: {}, sounds: {}, volumes: {}, items: {}, media: {}, filters: {}, sprigs: {},
		stream: false, record: false, clips: false, camera: false,
		garden: null, path: null, transition: null, deaths: null,
	};
}

export function authAnswer(password, salt, challenge) {
	const sha = (text) => createHash("sha256").update(text, "utf8").digest("base64");
	return sha(sha(password + salt) + challenge);
}

export class SprigCast extends EventEmitter {
	constructor({ port = 0, password = "", host = "127.0.0.1", WebSocketClass = WebSocket } = {}) {
		super();
		this.setPort = Number(port) || 0; // 0: look on AUTO_PORTS
		this.port = this.setPort || AUTO_PORTS[0];
		this.password = password;
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
	configure({ port, password }) {
		const wanted = Number(port) || 0;
		const changed = wanted !== this.setPort || (password ?? "") !== this.password;
		this.setPort = wanted;
		if (wanted) this.port = wanted;
		this.password = password ?? "";
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
		if (code === 4009) {
			this.emit("refused", reason || "Wrong password.");
			this._say("Wrong password: check it in SprigCast → Settings → Connections");
			this._tries = RETRY_MS.length - 1; // wait long: it won't work until the password changes
		} else if (code === 4900) {
			wait = 200; // OBS answered on this port: try the next one now
		} else {
			if (!was && !this.setPort) {
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
		} else if (message.hello) {
			const auth = message.hello.auth;
			const answer = auth ? authAnswer(this.password, auth.salt, auth.challenge) : null;
			this.socket.send(JSON.stringify({ auth: answer, events: EVENTS }));
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
		const [current, sounds, stream, record, clips, camera, gardens, paths, transition, media, sprigs, deaths] =
			await Promise.all(["bloom.current", "sounds.list", "stream.status", "record.status", "clips.status",
				"camera.status", "gardens.list", "paths.list", "transition.get", "media.list", "sprigs.list", "deaths.get"].map(ask));
		for (const [shape, bloom] of Object.entries(current?.onAir ?? {})) st.onAir[shape] = bloom?.name ?? null;
		for (const s of sounds?.sounds ?? []) {
			st.sounds[s.name] = s.muted;
			st.volumes[s.name] = s.volumeDb;
		}
		st.stream = Boolean(stream?.on);
		st.record = Boolean(record?.on);
		st.clips = Boolean(clips?.on);
		st.camera = Boolean(camera?.on);
		st.garden = gardens?.garden ?? null;
		st.path = paths?.path ?? null;
		st.transition = transition;
		for (const m of media?.media ?? []) st.media[m.media] = m.state;
		for (const p of sprigs?.sprigs ?? []) st.sprigs[p.name] = p.state ?? null;
		st.deaths = typeof deaths?.count === "number" ? deaths.count : null;
	}

	_event(message) {
		const { event } = message;
		const st = this.state;
		if (event === "bloom.shown") st.onAir[message.shape] = message.bloom;
		else if (event === "sound.changed") {
			st.sounds[message.name] = message.muted;
			st.volumes[message.name] = message.volumeDb;
		} else if (event === "stream.changed") st.stream = Boolean(message.on);
		else if (event === "record.changed") st.record = Boolean(message.on);
		else if (event === "clips.changed") st.clips = Boolean(message.on);
		else if (event === "camera.changed") st.camera = Boolean(message.on);
		else if (event === "garden.changed") st.garden = message.garden;
		else if (event === "path.changed") st.path = message.path;
		else if (event === "transition.changed") st.transition = { kind: message.kind, ms: message.ms };
		else if (event === "media.changed") st.media[message.media] = message.state;
		else if (event === "filter.changed") st.filters[`${message.source}/${message.name}`] = message.on;
		else if (event === "deaths.changed") st.deaths = message.count;
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
