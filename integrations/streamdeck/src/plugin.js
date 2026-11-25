// SprigCast for Stream Deck. Each key is a SingletonAction (manifest UUIDs below); they share one
// connection to SprigCast, opened only while a SprigCast key is on screen.

import streamDeck, { SingletonAction } from "@elgato/streamdeck";

import { SprigCast, addSeconds, clockText, placeTitle, timerText } from "./sprigcast.js";
import { NoPageError, TELEPROMPTER_TITLES, teleprompterCommand, teleprompterPort, teleprompterSend } from "./teleprompter.js";

const ID = "com.slothbloom.sprigcast";
const sprigcast = new SprigCast();
const visible = new Set(); // action contexts on screen
const all = [];

let keyStyle = "canopy"; // Key style in the key settings: canopy (the manifest's own pictures), colour or graphite
const STYLES = new Set(["canopy", "colour", "graphite"]);
let settingsLoaded = false; // connect only once the password is known (else SprigCast refuses the first tries)

function refreshConnection() {
	sprigcast.want(settingsLoaded && visible.size > 0);
}

let globals = {};

function useGlobalSettings(settings = {}) {
	globals = settings;
	sprigcast.configure({ port: Number(settings.port) || 0, password: settings.password ?? "", pair: settings.pair ?? "" });
	settingsLoaded = true;
	refreshConnection();
	const style = STYLES.has(settings.style) ? settings.style : "canopy";
	if (style !== keyStyle) {
		keyStyle = style;
		for (const kind of all) for (const action of kind.actions) kind.dress(action).catch(() => {});
	}
}

async function loadGlobalSettings() {
	useGlobalSettings(await streamDeck.settings.getGlobalSettings());
}

streamDeck.settings.onDidReceiveGlobalSettings((ev) => useGlobalSettings(ev.settings ?? {}));

const TITLED = new Set(["getTransitions"]); // SprigCast's own words (cut, fade...): shown as SprigCast shows them

/** Lists for the property inspector's pickers (sdpi-select datasource). */
async function listFor(event, payload = {}) {
	const lists = {
		getGardens: async () => (await sprigcast.run("gardens.list")).gardens,
		getPaths: async () => (await sprigcast.run("paths.list")).paths,
		getMedia: async () => (await sprigcast.run("media.list")).media.map((m) => m.media),
		getFilters: async () => (await sprigcast.run("filters.list", { source: payload.source ?? "" })).filters.map((f) => f.name),
		getTransitions: async () => (await sprigcast.run("transition.get")).kinds,
		getBlooms: async () => (await sprigcast.run("blooms.list")).blooms.map((b) => b.name),
		getSounds: async () => (await sprigcast.run("sounds.list")).sounds.map((s) => s.name),
		getSprigs: async () => (await sprigcast.run("sprigs.list")).sprigs.map((s) => s.name),
		getSources: async () => (await sprigcast.run("sources.list")).sources.map((s) => s.name),
		getActions: async () => (await sprigcast.run("app.actions")).actions.map((a) => a.do),
		getExpressions: async () => (await sprigcast.run("pngtuber.expressions")).expressions,
		// A place's key keeps its id; the list shows its name ("Twitch (horizontal)").
		getPlaces: async () => (await sprigcast.run("places.list")).places
			.map((p) => ({ label: p.fullName ?? `${p.name ?? p.service ?? p.id} (${p.shape})`, value: p.id })),
		getFreezable: async () => [...await sprigNames(), // sprigs, then the things in blooms that aren't sprigs
			...(await sprigcast.run("sources.list")).sources.filter((s) => s.kind !== "scene").map((s) => s.name)],
	};
	const fill = lists[event];
	if (!fill) return null;
	try {
		const found = await fill();
		if (found.some((item) => item && typeof item === "object")) { // already label and value
			return found.sort((a, b) => String(a.label).localeCompare(String(b.label)));
		}
		const names = [...new Set(found)].sort((a, b) => String(a).localeCompare(String(b)));
		const label = TITLED.has(event) ? (n) => n.charAt(0).toUpperCase() + n.slice(1) : (n) => n;
		return names.map((name) => ({ label: label(String(name)), value: String(name) }));
	} catch {
		return [{ label: "Open SprigCast first", value: "", disabled: true }];
	}
}

