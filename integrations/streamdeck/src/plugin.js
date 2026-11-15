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
	sprigcast.configure({ port: Number(settings.port) || 4455, password: settings.password ?? "" });
}

streamDeck.settings.onDidReceiveGlobalSettings((ev) => {
	const settings = ev.settings ?? {};
	sprigcast.configure({ port: Number(settings.port) || 4455, password: settings.password ?? "" });
});

/** Lists for the property inspector's pickers (sdpi-select datasource). */
async function listFor(event) {
	const lists = {
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
		return names.map((name) => ({ label: String(name), value: String(name) }));
	} catch {
		return [{ label: "Open SprigCast first", value: "", disabled: true }];
	}
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
		const items = await listFor(ev.payload?.event);
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
			if (action.isKey()) await this.paint(action, await action.getSettings());
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
	paint(action, s) {
		const shown = sprigcast.state.items[`${s.bloom}/${s.item}`];
		return typeof shown === "boolean" ? action.setState(shown ? 0 : 1) : undefined;
	}
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

const actions = [new ShowBloom(), new MuteSound(), new ToggleItem(), new GoLive(), new Record(), new SaveClip(),
	new Screenshot(), new SprigStateAction(), new DeathCount(), new RunAction()];
for (const action of actions) streamDeck.actions.registerAction(action);

sprigcast.on("changed", () => all.forEach((a) => a.repaintAll().catch(() => {})));
sprigcast.on("connection", () => all.forEach((a) => a.repaintAll().catch(() => {})));
sprigcast.on("refused", (why) => streamDeck.logger.warn(`SprigCast refused the connection: ${why}`));

await streamDeck.connect();
await loadGlobalSettings();
