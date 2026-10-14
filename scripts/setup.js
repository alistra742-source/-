'use strict';
/*
 * Browser install, pinned into the project so the cache is found no matter
 * which user (and therefore which HOME) later runs the app.
 *
 *   npm run setup                 # chromium only
 *   npm run setup -- --with-deps  # also install OS libraries (needs root/sudo)
 */
const path = require('path');
const { spawnSync } = require('child_process');

const root = path.join(__dirname, '..');
const browsers = path.join(root, '.playwright-browsers');
const bin = path.join(root, 'node_modules', '.bin', process.platform === 'win32' ? 'playwright.cmd' : 'playwright');
const extra = process.argv.slice(2);

console.log(`[setup] installing chromium into ${browsers}`);

const r = spawnSync(bin, ['install', ...extra, 'chromium'], {
  stdio: 'inherit',
  env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: browsers },
});

if (r.error) {
  console.error(`[setup] could not run playwright: ${r.error.message}`);
  console.error('[setup] dependency missing — run: npm install');
  process.exit(1);
}
if (r.status !== 0) {
  console.error(`[setup] playwright exited ${r.status}`);
  process.exit(r.status || 1);
}
console.log('[setup] done — start the app with: npm start');