async function sprigNames() {
	return (await sprigcast.run("sprigs.list")).sprigs.map((s) => s.name);
}

/** The Connection box in the key settings says whether SprigCast is found (and on which port). */
function sendStatus() {
	return streamDeck.ui.sendToPropertyInspector({ event: "status", text: sprigcast.status }).catch(() => {});
}

/** A word on the key for a moment, then the key's own title again (a tick alone is easy to miss). */
async function flash(action, text) {
	if (!text) return;
	await action.setTitle(text).catch(() => {});
	setTimeout(() => action.setTitle(undefined).catch(() => {}), 1800);
}

class SprigCastAction extends SingletonAction {
	constructor(suffix, states = 2) {
		super();
		this.suffix = suffix;
		this.states = states;
		this.manifestId = `${ID}.${suffix}`;
		all.push(this);
	}

	async onWillAppear(ev) {
		visible.add(ev.action.id);
		refreshConnection();
		if (keyStyle !== "canopy" || !sprigcast.ready) await this.dress(ev.action);
		return this.paint(ev.action, ev.payload.settings);
	}

	/** The key's pictures in the chosen Key style (canopy = the manifest's own, so the pictures reset). */
	async dress(action) {
		if (!action.isKey()) return;
		if (!sprigcast.ready) return action.setImage(`imgs/offline/${this.suffix}.png`); // SprigCast closed: dark key
		if (keyStyle === "canopy") return action.setImage();
		const image = (n) => `imgs/styles/${keyStyle}/${this.suffix}/key${n}@2x.png`;
		if (this.states === 1) return action.setImage(image(0));
		for (let n = 0; n < this.states; n++) await action.setImage(image(n), { state: n });
	}

	onWillDisappear(ev) {
		visible.delete(ev.action.id);
		refreshConnection();
	}

	onDidReceiveSettings(ev) {
		return this.paint(ev.action, ev.payload.settings);
	}

	async onSendToPlugin(ev) {
		if (ev.payload?.event === "getStatus") return sendStatus();
		const items = await listFor(ev.payload?.event, ev.payload);
		if (items) await streamDeck.ui.sendToPropertyInspector({ event: ev.payload.event, items });
	}

	async onKeyDown(ev) {
		try {
			const result = await this.press(ev.payload.settings, ev.action);
			if (this.okAfterPress) {
				await ev.action.showOk();
				await flash(ev.action, this.said(result));
			}
		} catch (error) {
			streamDeck.logger.warn(`${this.manifestId}: ${error.message}`);
			await ev.action.showAlert();
			if (this.okAfterPress) await flash(ev.action, sprigcast.ready ? "Didn't\nwork" : "Not\nconnected");
		}
	}

	/** The word a one-shot key shows for a moment after it worked. */
	said() {
		return "Done";
	}

	/** Light or title the key from SprigCast's state (state 1 = on). */
	async paint(_action, _settings) {}

	async repaintAll() {
		for (const action of this.actions) {
			await this.paint(action, await action.getSettings());
		}
	}
}

class ShowBloom extends SprigCastAction {
	constructor() { super("bloom"); }
	press(s) { return sprigcast.run("bloom.show", { bloom: s.bloom, ...(s.shape ? { shape: s.shape } : {}) }); }
	paint(action, s) { return action.setState(s.bloom && sprigcast.isOnAir(s.bloom) ? 1 : 0); }
}

class MuteSound extends SprigCastAction {
	constructor() { super("mute"); }
	press(s) {
		const muted = { mute: true, unmute: false }[s.mode];
		return sprigcast.run("sound.mute", { sound: s.sound, ...(muted === undefined ? {} : { muted }) });
	}
	paint(action, s) { return action.setState(sprigcast.state.sounds[s.sound] ? 1 : 0); }
}

