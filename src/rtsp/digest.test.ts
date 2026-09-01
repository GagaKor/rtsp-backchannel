import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { test } from 'node:test';
import {
  digestAuthorization,
  parseDigestChallenge,
  type DigestChallenge,
} from './digest.ts';

const md5 = (s: string) => crypto.createHash('md5').update(s).digest('hex');
const sha256 = (s: string) => crypto.createHash('sha256').update(s).digest('hex');

const MD5_QOP: DigestChallenge = {
  realm: 'cam', nonce: 'abc123', qop: 'auth', algorithm: 'MD5',
};

test('MD5 with qop reproduces the byte layout RtspClient sent before extraction', () => {
  const header = digestAuthorization({
    user: 'admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
    challenge: MD5_QOP, nonceCount: 1, cnonce: 'deadbeefdeadbeef',
  });
  const ha1 = md5('admin:cam:secret');
  const ha2 = md5('DESCRIBE:rtsp://cam/s1');
  const resp = md5(`${ha1}:abc123:00000001:deadbeefdeadbeef:auth:${ha2}`);
  assert.equal(
    header,
    'Digest username="admin", realm="cam", nonce="abc123", ' +
      'uri="rtsp://cam/s1", qop=auth, nc=00000001, cnonce="deadbeefdeadbeef", ' +
      `response="${resp}"`,
  );
});

test('MD5 without qop reproduces the pre-extraction byte layout', () => {
  const header = digestAuthorization({
    user: 'admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
    challenge: { realm: 'cam', nonce: 'abc123', algorithm: 'MD5' },
    nonceCount: 1, cnonce: 'deadbeefdeadbeef',
  });
  const ha1 = md5('admin:cam:secret');
  const ha2 = md5('DESCRIBE:rtsp://cam/s1');
  assert.equal(
    header,
    'Digest username="admin", realm="cam", nonce="abc123", ' +
      `uri="rtsp://cam/s1", response="${md5(`${ha1}:abc123:${ha2}`)}"`,
  );
});

test('opaque is appended last, as it was before extraction', () => {
  const header = digestAuthorization({
    user: 'admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
    challenge: { realm: 'cam', nonce: 'abc123', opaque: 'op-1', algorithm: 'MD5' },
    nonceCount: 1, cnonce: 'deadbeefdeadbeef',
  });
  assert.match(header, /, opaque="op-1"$/);
});

test('SHA-256 hashes with sha256 and keeps the RFC parameter shape', () => {
  const challenge: DigestChallenge = {
    realm: 'cam', nonce: 'n1', qop: 'auth', algorithm: 'SHA-256',
  };
  const header = digestAuthorization({
    user: 'admin', pass: 'secret', method: 'MULTITRANS', uri: 'rtsp://cam/multitrans',
    challenge, nonceCount: 1, cnonce: 'aaaabbbbccccdddd',
  });
  const ha1 = sha256('admin:cam:secret');
  const ha2 = sha256('MULTITRANS:rtsp://cam/multitrans');
  const resp = sha256(`${ha1}:n1:00000001:aaaabbbbccccdddd:auth:${ha2}`);
  assert.match(header, /qop=auth, nc=00000001,/);
  assert.match(header, new RegExp(`response="${resp}"`));
});

test('the vigi style quotes qop and nc and orders cnonce before nc', () => {
  const challenge: DigestChallenge = {
    realm: 'TP-LINK IP-Camera', nonce: 'n2', qop: 'auth', algorithm: 'SHA-256',
  };
  const header = digestAuthorization({
    user: 'admin', pass: 'secret', method: 'MULTITRANS', uri: 'rtsp://cam/multitrans',
    challenge, nonceCount: 1, cnonce: 'aaaabbbbccccdddd', style: 'vigi',
  });
  const ha1 = sha256('admin:TP-LINK IP-Camera:secret');
  const ha2 = sha256('MULTITRANS:rtsp://cam/multitrans');
  const resp = sha256(`${ha1}:n2:00000001:aaaabbbbccccdddd:auth:${ha2}`);
  assert.equal(
    header,
    'Digest username="admin", realm="TP-LINK IP-Camera", nonce="n2", ' +
      'uri="rtsp://cam/multitrans", qop="auth", cnonce="aaaabbbbccccdddd", ' +
      `nc="00000001", response="${resp}"`,
  );
});

test('nonceCount is rendered as eight lowercase hex digits', () => {
  const header = digestAuthorization({
    user: 'admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
    challenge: MD5_QOP, nonceCount: 255, cnonce: 'deadbeefdeadbeef',
  });
  assert.match(header, /nc=000000ff,/);
});

test('parses a Digest challenge and defaults the algorithm to MD5', () => {
  const parsed = parseDigestChallenge('Digest realm="cam", nonce="abc"');
  assert.deepEqual(parsed, { realm: 'cam', nonce: 'abc', algorithm: 'MD5' });
});

test('parses a SHA-256 challenge with qop', () => {
  const parsed = parseDigestChallenge(
    'Digest realm="cam", nonce="abc", algorithm="SHA-256", qop="auth"',
  );
  assert.deepEqual(parsed, {
    realm: 'cam', nonce: 'abc', algorithm: 'SHA-256', qop: 'auth',
  });
});

test('parses a mixed-case qop token to lowercase auth, matching the pre-extraction behavior', () => {
  const upper = parseDigestChallenge('Digest realm="cam", nonce="abc", qop="AUTH"');
  assert.deepEqual(upper, { realm: 'cam', nonce: 'abc', algorithm: 'MD5', qop: 'auth' });

  const lower = parseDigestChallenge('Digest realm="cam", nonce="abc", qop="auth"');
  assert.deepEqual(lower, upper);

  const input = {
    user: 'admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
    nonceCount: 1, cnonce: 'deadbeefdeadbeef',
  };
  const headerFromUpper = digestAuthorization({ ...input, challenge: upper as DigestChallenge });
  const headerFromLower = digestAuthorization({ ...input, challenge: lower as DigestChallenge });
  assert.equal(headerFromUpper, headerFromLower);
});

test('reports a Basic-only challenge instead of parsing it as Digest', () => {
  assert.equal(parseDigestChallenge('Basic realm="cam"'), 'basic');
});

test('rejects an algorithm the implementation cannot compute', () => {
  assert.throws(
    () => parseDigestChallenge('Digest realm="cam", nonce="abc", algorithm="SHA-512"'),
    { message: 'unsupported RTSP Digest algorithm: SHA-512' },
  );
});

test('rejects a session-variant algorithm', () => {
  assert.throws(
    () => parseDigestChallenge('Digest realm="cam", nonce="abc", algorithm="MD5-sess"'),
    { message: 'unsupported RTSP Digest algorithm: MD5-sess' },
  );
});

test('rejects a challenge missing realm or nonce', () => {
  assert.throws(
    () => parseDigestChallenge('Digest realm="cam"'),
    { message: 'invalid RTSP Digest challenge: missing realm or nonce' },
  );
});

test('rejects control characters in a quoted value', () => {
  assert.throws(
    () => digestAuthorization({
      user: '\x01admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
      challenge: MD5_QOP, nonceCount: 1, cnonce: 'deadbeefdeadbeef',
    }),
    { message: 'RTSP Digest username contains control characters' },
  );
});

test('picks only the Digest challenge when Basic is offered first', () => {
  const parsed = parseDigestChallenge('Basic realm="cam"');
  assert.equal(parsed, 'basic');
  const digest = parseDigestChallenge('Digest realm="cam", nonce="abc"');
  assert.deepEqual(digest, { realm: 'cam', nonce: 'abc', algorithm: 'MD5' });
});
