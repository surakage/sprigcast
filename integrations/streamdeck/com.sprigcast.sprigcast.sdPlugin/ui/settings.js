// The settings panel for every SprigCast key. Stream Deck calls connectElgatoStreamDeckSocket().
// Fields per key are below. Names (blooms, sounds...) are drop-down lists filled from SprigCast: the whole list
// every time, whatever is picked now. A name that isn't in SprigCast's list right now stays in it, so a key set up
// while SprigCast is closed keeps its setting.

const FIELDS = {
	bloom: [["bloom", "Bloom", "getBlooms"], ["shape", "Shape", ["both", "horizontal", "vertical"]]],
	mute: [["sound", "Sound", "getSounds"], ["mode", "Press", ["toggle", "mute", "unmute"]]],
	item: [["bloom", "Bloom", "getBlooms"], ["item", "Item", "getSources"], ["mode", "Press", ["toggle", "show", "hide"]]],
	"sprig-state": [["sprig", "Sprig", "getSprigs"], ["state", "State", null]],
	volume: [["sound", "Sound", "getSounds"], ["mode", "Press", ["change", "set"]], ["step", "Change (dB)", null],
		["db", "Set to (dB)", null]],
	media: [["media", "Media", "getMedia"], ["mode", "Press", ["toggle", "play", "pause", "restart", "stop"]]],
	filter: [["source", "Source", "getSources"], ["filter", "Filter", "getFilters"], ["mode", "Press", ["toggle", "on", "off"]]],
	transition: [["kind", "Transition", "getTransitions"], ["ms", "Length (ms)", null]],
	garden: [["garden", "Garden", "getGardens"]],
	path: [["path", "Path", "getPaths"]],
	deaths: [["mode", "Press", ["add", "remove", "reset", "show"]]],
	"timer-add": [["seconds", "Seconds", null]],
	freeze: [["what", "Sprig or source", "getFreezable"], ["mode", "Press", ["freeze", "toggle", "stop"]], ["seconds", "Seconds", null]],
	pngtuber: [["mode", "Press", ["expression", "next", "reset"]], ["expression", "Expression", "getExpressions"],
		["seconds", "For seconds", null]],
	run: [["action", "Action", "getActions"], ["fields", "Fields (JSON)", "json"]],
};

let socket, context, settings = {}, globals = {};

function send(event, payload) {
	socket?.send(JSON.stringify({ event, context, ...(payload === undefined ? {} : { payload }) }));
}

function build(kind) {
	const form = document.getElementById("fields");
	for (const [key, label, source] of FIELDS[kind] ?? []) {
		const row = document.createElement("label");
		row.className = "row";
		row.innerHTML = `<span></span>`;
		row.firstChild.textContent = label;
		let input;
		if (Array.isArray(source)) {
			input = document.createElement("select");
			for (const value of source) input.add(new Option(value[0].toUpperCase() + value.slice(1), value));
		} else if (source === "json") {
			input = document.createElement("textarea");
			input.placeholder = '{"bloom": "BRB"}';
		} else if (source) {
			input = document.createElement("select");
			input.dataset.source = source;
			input.dataset.key = key;
			fillSelect(input, []);
		} else {
			input = document.createElement("input");
		}
		input.id = `f-${key}`;
		if (input.tagName !== "SELECT" || Array.isArray(source)) input.value = settings[key] ?? (Array.isArray(source) ? source[0] : "");
		const save = () => {
			if (settings[key] === input.value) return;
			settings[key] = input.value;
			send("setSettings", settings);
			if (key === "source") askLists("getFilters"); // a source's own filters
		};
		input.addEventListener("change", save);
		if (input.tagName !== "SELECT") input.addEventListener("input", save); // saved as you type: a press works at once
		row.appendChild(input);
		form.appendChild(row);
	}
	if (!(FIELDS[kind] ?? []).length) document.getElementById("connection").open = true;
}

/** Every name SprigCast has, plus the one picked now (even if SprigCast doesn't have it right now). */
function fillSelect(select, items) {
	const picked = settings[select.dataset.key] ?? "";
	const listed = items.filter((i) => i.value);
	const names = listed.map((i) => i.value);
	const options = [new Option(names.length || picked ? "Choose…" : "Open SprigCast to pick", "")];
	options[0].disabled = true;
	if (picked && !names.includes(picked)) options.push(new Option(picked, picked));
	for (const item of listed) options.push(new Option(item.label ?? item.value, item.value));
	select.replaceChildren(...options);
	select.value = picked;
}

function fillList(event, items) {
	for (const select of document.querySelectorAll(`select[data-source="${event}"]`)) fillSelect(select, items);
}

/** Ask the plugin for the lists (all of them, or one). */
function askLists(only) {
	const wanted = new Set([...document.querySelectorAll("select[data-source]")].map((s) => s.dataset.source));
	for (const event of wanted) {
		if (event === "getFilters" && !settings.source) continue; // pick a source first
		if (!only || event === only) send("sendToPlugin", { event, source: settings.source });
	}
}

function showGlobals() {
	document.getElementById("port").value = globals.port ?? "";
	document.getElementById("password").value = globals.password ?? "";
	document.getElementById("style").value = globals.style ?? "canopy";
	document.getElementById("pair").value = globals.pair ?? "";
}

for (const id of ["port", "password", "style", "pair"]) {
	const save = (ev) => {
		if (globals[id] === ev.target.value) return;
		globals[id] = ev.target.value;
		send("setGlobalSettings", globals);
	};
	document.getElementById(id).addEventListener("change", save);
}

// eslint-disable-next-line no-unused-vars
function connectElgatoStreamDeckSocket(port, uuid, registerEvent, _info, actionInfo) {
	context = uuid;
	const info = JSON.parse(actionInfo);
	settings = info.payload?.settings ?? {};
	build(String(info.action).split(".").pop());
	socket = new WebSocket(`ws://127.0.0.1:${port}`);
	socket.onopen = () => {
		socket.send(JSON.stringify({ event: registerEvent, uuid }));
		send("getGlobalSettings");
		send("sendToPlugin", { event: "getStatus" });
		askLists();
	};
	socket.onmessage = (message) => {
		const data = JSON.parse(message.data);
		if (data.event === "didReceiveGlobalSettings") {
			globals = data.payload?.settings ?? {};
			showGlobals();
		} else if (data.event === "sendToPropertyInspector" && data.payload?.event === "status") {
			const text = data.payload.text ?? "";
			if (text.startsWith("Connected") && !document.getElementById("status").textContent.startsWith("Connected")) askLists();
			document.getElementById("status").textContent = text;
		} else if (data.event === "sendToPropertyInspector" && data.payload?.items) {
			fillList(data.payload.event, data.payload.items);
		}
	};
}
