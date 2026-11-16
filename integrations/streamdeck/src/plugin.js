// SprigCast for Stream Deck. Each key is a SingletonAction (manifest UUIDs below); they share one
// connection to SprigCast, opened only while a SprigCast key is on screen.

import streamDeck, { SingletonAction } from "@elgato/streamdeck";

import { SprigCast } from "./sprigcast.js";

const ID = "com.sprigcast.sprigcast";
const sprigcast = new SprigCast();
const visible = new Set(); // action contexts on screen
const all = [];

function refreshConnection() {
	sprigcast.want(visible.size > 0);
}

async function loadGlobalSettings() {
	const settings = await streamDeck.settings.getGlobalSettings();
	sprigcast.configure({ port: Number(settings.port) || 0, password: settings.password ?? "" });
}

streamDeck.settings.onDidReceiveGlobalSettings((ev) => {
	const settings = ev.settings ?? {};
	sprigcast.configure({ port: Number(settings.port) || 0, password: settings.password ?? "" });
});

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
	};
	const fill = lists[event];
	if (!fill) return null;
	try {
		const names = [...new Set(await fill())].sort((a, b) => String(a).localeCompare(String(b)));
		const label = TITLED.has(event) ? (n) => n.charAt(0).toUpperCase() + n.slice(1) : (n) => n;
		return names.map((name) => ({ label: label(String(name)), value: String(name) }));
	} catch {
		return [{ label: "Open SprigCast first", value: "", disabled: true }];
	}
}

/** The Connection box in the key settings says whether SprigCast is found (and on which port). */
function sendStatus() {
	return streamDeck.ui.sendToPropertyInspector({ event: "status", text: sprigcast.status }).catch(() => {});
}

class SprigCastAction extends SingletonAction {
	constructor(suffix) {
		super();
		this.manifestId = `${ID}.${suffix}`;
		all.push(this);
	}

	onWillAppear(ev) {
		visible.add(ev.action.id);
		refreshConnection();
		return this.paint(ev.action, ev.payload.settings);
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
			await this.press(ev.payload.settings, ev.action);
			if (this.okAfterPress) await ev.action.showOk();
		} catch (error) {
			streamDeck.logger.warn(`${this.manifestId}: ${error.message}`);
			await ev.action.showAlert();
		}
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
	constructor() { super("volume"); }
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

class Record extends SprigCastAction {
	constructor() { super("record"); }
	press() { return sprigcast.run("record.toggle"); }
	paint(action) { return action.setState(sprigcast.state.record ? 1 : 0); }
}

class SaveClip extends SprigCastAction {
	constructor() { super("clip"); this.okAfterPress = true; }
	press() { return sprigcast.run("clip.save"); }
}

class Screenshot extends SprigCastAction {
	constructor() { super("screenshot"); this.okAfterPress = true; }
	press() { return sprigcast.run("screenshot.take"); }
}

class SprigStateAction extends SprigCastAction {
	constructor() { super("sprig-state"); this.okAfterPress = true; }
	press(s) { return sprigcast.run("sprig.state", { sprig: s.sprig, state: s.state }); }
}

class DeathCount extends SprigCastAction {
	constructor() { super("deaths"); }
	async press(s, action) {
		const verb = { remove: "deaths.remove", reset: "deaths.reset", show: "deaths.show" }[s.mode] ?? "deaths.add";
		const result = await sprigcast.run(verb);
		if (typeof result.count === "number") await action.setTitle(String(result.count));
	}
	paint(action) {
		return sprigcast.state.deaths === null ? undefined : action.setTitle(String(sprigcast.state.deaths));
	}
}

class RunAction extends SprigCastAction {
	constructor() { super("run"); this.okAfterPress = true; }
	press(s) {
		let fields = {};
		if (s.fields) fields = JSON.parse(s.fields); // a bad JSON throws: the key shows an alert
		return sprigcast.run(s.action, fields);
	}
}

const actions = [new ShowBloom(), new MuteSound(), new SoundVolume(), new ToggleItem(), new GoLive(), new Record(),
	new ClipsKey(), new SaveClip(), new CameraKey(), new Screenshot(), new MediaKey(), new FilterKey(),
	new TransitionKey(), new GardenKey(), new PathKey(), new SprigStateAction(), new DeathCount(), new RunAction()];
for (const action of actions) streamDeck.actions.registerAction(action);

sprigcast.on("changed", () => all.forEach((a) => a.repaintAll().catch(() => {})));
sprigcast.on("connection", () => all.forEach((a) => a.repaintAll().catch(() => {})));
sprigcast.on("refused", (why) => streamDeck.logger.warn(`SprigCast refused the connection: ${why}`));
sprigcast.on("status", () => sendStatus());

await streamDeck.connect();
await loadGlobalSettings();