class ToggleItem extends SprigCastAction {
	constructor() { super("item"); }
	press(s, action) {
		const verb = { show: "item.show", hide: "item.hide" }[s.mode] ?? "item.toggle";
		return sprigcast.run(verb, { bloom: s.bloom, item: s.item }).then((result) => {
			if (typeof result?.visible === "boolean") {
				sprigcast.state.items[`${s.bloom}/${s.item}`] = result.visible;
				return action.setState(result.visible ? 0 : 1);
			}
		});
	}
	async paint(action, s) {
		const key = `${s.bloom}/${s.item}`;
		if (sprigcast.state.items[key] === undefined && sprigcast.ready && s.bloom && s.item) {
			try {
				sprigcast.state.items[key] = (await sprigcast.run("item.status", { bloom: s.bloom, item: s.item })).visible;
			} catch {
				return undefined;
			}
		}
		const shown = sprigcast.state.items[key];
		return typeof shown === "boolean" ? action.setState(shown ? 0 : 1) : undefined;
	}
}

class SoundVolume extends SprigCastAction {
	// A key changes the volume by a step (or sets it); on a Stream Deck + dial, turning changes it and
	// pressing mutes.
	constructor() { super("volume", 1); }
	press(s) {
		const db = Number(s.db);
		if (s.mode === "set" && Number.isFinite(db)) return sprigcast.run("sound.volume", { sound: s.sound, db });
		return sprigcast.run("sound.volume", { sound: s.sound, change: Number(s.step) || 1 });
	}
	async onDialRotate(ev) {
		const s = ev.payload.settings;
		const step = Math.abs(Number(s.step) || 1);
		try {
			await sprigcast.run("sound.volume", { sound: s.sound, change: ev.payload.ticks * step });
		} catch {
			await ev.action.showAlert();
		}
	}
	async onDialDown(ev) {
		try {
			await sprigcast.run("sound.mute", { sound: ev.payload.settings.sound });
		} catch {
			await ev.action.showAlert();
		}
	}
	paint(action, s) {
		const db = sprigcast.state.volumes[s.sound];
		if (!action.isDial() || db === undefined) return undefined;
		const muted = sprigcast.state.sounds[s.sound];
		const pct = Math.round(Math.max(0, Math.min(100, ((db + 60) / 66) * 100))); // -60 dB .. +6 dB
		return action.setFeedback({
			title: s.sound ?? "Sound",
			value: muted ? "Muted" : `${db > 0 ? "+" : ""}${Number(db).toFixed(1)} dB`,
			indicator: { value: muted ? 0 : pct },
		});
	}
}

class MediaKey extends SprigCastAction {
	constructor() { super("media"); }
	press(s) {
		const verb = { play: "media.play", pause: "media.pause", restart: "media.restart", stop: "media.stop" }[s.mode];
		return sprigcast.run(verb ?? "media.toggle", { media: s.media });
	}
	paint(action, s) { return action.setState(sprigcast.state.media[s.media] === "playing" ? 1 : 0); }
}

class FilterKey extends SprigCastAction {
	constructor() { super("filter"); }
	press(s) {
		if (s.mode === "on" || s.mode === "off") return sprigcast.run("filter.set", { source: s.source, filter: s.filter, on: s.mode === "on" });
		return sprigcast.run("filter.toggle", { source: s.source, filter: s.filter });
	}
	async paint(action, s) {
		const key = `${s.source}/${s.filter}`;
		if (sprigcast.state.filters[key] === undefined && sprigcast.ready && s.source && s.filter) {
			try {
				for (const f of (await sprigcast.run("filters.list", { source: s.source })).filters) {
					sprigcast.state.filters[`${s.source}/${f.name}`] = f.on;
				}
			} catch {
				return undefined;
			}
		}
		return action.setState(sprigcast.state.filters[key] ? 1 : 0);
	}
}

