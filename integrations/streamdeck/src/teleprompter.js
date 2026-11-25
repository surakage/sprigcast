// The Teleprompter key talks to the THSV Teleprompter desktop app on this PC: GET /control/<command> on its
// local port (8765 unless changed in the app). It works with the teleprompter window in the background.

export const TELEPROMPTER_PORT = 8765;

/** Every command the app takes, with the word shown on the key. */
export const TELEPROMPTER_TITLES = {
	toggle: "Play/Pause",
	start: "Start",
	pause: "Pause",
	reset: "Reset",
	next: "Next",
	prev: "Back",
	faster: "Faster",
	slower: "Slower",
	bigger: "Bigger",
	smaller: "Smaller",
	mirror: "Mirror",
};

export function teleprompterCommand(settings = {}) {
	return Object.hasOwn(TELEPROMPTER_TITLES, settings.command) ? settings.command : "toggle";
}

/** The port from the key's settings, or 8765 when it's empty or not a port. */
export function teleprompterPort(settings = {}) {
	const port = Number(String(settings.tpPort ?? "").trim());
	return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : TELEPROMPTER_PORT;
}

export function teleprompterUrl(command, port, host = "127.0.0.1") {
	return `http://${host}:${port}/control/${encodeURIComponent(command)}`;
}

/** Sends one command. Tries 127.0.0.1, then localhost (an app listening on IPv6 only). Throws if it's not running. */
export async function teleprompterSend(command, port, fetchImpl = fetch) {
	let last;
	for (const host of ["127.0.0.1", "localhost"]) {
		try {
			const response = await fetchImpl(teleprompterUrl(command, port, host), { signal: AbortSignal.timeout(1500) });
			if (!response.ok) throw new Error(`Teleprompter answered ${response.status}`);
			return;
		} catch (error) {
			last = error;
			if (String(error.message).startsWith("Teleprompter answered")) break; // it's running; it said no
		}
	}
	throw new Error(`Teleprompter not reached on port ${port}: ${last?.message ?? "no answer"}`);
}
