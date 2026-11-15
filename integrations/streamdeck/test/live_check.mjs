// Manual check against a running SprigCast API: node test/live_check.mjs <port> <password>
import { SprigCast } from "../src/sprigcast.js";

const [port, password] = process.argv.slice(2);
const sc = new SprigCast({ port: Number(port), password });
const events = [];
sc.on("event", (e) => events.push(e.event));
sc.on("refused", (why) => { console.log("refused", why); process.exit(2); });
sc.once("connection", async (up) => {
	console.log("connected", up, JSON.stringify({ onAir: sc.state.onAir, garden: sc.state.garden, path: sc.state.path,
		clips: sc.state.clips, camera: sc.state.camera, transition: sc.state.transition, volumes: sc.state.volumes }));
	console.log("show", JSON.stringify(await sc.run("bloom.show", { bloom: "BRB" })));
	console.log("volume", JSON.stringify(await sc.run("sound.volume", { sound: "Mic", change: -3 })));
	await new Promise((r) => setTimeout(r, 600));
	console.log("onAir after", JSON.stringify(sc.state.onAir), "isOnAir BRB", sc.isOnAir("BRB"), "Mic", sc.state.volumes.Mic);
	console.log("events", events.join(","));
	try { await sc.run("bloom.show", { bloom: "Nope" }); } catch (e) { console.log("error ok:", e.message); }
	sc.want(false);
	process.exit(0);
});
sc.want(true);