class TransitionKey extends SprigCastAction {
	constructor() { super("transition"); }
	press(s) {
		const ms = Number(s.ms);
		return sprigcast.run("transition.set", { ...(s.kind ? { kind: s.kind } : {}), ...(Number.isFinite(ms) && ms > 0 ? { ms } : {}) });
	}
	paint(action, s) {
		const now = sprigcast.state.transition;
		return action.setState(now && s.kind && now.kind === s.kind ? 1 : 0);
	}
}

class GardenKey extends SprigCastAction {
	constructor() { super("garden"); }
	press(s) { return sprigcast.run("garden.switch", { garden: s.garden }); }
	paint(action, s) { return action.setState(s.garden && sprigcast.state.garden === s.garden ? 1 : 0); }
}

class PathKey extends SprigCastAction {
	constructor() { super("path"); }
	press(s) { return sprigcast.run("path.switch", { path: s.path }); }
	paint(action, s) { return action.setState(s.path && sprigcast.state.path === s.path ? 1 : 0); }
}

class ClipsKey extends SprigCastAction {
	constructor() { super("clips"); }
	press() { return sprigcast.run("clips.set", { on: !sprigcast.state.clips }); }
	paint(action) { return action.setState(sprigcast.state.clips ? 1 : 0); }
}

class CameraKey extends SprigCastAction {
	constructor() { super("camera"); }
	press() { return sprigcast.run("camera.set"); }
	paint(action) { return action.setState(sprigcast.state.camera ? 1 : 0); }
}

class GoLive extends SprigCastAction {
	constructor() { super("live"); }
	press() { return sprigcast.run("stream.toggle"); }
	paint(action) { return action.setState(sprigcast.state.stream ? 1 : 0); }
}

class PlaceKey extends SprigCastAction {
	// One of the places the stream goes to: start or stop it while live, the others carry on.
	constructor() { super("place"); }
	async press(s) {
		if (!s.place) throw new Error("Pick a place in the key's settings");
		const sending = sprigcast.state.places[s.place] !== undefined;
		const start = s.mode === "start" || (s.mode !== "stop" && !sending);
		if (start === sending) return; // already as asked
		const result = await sprigcast.run(start ? "place.start" : "place.stop", { place: s.place });
		sprigcast.setPlaces(result?.places);
		sprigcast.emit("changed");
	}
	async paint(action, s) {
		const state = sprigcast.state.places[s.place];
		await action.setState(state === undefined ? 0 : 1);
		return action.setTitle(placeTitle(state)); // "Retrying" while it's lost its connection
	}
}

class Record extends SprigCastAction {
	constructor() { super("record"); }
	press() { return sprigcast.run("record.toggle"); }
	paint(action) { return action.setState(sprigcast.state.record ? 1 : 0); }
}

class RecordPause extends SprigCastAction {
	constructor() { super("record-pause"); }
	press(s) {
		const verb = { pause: "record.pause", resume: "record.resume" }[s.mode] ?? "record.toggle_pause";
		return sprigcast.run(verb);
	}
	paint(action) { return action.setState(sprigcast.state.recordPaused ? 1 : 0); }
}

class RecordChapter extends SprigCastAction {
	constructor() { super("chapter", 1); this.okAfterPress = true; }
	press(s) {
		const name = String(s.name ?? "").trim();
		return sprigcast.run("record.chapter", name ? { name } : {});
	}
	said() { return "Marked"; }
}

class SaveClip extends SprigCastAction {
	constructor() { super("clip", 1); this.okAfterPress = true; }
	async press() {
		const result = await sprigcast.run("clip.save");
		if (result?.clip === false && !result.started) throw new Error(result.note ?? "No clip was saved"); // an alert, not a tick
		return result;
	}
	said(result) { return result?.started ? "Clips\nstarting" : "Clip\nsaved"; } // clips were off: this press turned them on
}

