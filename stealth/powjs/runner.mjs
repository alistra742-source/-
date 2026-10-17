/*
 * Native guns.lol proof-of-work runner.
 *
 * guns.lol gates a view on a WASM proof-of-work: the profile page carries a
 * `_gpp_ch` challenge, and a worker module served from `/_challenge/pow/<id>/…`
 * holds the solver. guns.lol *rotates* that module — binary, glue, and the
 * hashed export names — so nothing about it can be hard-coded: the worker's
 * obfuscated glue import is decoded, the glue is fetched, and the wasm is
 * driven through the glue the page itself would use. Python scrapes and
 * orchestrates (stealth/guns.py); this file only does the arithmetic, because
 * the module is wasm-bindgen JavaScript and wasm-bindgen JavaScript wants a
 * JavaScript host.
 *
 *   node runner.mjs <job.json>
 *   node runner.mjs --selftest
 *
 * job.json: { glue, wasm, args, class?, solve?, out? }
 *   glue   path to the fetched wasm-bindgen glue (.mjs)
 *   wasm   path to the matching binary
 *   args   constructor arguments, in the worker's own order
 *   class  export name of the solver class (optional — discovered when absent)
 *   solve  method name that produces the proof (optional — discovered when absent)
 *
 * Prints one JSON object on stdout: { ok: true, proof, raw } or
 * { ok: false, error }.
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';

const META_METHODS = new Set(['constructor', 'free', '__destroy_into_raw']);

/** Load a glue module. `bust` keeps two rotations in one process from colliding. */
async function loadGlue(gluePath, bust) {
  const url = pathToFileURL(gluePath).href + (bust ? `?v=${bust}` : '');
  return await import(url);
}

/** Instantiate the wasm behind a glue module, newest wasm-bindgen first. */
function initWasm(mod, wasmPath) {
  const bytes = fs.readFileSync(wasmPath);
  const module = new WebAssembly.Module(bytes);
  if (typeof mod.initSync === 'function') {
    mod.initSync({ module });
    return 'initSync';
  }
  // Older/newer glue exposes only the async default; hand it the compiled module.
  if (typeof mod.default === 'function') {
    const out = mod.default({ module_or_path: module });
    if (out && typeof out.then === 'function') return 'default(async)';
    return 'default';
  }
  throw new Error('glue exposes neither initSync nor default');
}

/** The solver class is the exported constructor with methods beyond free(). */
function discoverClass(mod) {
  for (const name of Object.keys(mod)) {
    const value = mod[name];
    if (typeof value !== 'function' || !value.prototype) continue;
    const methods = Object.getOwnPropertyNames(value.prototype)
      .filter((m) => !META_METHODS.has(m));
    if (methods.length) return { name, method: methods[0] };
  }
  return null;
}

/** Find the method that returns the proof, when the caller did not name it. */
async function discoverSolve(mod, className, solveName, args, proofKey) {
  const proto = mod[className].prototype;
  const candidates = Object.getOwnPropertyNames(proto).filter((m) => !META_METHODS.has(m));
  for (const method of candidates) {
    if (solveName && method !== solveName) continue;
    let instance;
    try {
      instance = new mod[className](...args);
    } catch (e) {
      throw new Error(`constructor ${className} rejected the challenge arguments: ${e}`);
    }
    try {
      const result = await instance[method]();
      if (result && typeof result === 'object' && typeof result[proofKey] === 'string') {
        return { method, result };
      }
    } catch (e) {
      if (solveName) throw new Error(`${className}.${method}() failed: ${e}`);
    } finally {
      try { if (typeof instance.free === 'function') instance.free(); } catch { /* freed */ }
    }
  }
  return null;
}

async function solve(job) {
  const proofKey = job.proofKey || '_oo';
  const dealClass = job.class || null;
  const dealSolve = job.solve || null;
  const bust = path.basename(job.glue);

  const mod = await loadGlue(job.glue, bust);
  const init = initWasm(mod, job.wasm);

  let className = dealClass;
  let solveName = dealSolve;
  if (!className || typeof mod[className] !== 'function') {
    const found = discoverClass(mod);
    if (!found) throw new Error('no solver class among the glue exports');
    className = found.name;
    if (!solveName) solveName = found.method;
  }

  const hit = await discoverSolve(mod, className, solveName, job.args || [], proofKey);
  if (!hit) throw new Error(`no method on ${className} produced ${proofKey}`);

  return { init, className, solve: hit.method, raw: hit.result, proof: hit.result[proofKey] };
}

/** Exercise discovery + invocation against a stub glue — no wasm, no network. */
async function selftest() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pow-selftest-'));
  const stub = path.join(dir, 'stub.mjs');
  fs.writeFileSync(stub, `
    let live = false;
    export function initSync() { live = true; }
    export class _deadbeefcafe {
      constructor(a, b, c, d, e) { this.args = [a, b, c, d, e]; }
      free() {}
      _solve123() {
        if (!live) throw new Error('wasm not initialised');
        return { _oo: 'proof-' + this.args.join('|') };
      }
    }
  `);
  const job = { glue: stub, wasm: path.join(dir, 'missing.wasm'), args: ['c', 5, '1', 'n', 's'],
                proofKey: '_oo', skipWasm: true };
  // The stub needs no binary; drive solve() with a no-op init.
  const mod = await loadGlue(stub, 'stub');
  mod.initSync({ module: null });
  const found = discoverClass(mod);
  const hit = await discoverSolve(mod, found.name, null, job.args, '_oo');
  const ok = found.name === '_deadbeefcafe' && hit.method === '_solve123'
    && hit.result._oo === 'proof-c|5|1|n|s';
  fs.rmSync(dir, { recursive: true, force: true });
  return { ok, discovered: found, solved: hit };
}

const arg = process.argv[2];
if (arg === '--selftest') {
  try {
    const out = await selftest();
    process.stdout.write(JSON.stringify(out) + '\n');
    process.exit(out.ok ? 0 : 1);
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }) + '\n');
    process.exit(1);
  }
}

const jobPath = arg || process.env.POW_JOB;
try {
  const job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
  const out = await solve(job);
  if (job.out) fs.writeFileSync(job.out, JSON.stringify(out));
  process.stdout.write(JSON.stringify({ ok: true, ...out }) + '\n');
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }) + '\n');
  process.exit(1);
}
