/**
 * Bundle playwright-selector-generator into a browser IIFE for page.evaluate injection.
 *
 * Usage (from repo root or scripts/codegen-iife):
 *   node scripts/build_codegen_iife.mjs
 *
 * Rebuild after upgrading playwright-selector-generator / Playwright major versions.
 */
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const vendorDir = path.join(__dirname, "codegen-iife");
const require = createRequire(path.join(vendorDir, "package.json"));
const esbuild = require("esbuild");

const entry = path.join(vendorDir, "entry.js");
const outfile = path.join(root, "core", "assets", "codegen_locator.iife.js");

await esbuild.build({
  entryPoints: [entry],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["chrome100"],
  outfile,
  minify: true,
  logLevel: "info",
  absWorkingDir: vendorDir,
});

console.log("wrote", outfile);
