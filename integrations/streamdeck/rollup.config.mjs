import commonjs from "@rollup/plugin-commonjs";
import nodeResolve from "@rollup/plugin-node-resolve";

const sdPlugin = "com.sprigcast.sprigcast.sdPlugin";

export default {
	input: "src/plugin.js",
	output: { file: `${sdPlugin}/bin/plugin.js`, format: "es" },
	plugins: [
		nodeResolve({ browser: false, exportConditions: ["node"], preferBuiltins: true }),
		commonjs(),
		{
			name: "emit-module-package-file",
			generateBundle() {
				this.emitFile({ fileName: "package.json", source: `{ "type": "module" }`, type: "asset" });
			},
		},
	],
};
