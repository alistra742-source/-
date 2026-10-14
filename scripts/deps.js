'use strict';
/* Verify chromium can actually start, and fix it if it cannot. */
const { ensureChromiumDeps, chromiumExecutable } = require('../runner');

(async () => {
  await ensureChromiumDeps((m) => console.log(`[deps] ${m}`));
  console.log(`[deps] chromium starts cleanly: ${chromiumExecutable()}`);
})().catch((e) => {
  console.error(`[deps] ${e.message}`);
  process.exit(1);
});