class Screenshot extends SprigCastAction {
	constructor() { super("screenshot", 1); this.okAfterPress = true; }
	press() { return sprigcast.run("screenshot.take"); }
	said() { return "Saved"; }
}

class SprigStateAction extends SprigCastAction {
	constructor() { super("sprig-state", 1); this.okAfterPress = true; }
	press(s) { return sprigcast.run("sprig.state", { sprig: s.sprig, state: s.state }); }
}

class DeathCount extends SprigCastAction {
	constructor() { super("deaths", 1); }
	async press(s, action) {
		const verb = { remove: "deaths.remove", reset: "deaths.reset", show: "deaths.show" }[s.mode] ?? "deaths.add";
		const result = await sprigcast.run(verb);
		if (typeof result.count === "number") await action.setTitle(String(result.count));
	}
	paint(action) {
		if (sprigcast.state.deaths === null) return sprigcast.ready ? undefined : action.setTitle(undefined); // no old count while dark
		return action.setTitle(String(sprigcast.state.deaths));
	}
}

class TimerKey extends SprigCastAction {
	constructor() { super("timer"); this.shown = new Map(); } // action id -> the time on it (set only when it changes)
	async press(_s, action) {
		sprigcast.setTimer(await sprigcast.run("timer.toggle"));
		return this.paint(action);
	}
	async paint(action) {
		const timer = sprigcast.state.timer;
		const title = sprigcast.ready && timer ? timerText(timer) : undefined; // no old time while dark
		const state = timer?.running ? 1 : 0;
		const shown = `${state}|${title}`;
		if (this.shown.get(action.id) === shown) return;
		this.shown.set(action.id, shown);
		await action.setState(state);
		await action.setTitle(title);
	}
	onWillDisappear(ev) {
		this.shown.delete(ev.action.id);
		return super.onWillDisappear(ev);
	}
	/** Each tick while the timer goes: the time on the keys moves on its own (SprigCast only says when it changes). */
	async tick() {
		if (!sprigcast.state.timer?.running) return;
		for (const action of this.actions) await this.paint(action).catch(() => {});
	}
}

class TimerReset extends SprigCastAction {
	constructor() { super("timer-reset", 1); this.okAfterPress = true; }
	async press() { sprigcast.setTimer(await sprigcast.run("timer.reset")); }
	said() { return ""; } // the timer key shows it
}

class TimerAdd extends SprigCastAction {
	constructor() { super("timer-add", 1); }
	async press(s) { sprigcast.setTimer(await sprigcast.run("timer.add", { seconds: addSeconds(s) })); }
	paint(action, s) {
		const seconds = addSeconds(s);
		return action.setTitle(`${seconds < 0 ? "-" : "+"}${clockText(Math.abs(seconds))}`);
	}
}

class ReplayKey extends SprigCastAction {
	constructor() { super("replay", 1); this.okAfterPress = true; }
	press() { return sprigcast.run("replay.play"); }
	said() { return "Replay"; }
}

class Pngtuber extends SprigCastAction {
	constructor() { super("pngtuber", 1); this.okAfterPress = true; }
	press(s) {
		const seconds = Number(s.seconds) > 0 ? { seconds: Number(s.seconds) } : {};
		if (s.mode === "next") return sprigcast.run("pngtuber.next", seconds);
		if (s.mode === "reset") return sprigcast.run("pngtuber.reset");
		return sprigcast.run("pngtuber.expression", { expression: s.expression ?? "", ...seconds });
	}
	said(result) { return result?.expression ? String(result.expression) : "Done"; }
}

class FreezeKey extends SprigCastAction {
	constructor() { super("freeze", 1); this.okAfterPress = true; }
	async press(s) {
		const name = s.what ?? "";
		const which = (await sprigNames()).includes(name) ? { sprig: name } : { source: name }; // a sprig: everywhere it's shown
		if (s.mode === "toggle") return sprigcast.run("freeze.toggle", which);
		if (s.mode === "stop") return sprigcast.run("freeze.stop", which);
		const seconds = String(s.seconds ?? "").trim() === "" ? NaN : Number(s.seconds);
		return sprigcast.run("freeze.start", { ...which, ...(Number.isFinite(seconds) ? { seconds } : {}) }); // none: its usual time
	}
	said(result) { return result?.frozen?.length ? "Frozen" : "Moving"; }
}

