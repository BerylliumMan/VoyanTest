# Codegen IIFE build

Bundles `playwright-selector-generator` for in-page locator solidify.

```bash
cd scripts/codegen-iife && npm ci
node ../build_codegen_iife.mjs
```

Output: `core/assets/codegen_locator.iife.js` (committed). Rebuild after upgrading the generator / Playwright major versions.
