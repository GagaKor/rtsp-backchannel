import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pcm16ToG711 } from './g711.ts';

test('applies the Python-compatible Q11 volume before PCMA encoding', () => {
  const pcm = Int16Array.from([
    -32768,
    -30000,
    -1000,
    -1,
    0,
    1,
    1000,
    30000,
    32767,
  ]);

  const encoded = pcm16ToG711(pcm, 'PCMA', 0.05);

  assert.deepEqual([...encoded], [108, 98, 86, 85, 213, 213, 214, 226, 236]);
});