class RunAction extends SprigCastAction {
	constructor() { super("run", 1); this.okAfterPress = true; }
	press(s) {
		let fields = {};
		if (s.fields) fields = JSON.parse(s.fields); // a bad JSON throws: the key shows an alert
		return sprigcast.run(s.action, fields);
	}
}

/**
 * Teleprompter: runs the THSV Teleprompter app (its own local web control, GET /control/<command>), not
 * SprigCast. It works whether SprigCast is open or not, so it never goes dark or opens a SprigCast connection.
 */
class TeleprompterKey extends SprigCastAction {
	constructor() { super("teleprompter", 1); }
	async onWillAppear(ev) {
		await this.dress(ev.action);
		return this.paint(ev.action, ev.payload.settings);
	}
	onWillDisappear() {}
	async dress(action) {
		if (!action.isKey()) return;
		if (keyStyle === "canopy") return action.setImage();
		return action.setImage(`imgs/styles/${keyStyle}/${this.suffix}/key0@2x.png`);
	}
	async onSendToPlugin() {}
	async onKeyDown(ev) {
		const s = ev.payload.settings;
		try {
			await teleprompterSend(teleprompterCommand(s), teleprompterPort(s));
			await ev.action.showOk();
		} catch (error) {
			streamDeck.logger.warn(`${this.manifestId}: ${error.message}`);
			await ev.action.showAlert();
			await flash(ev.action, error instanceof NoPageError ? "No page\nopen" : "Not\nrunning");
			setTimeout(() => this.paint(ev.action, s).catch(() => {}), 1900);
		}
	}
	paint(action, s) { return action.setTitle(TELEPROMPTER_TITLES[teleprompterCommand(s)]); }
	repaintAll() { return Promise.resolve(); } // nothing from SprigCast changes it
}

const actions = [new ShowBloom(), new MuteSound(), new SoundVolume(), new ToggleItem(), new GoLive(), new PlaceKey(), new Record(), new RecordPause(), new RecordChapter(),
	new ClipsKey(), new SaveClip(), new CameraKey(), new Screenshot(), new MediaKey(), new FilterKey(),
	new TransitionKey(), new GardenKey(), new PathKey(), new SprigStateAction(), new DeathCount(), new Pngtuber(),
	new TimerKey(), new TimerReset(), new TimerAdd(), new ReplayKey(), new FreezeKey(),
	new RunAction(), new TeleprompterKey()];
for (const action of actions) streamDeck.actions.registerAction(action);

sprigcast.on("changed", () => all.forEach((a) => a.repaintAll().catch(() => {})));
const timerKeys = actions.find((a) => a instanceof TimerKey);
setInterval(() => timerKeys.tick().catch(() => {}), 250); // a quarter second: the time turns over close to SprigCast's own
// Connected or gone: every key swaps between its own picture and the dark offline one, then shows SprigCast's state.
sprigcast.on("connection", () => all.forEach((kind) => {
	for (const action of kind.actions) kind.dress(action).catch(() => {});
	kind.repaintAll().catch(() => {});
}));
sprigcast.on("refused", (why) => streamDeck.logger.warn(`SprigCast refused the connection: ${why}`));
sprigcast.on("status", () => sendStatus());
// Paired with a code: keep this device's own key as the password (the code only works once).
sprigcast.on("paired", (key) => {
	globals = { ...globals, password: key, pair: "" };
	streamDeck.settings.setGlobalSettings(globals).catch(() => {});
});
sprigcast.on("pairFailed", () => {
	globals = { ...globals, pair: "" };
	streamDeck.settings.setGlobalSettings(globals).catch(() => {});
});

await streamDeck.connect();
await loadGlobalSettings();
