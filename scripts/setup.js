'use strict';
/*
 * One-command readiness check: browser present AND able to start.
 *
 *   npm run setup    # browser + system libraries
 *   npm run deps     # system libraries only
 *
 * Both are automatic at first launch; these exist so you can pre-warm a build
 * image or diagnose without starting the app.
 */
const { ensureChromium, ensureChromiumDeps } = require('../runner');

(async () => {
  const exe = await ensureChromium((m) => console.log(`[setup] ${m}`));
  console.log(`[setup] chromium: ${exe}`);
  await ensureChromiumDeps((m) => console.log(`[setup] ${m}`));
  console.log('[setup] ready — start the app with: npm start');
})().catch((e) => {
  console.error(`[setup] ${e.message}`);
  process.exit(1);
});
