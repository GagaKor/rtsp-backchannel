import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const REPO_ROOT = fileURLToPath(new URL('../..', import.meta.url));

/**
 * Loads two modules in the given order in a fresh process. ESM caches per
 * process, so a load-order bug is invisible to a suite that has already
 * imported half the graph — it only shows when a process starts with the
 * module that evaluates first.
 */
function loadInOrder(...specifiers: string[]): ReturnType<typeof spawnSync> {
  const chain = specifiers
    .map((s) => `await import(${JSON.stringify(s)});`)
    .join('\n');
  return spawnSync(process.execPath, ['--experimental-transform-types', '--input-type=module', '-e', chain], {
    encoding: 'utf8',
    cwd: REPO_ROOT,
    timeout: 60_000,
  });
}

test('the backchannel and VIGI talk modules load in either order', () => {
  // These two formed a load-time cycle: talk.ts computed a top-level constant
  // from backchannel.ts's SAMPLE_RATE, so whenever backchannel.ts evaluated
  // first the constant was read in its temporal dead zone and threw
  // `Cannot access 'SAMPLE_RATE' before initialization`. It was worked around
  // with a dynamic import() on the VIGI open path, which left the cycle in
  // place -- and put an unanalyzable runtime import in a package that also
  // ships a CommonJS require entry point. Pacing now lives in its own module
  // and neither direction is a cycle.
  for (const order of [
    ['./src/backchannel.ts', './src/vigi/talk.ts'],
    ['./src/vigi/talk.ts', './src/backchannel.ts'],
  ]) {
    const result = loadInOrder(...order);
    assert.equal(
      result.status,
      0,
      `${order.join(' then ')} failed: ${
        result.error ? result.error.message : String(result.stderr)
      }`,
    );
  }
});

test('nothing in the shipped graph reaches a transport through a dynamic import', () => {
  // The cycle's workaround was `await import('./vigi/talk.ts')`. A static
  // import is now possible, and must stay that way: a runtime import() is
  // opaque to bundlers and to the require() entry point.
  const source = readFileSync(new URL('../backchannel.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(
    source,
    /await import\(/,
    'backchannel.ts must import its transports statically',
  );
});

test('pacing does not import a transport', () => {
  // The property that keeps the cycle gone: this module sits below both
  // transports, so adding an import of either would recreate it.
  const source = readFileSync(new URL('./pacing.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /from '\.\.\/backchannel\.ts'/);
  assert.doesNotMatch(source, /from '\.\.\/vigi\//);
  assert.doesNotMatch(source, /from '\.\.\/rtsp\//);
});
