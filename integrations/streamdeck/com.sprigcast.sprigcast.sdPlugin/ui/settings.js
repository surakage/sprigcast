// The settings panel for every SprigCast key. Stream Deck calls connectElgatoStreamDeckSocket().
// Fields per key are below; names fill in from SprigCast while it runs (any text works too).

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
		} else {
			input = document.createElement("input");
			if (source) {
				const list = document.createElement("datalist");
				list.id = `list-${key}`;
				input.setAttribute("list", list.id);
				row.appendChild(list);
				input.addEventListener("focus", () => send("sendToPlugin", { event: source, source: settings.source }));
				input.dataset.source = source;
			}
		}
		input.id = `f-${key}`;
		input.value = settings[key] ?? (Array.isArray(source) ? source[0] : "");
		input.addEventListener("change", () => {
			settings[key] = input.value;
			send("setSettings", settings);
		});
		row.appendChild(input);
		form.appendChild(row);
	}
	if (!(FIELDS[kind] ?? []).length) document.getElementById("connection").open = true;
}

function fillList(event, items) {
	for (const input of document.querySelectorAll(`[data-source="${event}"]`)) {
		const list = document.getElementById(input.getAttribute("list"));
		list.replaceChildren(...items.filter((i) => i.value).map((i) => new Option(i.label, i.value)));
	}
}

function showGlobals() {
	document.getElementById("port").value = globals.port ?? "";
	document.getElementById("password").value = globals.password ?? "";
}

for (const id of ["port", "password"]) {
	document.getElementById(id).addEventListener("change", (ev) => {
		globals[id] = ev.target.value;
		send("setGlobalSettings", globals);
	});
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
	};
	socket.onmessage = (message) => {
		const data = JSON.parse(message.data);
		if (data.event === "didReceiveGlobalSettings") {
			globals = data.payload?.settings ?? {};
			showGlobals();
		} else if (data.event === "sendToPropertyInspector" && data.payload?.event === "status") {
			document.getElementById("status").textContent = data.payload.text ?? "";
		} else if (data.event === "sendToPropertyInspector" && data.payload?.items) {
			fillList(data.payload.event, data.payload.items);
		}
	};
}
