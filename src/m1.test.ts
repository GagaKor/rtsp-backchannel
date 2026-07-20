import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

test('m1 delegates RTSP target handling to the shared sanitized parser', () => {
  const source = readFileSync(new URL('./m1.ts', import.meta.url), 'utf8');

  assert.match(source, /parseRtspTarget\(streamUri, user, pass\)/);
  assert.match(source, /const baseUri = endpoint\.uri/);
  assert.match(
    source,
    /new RtspClient\(endpoint\.host, endpoint\.port, endpoint\.user, endpoint\.pass\)/,
  );
  assert.doesNotMatch(source, /function rtspParts|\^rtsp:/);
});
