// A small client for the SprigCast API (ws://127.0.0.1:<port>/sprigcast). See docs/control-api.md.
//
// It connects only while some Stream Deck key needs SprigCast (want(true)), and lets go when none does,
// so an idle Stream Deck costs SprigCast nothing. Events arrive as SprigCast sends them: no polling.

import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import WebSocket from "ws";

const RETRY_MS = [2000, 5000, 10000, 30000];

export function authAnswer(password, salt, challenge) {
	const sha = (text) => createHash("sha256").update(text, "utf8").digest("base64");
	return sha(sha(password + salt) + challenge);
}

export class SprigCast extends EventEmitter {
	constructor({ port = 4455, password = "", host = "127.0.0.1", WebSocketClass = WebSocket } = {}) {
		super();
		this.port = port;
		this.password = password;
		this.host = host;
		this.WebSocketClass = WebSocketClass;
		this.socket = null;
		this.ready = false;
		this.wanted = false;
		this.state = {
			onAir: {}, sounds: {}, volumes: {}, items: {}, media: {}, filters: {},
			stream: false, record: false, clips: false, camera: false,
			garden: null, path: null, transition: null, deaths: null,
		};
		this._nextId = 0;
		this._pending = new Map();
		this._tries = 0;
		this._timer = null;
	}

	/** Connect (or stay connected) while true; close when false. */
	want(on) {
		this.wanted = on;
		if (on && !this.socket && !this._timer) this._open();
		if (!on) this.close();
	}

	configure({ port, password }) {
		const changed = (port && Number(port) !== this.port) || (password ?? "") !== this.password;
		if (port) this.port = Number(port);
		this.password = password ?? "";
		if (changed && this.wanted) {
			this.close();
			this._open();
		}
	}

	close() {
		clearTimeout(this._timer);
		this._timer = null;
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
	}

	/** Run a SprigCast action: resolves with its result, rejects with its error message. */
	run(action, fields = {}) {
		if (!this.ready) return Promise.reject(new Error("SprigCast isn't connected"));
		const id = String(++this._nextId);
		return new Promise((resolve, reject) => {
			this._pending.set(id, { resolve, reject });
			this.socket.send(JSON.stringify({ id, do: action, ...fields }));
			setTimeout(() => {
				if (this._pending.delete(id)) reject(new Error(`${action} took too long`));
			}, 15000);
		});
	}

	_open() {
		this._timer = null;
		if (!this.wanted) return;
		const socket = (this.socket = new this.WebSocketClass(`ws://${this.host}:${this.port}/sprigcast`));
		socket.on("message", (data) => this._received(String(data)));
		socket.on("close", (code, reason) => this._lost(code, String(reason ?? "")));
		socket.on("error", () => {}); // "close" follows
	}

	_lost(code = 0, reason = "") {
		const was = this.ready;
		if (code === 4009) {
			this.emit("refused", reason || "Wrong password.");
			this._tries = RETRY_MS.length - 1; // wait long: it won't work until the password changes
		}
		this.socket = null;
		this.ready = false;
		for (const { reject } of this._pending.values()) reject(new Error("SprigCast closed"));
		this._pending.clear();
		if (was) this.emit("connection", false);
		if (!this.wanted) return;
		const wait = RETRY_MS[Math.min(this._tries++, RETRY_MS.length - 1)];
		this._timer = setTimeout(() => this._open(), wait);
	}

	_received(text) {
		let message;
		try {
			message = JSON.parse(text);
		} catch {
			return;
		}
		if (message.hello) {
			const auth = message.hello.auth;
			const answer = auth ? authAnswer(this.password, auth.salt, auth.challenge) : null;
			this.socket.send(JSON.stringify({ auth: answer, events: ["bloom", "sound", "output", "item", "plugin", "media", "filter", "transition", "garden", "path"] }));
		} else if (message.welcome) {
			this.ready = true;
			this._tries = 0;
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
		try {
			const current = await this.run("bloom.current");
			for (const [shape, bloom] of Object.entries(current.onAir ?? {})) this.state.onAir[shape] = bloom?.name ?? null;
			const sounds = await this.run("sounds.list");
			for (const s of sounds.sounds ?? []) {
				this.state.sounds[s.name] = s.muted;
				this.state.volumes[s.name] = s.volumeDb;
			}
			this.state.stream = Boolean((await this.run("stream.status")).on);
			this.state.record = Boolean((await this.run("record.status")).on);
			this.state.clips = Boolean((await this.run("clips.status")).on);
			this.state.camera = Boolean((await this.run("camera.status").catch(() => ({}))).on);
			this.state.garden = (await this.run("gardens.list")).garden ?? null;
			this.state.path = (await this.run("paths.list")).path ?? null;
			this.state.transition = await this.run("transition.get");
			for (const m of (await this.run("media.list")).media ?? []) this.state.media[m.media] = m.state;
		} catch {
			// a half-ready SprigCast: events fill the state in as things change
		}
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
		else if (event === "item.changed" && message.bloom) st.items[`${message.bloom}/${message.item}`] = message.visible;
		this.emit("event", message);
		this.emit("changed");
	}

	isOnAir(bloom) {
		return Object.values(this.state.onAir).some((name) => name === bloom);
	}
}
