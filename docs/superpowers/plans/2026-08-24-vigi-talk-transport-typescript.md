# VIGI Talk Transport (TypeScript) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TP-Link's VIGI OpenAPI `talk` protocol as a second audio-send transport behind the existing `BackchannelSession` interface, selectable via `transport: 'auto' | 'onvif' | 'vigi'` and defaulting to `'auto'`.

**Architecture:** `openBackchannel` becomes a selector over two transport implementations. The ONVIF path is extracted unchanged into `openOnvifBackchannel`; a new VIGI path pairs an HTTPS control client (`doAuth` → `stok`) with an RTSP-framed `MULTITRANS` stream session that carries interleaved RTP over TCP. Digest computation moves out of `RtspClient` into a shared, algorithm-aware module so both transports use it.

**Tech Stack:** TypeScript 7, Node 22+ (`node:https`, `node:net`, `node:crypto`), `node --test`, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-24-vigi-openapi-talk-transport-design.md`

**Sibling plans:** the Python and Rust ports are separate plans. All three must land before 0.4.0 ships — tri-language parity is a release requirement, not a per-plan one. Task 8 here produces the fixture those plans consume.

## Global Constraints

- Node 22 or later; no new runtime dependencies (`saxes` remains the only one).
- Never copy prose or tables from TP-Link's *VIGI IPC Open API Document*. Method names, field names, and error codes are functional identifiers and may be used. All descriptions are written fresh.
- `VIGI` and `TP-Link` are TP-Link marks: descriptive use only, no package name containing `vigi`, nothing implying affiliation or endorsement.
- Explicit codec preferences never fall back to a different codec (`src/rtsp/sdp.ts:117`).
- `doAuth` is attempted at most once per session open. Never retry `-10020` or `-10022`.
- The library never calls `setSpeakerVolume`.
- Camera HTTPS uses self-signed certificates: `node:https` with `rejectUnauthorized: false`, never global `fetch`.
- Error messages must not echo credentials or response bodies (existing repo rule — see `redactRtspCredentials`).
- Run `npm run typecheck` and `npm test` before every commit.

---

### Task 1: Extract algorithm-aware RTSP digest

Digest computation currently lives inside `RtspClient` and is MD5-only — `parseChallenge` actively throws on any other algorithm. VIGI's `MULTITRANS` needs SHA-256, and needs `qop`/`nc` quoted in TP-Link's parameter order. Extract the computation, add SHA-256, and put the vendor quirk behind an explicit style switch.

**Files:**
- Create: `src/rtsp/digest.ts`
- Create: `src/rtsp/digest.test.ts`
- Modify: `src/rtsp/backchannelClient.ts:37` (`md5` helper), `:39-77` (`parseDigestParameters`), `:77-85` (`quoteDigestValue`), `:148-179` (`authHeader`), `:180-210` (`parseChallenge`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DigestChallenge { realm: string; nonce: string; qop?: string; opaque?: string; algorithm: 'MD5' | 'SHA-256' }`
  - `DigestParameterStyle = 'rfc7616' | 'vigi'`
  - `parseDigestParameters(headerValue: string): Record<string, string> | undefined`
  - `parseDigestChallenge(headerValue: string): DigestChallenge | 'basic' | undefined`
  - `digestAuthorization(input: DigestAuthorizationInput): string` where
    `DigestAuthorizationInput { user: string; pass: string; method: string; uri: string; challenge: DigestChallenge; nonceCount: number; cnonce: string; style?: DigestParameterStyle }`

- [ ] **Step 1: Write the failing tests**

Create `src/rtsp/digest.test.ts`:

```ts
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
      user: 'admin', pass: 'secret', method: 'DESCRIBE', uri: 'rtsp://cam/s1',
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --experimental-transform-types --test src/rtsp/digest.test.ts`
Expected: FAIL — `Cannot find module './digest.ts'`.

- [ ] **Step 3: Write `src/rtsp/digest.ts`**

```ts
/**
 * RTSP/HTTP Digest authentication, shared by the ONVIF backchannel client and
 * the VIGI talk client.
 *
 * Two parameter styles exist because one camera family requires a
 * non-conformant one. See DigestParameterStyle.
 */
import crypto from 'node:crypto';

export type DigestAlgorithm = 'MD5' | 'SHA-256';

export interface DigestChallenge {
  realm: string;
  nonce: string;
  qop?: string;
  opaque?: string;
  algorithm: DigestAlgorithm;
}

/**
 * `rfc7616` renders `qop` and `nc` as bare tokens, which is what RFC 7616
 * specifies and what every ONVIF camera this library has met accepts.
 *
 * `vigi` quotes both and places `cnonce` before `nc`, matching the example in
 * TP-Link's VIGI IPC Open API document. Measured on a VIGI C540V (firmware
 * 2.3.3): the RFC form gets 401, this form gets 200. Do not make it the
 * default — quoting `qop` is not conformant.
 */
export type DigestParameterStyle = 'rfc7616' | 'vigi';

export interface DigestAuthorizationInput {
  user: string;
  pass: string;
  method: string;
  uri: string;
  challenge: DigestChallenge;
  /** 1-based, owned by the caller so one connection's counter never repeats. */
  nonceCount: number;
  cnonce: string;
  style?: DigestParameterStyle;
}

function hasher(algorithm: DigestAlgorithm): (value: string) => string {
  const nodeName = algorithm === 'SHA-256' ? 'sha256' : 'md5';
  return (value) => crypto.createHash(nodeName).update(value).digest('hex');
}

export function parseDigestParameters(
  headerValue: string,
): Record<string, string> | undefined {
  const digest = /\bDigest\s+/i.exec(headerValue);
  if (!digest) return undefined;
  const parameters: Record<string, string> = {};
  let index = digest.index + digest[0].length;
  while (index < headerValue.length) {
    while (index < headerValue.length && /[\s,]/.test(headerValue[index])) index++;
    const keyMatch = /^[a-z][a-z\d_-]*/i.exec(headerValue.slice(index));
    if (!keyMatch) break;
    const key = keyMatch[0].toLowerCase();
    index += keyMatch[0].length;
    while (headerValue[index] === ' ' || headerValue[index] === '\t') index++;
    if (headerValue[index] !== '=') break;
    index++;
    while (headerValue[index] === ' ' || headerValue[index] === '\t') index++;

    let value = '';
    if (headerValue[index] === '"') {
      index++;
      while (index < headerValue.length) {
        const character = headerValue[index++];
        if (character === '"') break;
        if (character === '\\' && index < headerValue.length) {
          value += headerValue[index++];
        } else {
          value += character;
        }
      }
    } else {
      const end = headerValue.indexOf(',', index);
      value = headerValue.slice(index, end < 0 ? headerValue.length : end).trim();
      index = end < 0 ? headerValue.length : end;
    }
    parameters[key] = value;
  }
  return parameters;
}

function escapeQuoted(name: string, value: string): string {
  if (/[\x00-\x1f\x7f]/.test(value)) {
    throw new Error(`RTSP Digest ${name} contains control characters`);
  }
  return value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/**
 * Returns the parsed challenge, the string `'basic'` when the header offers
 * Basic instead of Digest, or `undefined` when it is neither.
 */
export function parseDigestChallenge(
  headerValue: string,
): DigestChallenge | 'basic' | undefined {
  const parameters = parseDigestParameters(headerValue);
  if (!parameters) {
    return /^\s*Basic\b/i.test(headerValue) ? 'basic' : undefined;
  }
  const { realm, nonce } = parameters;
  if (!realm || !nonce) {
    throw new Error('invalid RTSP Digest challenge: missing realm or nonce');
  }
  const declared = parameters.algorithm || 'MD5';
  const normalised = declared.toUpperCase();
  if (normalised !== 'MD5' && normalised !== 'SHA-256') {
    throw new Error(`unsupported RTSP Digest algorithm: ${declared}`);
  }
  const challenge: DigestChallenge = {
    realm,
    nonce,
    algorithm: normalised as DigestAlgorithm,
  };
  if (parameters.qop !== undefined) {
    const auth = parameters.qop
      .split(',')
      .map((value) => value.trim())
      .find((value) => value === 'auth');
    if (auth) challenge.qop = auth;
  }
  if (parameters.opaque !== undefined) challenge.opaque = parameters.opaque;
  return challenge;
}

export function digestAuthorization(input: DigestAuthorizationInput): string {
  const { user, pass, method, uri, challenge, nonceCount, cnonce } = input;
  const style = input.style ?? 'rfc7616';
  const hash = hasher(challenge.algorithm);
  const ha1 = hash(`${user}:${challenge.realm}:${pass}`);
  const ha2 = hash(`${method}:${uri}`);

  const username = escapeQuoted('username', user);
  const realm = escapeQuoted('realm', challenge.realm);
  const nonce = escapeQuoted('nonce', challenge.nonce);
  const digestUri = escapeQuoted('uri', uri);
  const opaque = challenge.opaque === undefined
    ? ''
    : `, opaque="${escapeQuoted('opaque', challenge.opaque)}"`;
  const head =
    `Digest username="${username}", realm="${realm}", nonce="${nonce}", ` +
    `uri="${digestUri}"`;

  if (!challenge.qop) {
    return `${head}, response="${hash(`${ha1}:${challenge.nonce}:${ha2}`)}"${opaque}`;
  }

  const nc = nonceCount.toString(16).padStart(8, '0');
  const response = hash(
    `${ha1}:${challenge.nonce}:${nc}:${cnonce}:${challenge.qop}:${ha2}`,
  );
  if (style === 'vigi') {
    return (
      `${head}, qop="${challenge.qop}", cnonce="${cnonce}", nc="${nc}", ` +
      `response="${response}"${opaque}`
    );
  }
  return (
    `${head}, qop=${challenge.qop}, nc=${nc}, cnonce="${cnonce}", ` +
    `response="${response}"${opaque}`
  );
}

/** Eight bytes of hex, the cnonce length this library has always used. */
export function generateCnonce(): string {
  return crypto.randomBytes(8).toString('hex');
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --experimental-transform-types --test src/rtsp/digest.test.ts`
Expected: PASS, 13 tests.

- [ ] **Step 5: Rewire `RtspClient` onto the shared module**

In `src/rtsp/backchannelClient.ts`: delete the local `md5`, `parseDigestParameters`, `quoteDigestValue`, and the `DigestChallenge` interface; import from `./digest.ts`. Replace `authHeader` and `parseChallenge` bodies:

```ts
import {
  digestAuthorization,
  generateCnonce,
  parseDigestChallenge,
  type DigestChallenge,
} from './digest.ts';

  private authHeader(method: string, uri: string): string | undefined {
    if (this.basic) {
      return 'Basic ' + Buffer.from(`${this.user}:${this.pass}`).toString('base64');
    }
    const challenge = this.challenge;
    if (!challenge) return undefined;
    this.digestNonceCount += 1;
    return digestAuthorization({
      user: this.user,
      pass: this.pass,
      method,
      uri,
      challenge,
      nonceCount: this.digestNonceCount,
      cnonce: generateCnonce(),
    });
  }

  private parseChallenge(headerValue: string): void {
    const parsed = parseDigestChallenge(headerValue);
    if (parsed === 'basic') {
      this.basic = true;
      this.challenge = undefined;
      this.digestNonceCount = 0;
      return;
    }
    if (parsed === undefined) return;
    this.challenge = parsed;
  }
```

Note the counter now increments on every digest header, including the no-qop
branch where it is unused. That is harmless and keeps one increment site.

- [ ] **Step 6: Run the full suite to prove no ONVIF regression**

Run: `npm run typecheck && npm test`
Expected: typecheck clean; all existing tests pass, including
`src/rtsp/backchannelClient.test.ts` and `src/backchannel.test.ts`.

- [ ] **Step 7: Commit**

```bash
git add src/rtsp/digest.ts src/rtsp/digest.test.ts src/rtsp/backchannelClient.ts
git commit -m "refactor(rtsp): extract algorithm-aware digest auth from RtspClient"
```

---

### Task 2: VIGI control client

The control channel authenticates over HTTPS and answers two questions the transport needs: which port carries the stream, and whether the camera reports a speaker.

**Files:**
- Create: `src/vigi/control.ts`
- Create: `src/vigi/control.test.ts`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `VigiControlError extends Error` with `readonly code: number`
  - `VigiAudioCapability { speaker: boolean; microphone: boolean }`
  - `VigiControlSession { readonly stok: string; getStreamPort(): Promise<number>; getAudioCapability(): Promise<VigiAudioCapability> }`
  - `VigiControlOptions { host: string; user?: string; pass: string; port?: number; timeoutMs?: number }`
  - `VigiControlDependencies { postJson(url: string, body: unknown, timeoutMs: number): Promise<unknown> }`
  - `openVigiControl(options: VigiControlOptions): Promise<VigiControlSession>`
  - `openVigiControlWithDependencies(options: VigiControlOptions, dependencies: VigiControlDependencies): Promise<VigiControlSession>`
  - `DEFAULT_VIGI_CONTROL_PORT = 20443`

- [ ] **Step 1: Write the failing tests**

Create `src/vigi/control.test.ts`:

```ts
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { test } from 'node:test';
import {
  VigiControlError,
  openVigiControlWithDependencies,
  type VigiControlDependencies,
} from './control.ts';

const sha256 = (s: string) => crypto.createHash('sha256').update(s).digest('hex');

interface RecordedPost { url: string; body: unknown }

function fakeControl(
  posts: RecordedPost[],
  replies: {
    challenge?: unknown;
    auth?: unknown;
    streamPort?: unknown;
    audioCapability?: unknown;
  } = {},
): VigiControlDependencies {
  const challenge = replies.challenge ?? {
    method: 'doAuth',
    authenticate: {
      realm: 'TP-LINK IP-Camera',
      nonce: 'n0',
      algorithm: 'SHA-256',
      uri: 'doAuth',
      method: 'POST',
    },
    errCode: -10020,
  };
  return {
    async postJson(url, body) {
      posts.push({ url, body });
      const method = (body as { method?: string }).method;
      const params = (body as { params?: unknown }).params;
      if (method === 'doAuth' && params === null) return challenge;
      if (method === 'doAuth') {
        return replies.auth ?? { method: 'doAuth', stok: 'STOK1', errCode: 0 };
      }
      if (method === 'getStreamPort') {
        return replies.streamPort
          ?? { method: 'getStreamPort', result: { streamPort: '554' }, errCode: 0 };
      }
      if (method === 'getAudioCapability') {
        return replies.audioCapability ?? {
          method: 'getAudioCapability',
          result: { speaker: { volume: '1' }, microphone: { volume: '1' } },
          errCode: 0,
        };
      }
      return { method, errCode: -10030 };
    },
  };
}

const OPTIONS = { host: 'cam', pass: 'secret' };

test('computes the doAuth response from the challenge and exchanges it for a stok', async () => {
  const posts: RecordedPost[] = [];
  const session = await openVigiControlWithDependencies(OPTIONS, fakeControl(posts));

  assert.equal(session.stok, 'STOK1');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].url, 'https://cam:20443');
  assert.deepEqual(posts[0].body, { method: 'doAuth', params: null });

  const a1 = sha256('admin:TP-LINK IP-Camera:secret');
  const a2 = sha256('POST:doAuth');
  assert.deepEqual(posts[1].body, {
    method: 'doAuth',
    params: { nonce: 'n0', response: sha256(`${a1}:n0:${a2}`) },
  });
});

test('honours a non-default control port', async () => {
  const posts: RecordedPost[] = [];
  await openVigiControlWithDependencies({ ...OPTIONS, port: 21443 }, fakeControl(posts));
  assert.equal(posts[0].url, 'https://cam:21443');
});

test('sends later calls to the stok path', async () => {
  const posts: RecordedPost[] = [];
  const session = await openVigiControlWithDependencies(OPTIONS, fakeControl(posts));
  await session.getStreamPort();
  assert.equal(posts.at(-1)!.url, 'https://cam:20443/stok=STOK1');
  assert.deepEqual(posts.at(-1)!.body, { method: 'getStreamPort', params: null });
});

test('parses the stream port from its string form', async () => {
  const session = await openVigiControlWithDependencies(OPTIONS, fakeControl([]));
  assert.equal(await session.getStreamPort(), 554);
});

test('reports a speaker and microphone from getAudioCapability', async () => {
  const session = await openVigiControlWithDependencies(OPTIONS, fakeControl([]));
  assert.deepEqual(await session.getAudioCapability(), {
    speaker: true,
    microphone: true,
  });
});

test('reports no speaker when the response omits it', async () => {
  const session = await openVigiControlWithDependencies(
    OPTIONS,
    fakeControl([], {
      audioCapability: {
        method: 'getAudioCapability',
        result: { microphone: { volume: '1' } },
        errCode: 0,
      },
    }),
  );
  assert.deepEqual(await session.getAudioCapability(), {
    speaker: false,
    microphone: true,
  });
});

test('raises a distinct error for an authentication failure and does not retry', async () => {
  const posts: RecordedPost[] = [];
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl(posts, { auth: { method: 'doAuth', errCode: -10020 } }),
    ),
    (error: unknown) => {
      assert.ok(error instanceof VigiControlError);
      assert.equal(error.code, -10020);
      assert.equal(error.message, 'VIGI OpenAPI authentication failed');
      return true;
    },
  );
  assert.equal(posts.length, 2, 'exactly one doAuth attempt after the challenge');
});

test('raises a distinct error when the account is locked and does not retry', async () => {
  const posts: RecordedPost[] = [];
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl(posts, { auth: { method: 'doAuth', errCode: -10022 } }),
    ),
    (error: unknown) => {
      assert.ok(error instanceof VigiControlError);
      assert.equal(error.code, -10022);
      assert.equal(error.message, 'VIGI OpenAPI account is locked by retry limit');
      return true;
    },
  );
  assert.equal(posts.length, 2);
});

test('raises an unsupported-directive error for -10030', async () => {
  const session = await openVigiControlWithDependencies(
    OPTIONS,
    fakeControl([], { streamPort: { method: 'getStreamPort', errCode: -10030 } }),
  );
  await assert.rejects(session.getStreamPort(), (error: unknown) => {
    assert.ok(error instanceof VigiControlError);
    assert.equal(error.code, -10030);
    assert.equal(error.message, 'VIGI OpenAPI rejected getStreamPort as unsupported');
    return true;
  });
});

test('rejects a challenge missing the authenticate object', async () => {
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], { challenge: { method: 'doAuth', errCode: -10020 } }),
    ),
    { message: 'invalid VIGI doAuth challenge' },
  );
});

test('rejects a challenge whose algorithm is not SHA-256', async () => {
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], {
        challenge: {
          method: 'doAuth',
          authenticate: {
            realm: 'r', nonce: 'n', algorithm: 'MD5', uri: 'doAuth', method: 'POST',
          },
          errCode: -10020,
        },
      }),
    ),
    { message: 'unsupported VIGI doAuth algorithm: MD5' },
  );
});

test('rejects a successful doAuth that carries no stok', async () => {
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], { auth: { method: 'doAuth', errCode: 0 } }),
    ),
    { message: 'VIGI doAuth returned no stok' },
  );
});

test('never puts the password in an error message', async () => {
  await assert.rejects(
    openVigiControlWithDependencies(
      { host: 'cam', pass: 'p@ssw0rd-unique' },
      fakeControl([], { auth: { method: 'doAuth', errCode: -10020 } }),
    ),
    (error: unknown) => {
      assert.ok(!String((error as Error).message).includes('p@ssw0rd-unique'));
      return true;
    },
  );
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --experimental-transform-types --test src/vigi/control.test.ts`
Expected: FAIL — `Cannot find module './control.ts'`.

- [ ] **Step 3: Write `src/vigi/control.ts`**

```ts
/**
 * VIGI OpenAPI control channel.
 *
 * Authenticates over HTTPS and answers the two questions the talk transport
 * needs: which port carries the stream, and whether the camera reports a
 * speaker. Authentication is attempted exactly once — the device keeps a retry
 * counter and locks the account when it is exceeded.
 */
import https from 'node:https';
import crypto from 'node:crypto';

export const DEFAULT_VIGI_CONTROL_PORT = 20443;
const DEFAULT_TIMEOUT_MS = 8000;
const MAX_RESPONSE_BYTES = 1024 * 1024;

/** Device-reported failure. `code` is the device's own errCode. */
export class VigiControlError extends Error {
  constructor(readonly code: number, message: string) {
    super(message);
    this.name = 'VigiControlError';
  }
}

export interface VigiAudioCapability {
  speaker: boolean;
  microphone: boolean;
}

export interface VigiControlSession {
  readonly stok: string;
  getStreamPort(): Promise<number>;
  getAudioCapability(): Promise<VigiAudioCapability>;
}

export interface VigiControlOptions {
  host: string;
  /** The device documents admin as the only OpenAPI account. */
  user?: string;
  pass: string;
  port?: number;
  timeoutMs?: number;
}

export interface VigiControlDependencies {
  postJson(url: string, body: unknown, timeoutMs: number): Promise<unknown>;
}

const sha256 = (value: string) =>
  crypto.createHash('sha256').update(value).digest('hex');

function describeErrorCode(code: number, operation: string): string {
  if (code === -10020) return 'VIGI OpenAPI authentication failed';
  if (code === -10022) return 'VIGI OpenAPI account is locked by retry limit';
  if (code === -10002) return 'VIGI OpenAPI request was unauthorized';
  if (code === -10030) {
    return `VIGI OpenAPI rejected ${operation} as unsupported`;
  }
  return `VIGI OpenAPI ${operation} failed`;
}

function requireSuccess(reply: unknown, operation: string): Record<string, unknown> {
  if (typeof reply !== 'object' || reply === null) {
    throw new Error(`invalid VIGI ${operation} response`);
  }
  const record = reply as Record<string, unknown>;
  const code = typeof record.errCode === 'number' ? record.errCode : 0;
  if (code !== 0) throw new VigiControlError(code, describeErrorCode(code, operation));
  return record;
}

/** Node's global fetch cannot accept the camera's self-signed certificate. */
function postJsonOverHttps(
  url: string,
  body: unknown,
  timeoutMs: number,
): Promise<unknown> {
  const target = new URL(url);
  const payload = Buffer.from(JSON.stringify(body), 'utf8');
  const options: https.RequestOptions = {
    method: 'POST',
    hostname: target.hostname,
    port: target.port || 443,
    path: target.pathname + target.search,
    headers: {
      'Content-Type': 'application/json',
      'Content-Length': payload.byteLength,
    },
    rejectUnauthorized: false,
  };
  return new Promise((resolve, reject) => {
    let settled = false;
    const settle = (error?: Error, value?: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(value);
    };
    const timer = setTimeout(
      () => settle(new Error('VIGI OpenAPI request timeout')),
      timeoutMs,
    );
    const request = https.request(options, (response) => {
      const chunks: Buffer[] = [];
      let bytes = 0;
      response.on('data', (chunk: Buffer) => {
        bytes += chunk.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) {
          settle(new Error('VIGI OpenAPI response too large'));
          response.destroy();
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', () => {
        try {
          settle(undefined, JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch {
          settle(new Error('VIGI OpenAPI response was not JSON'));
        }
      });
      response.on('error', () => settle(new Error('VIGI OpenAPI response error')));
    });
    request.on('error', () => settle(new Error('VIGI OpenAPI request failed')));
    request.end(payload);
  });
}

const defaultDependencies: VigiControlDependencies = { postJson: postJsonOverHttps };

export function openVigiControl(
  options: VigiControlOptions,
): Promise<VigiControlSession> {
  return openVigiControlWithDependencies(options, defaultDependencies);
}

/** @internal */
export async function openVigiControlWithDependencies(
  options: VigiControlOptions,
  dependencies: VigiControlDependencies,
): Promise<VigiControlSession> {
  const user = options.user ?? 'admin';
  const port = options.port ?? DEFAULT_VIGI_CONTROL_PORT;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('timeoutMs must be finite and greater than 0');
  }
  const base = `https://${options.host}:${port}`;

  const challengeReply = await dependencies.postJson(
    base,
    { method: 'doAuth', params: null },
    timeoutMs,
  );
  const authenticate = (challengeReply as { authenticate?: unknown } | null)
    ?.authenticate;
  if (typeof authenticate !== 'object' || authenticate === null) {
    throw new Error('invalid VIGI doAuth challenge');
  }
  const challenge = authenticate as Record<string, unknown>;
  const realm = challenge.realm;
  const nonce = challenge.nonce;
  const uri = challenge.uri;
  const method = challenge.method;
  if (
    typeof realm !== 'string' || typeof nonce !== 'string'
    || typeof uri !== 'string' || typeof method !== 'string'
  ) {
    throw new Error('invalid VIGI doAuth challenge');
  }
  const algorithm = typeof challenge.algorithm === 'string'
    ? challenge.algorithm
    : 'unknown';
  if (algorithm.toUpperCase() !== 'SHA-256') {
    throw new Error(`unsupported VIGI doAuth algorithm: ${algorithm}`);
  }

  const a1 = sha256(`${user}:${realm}:${options.pass}`);
  const a2 = sha256(`${method}:${uri}`);
  const response = sha256(`${a1}:${nonce}:${a2}`);

  // One attempt only. A retry here is what locks the account (-10022).
  const authReply = requireSuccess(
    await dependencies.postJson(
      base,
      { method: 'doAuth', params: { nonce, response } },
      timeoutMs,
    ),
    'doAuth',
  );
  const stok = authReply.stok;
  if (typeof stok !== 'string' || stok.length === 0) {
    throw new Error('VIGI doAuth returned no stok');
  }

  const call = async (
    operation: string,
  ): Promise<Record<string, unknown> | undefined> => {
    const reply = requireSuccess(
      await dependencies.postJson(
        `${base}/stok=${stok}`,
        { method: operation, params: null },
        timeoutMs,
      ),
      operation,
    );
    const result = reply.result;
    return typeof result === 'object' && result !== null
      ? (result as Record<string, unknown>)
      : undefined;
  };

  return {
    stok,
    async getStreamPort(): Promise<number> {
      const result = await call('getStreamPort');
      const raw = result?.streamPort;
      const parsed = typeof raw === 'string' ? Number(raw) : raw;
      if (typeof parsed !== 'number' || !Number.isInteger(parsed) || parsed <= 0) {
        throw new Error('invalid VIGI getStreamPort response');
      }
      return parsed;
    },
    async getAudioCapability(): Promise<VigiAudioCapability> {
      const result = await call('getAudioCapability');
      const present = (key: string): boolean => {
        const value = result?.[key];
        return typeof value === 'object' && value !== null;
      };
      return { speaker: present('speaker'), microphone: present('microphone') };
    },
  };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --experimental-transform-types --test src/vigi/control.test.ts`
Expected: PASS, 13 tests.

- [ ] **Step 5: Commit**

```bash
git add src/vigi/control.ts src/vigi/control.test.ts
git commit -m "feat(vigi): add the OpenAPI control client"
```

---

### Task 3: VIGI talk stream session

The stream side opens a `MULTITRANS` session and then writes interleaved RTP on the same socket. The socket is not opened until the first `send()`, because `playFile` opens the session before a transcode that can take seconds and VIGI documents no keep-alive.

**Files:**
- Create: `src/vigi/talk.ts`
- Create: `src/vigi/talk.test.ts`

**Interfaces:**
- Consumes: `digestAuthorization`, `generateCnonce`, `parseDigestChallenge`, `DigestChallenge` from `src/rtsp/digest.ts` (Task 1). `RtpPacketizer`, `interleave` from `src/rtp/sender.ts`. `sendPacedFrames`, `SAMPLE_RATE`, `type PacingClock` from `src/backchannel.ts`.
- Produces:
  - `VIGI_TALK_PAYLOAD_TYPE = 8`, `VIGI_TALK_CHANNEL = 0`, `VIGI_TALK_PACKET_MS = 20`, `VIGI_TALK_SAMPLES_PER_PACKET = 160`
  - `VigiTalkMode = 'half_duplex' | 'aec'`
  - `VigiTalkOptions { host: string; user?: string; pass: string; streamPort: number; mode?: VigiTalkMode; channel?: number; timeoutMs?: number; clock?: PacingClock }`
  - `VigiTalkSession { readonly payloadType: number; readonly clockRate: number; readonly rtpChannel: number; send(g711: Buffer): Promise<number>; close(): Promise<void> }`
  - `createVigiTalkSession(options: VigiTalkOptions, dependencies?: VigiTalkDependencies): VigiTalkSession`
  - `VigiTalkDependencies { connect(port: number, host: string): VigiTalkSocket }`
  - `VigiTalkSocket` — the narrow slice of `net.Socket` used: `write`, `on`, `end`, `destroy`, `setTimeout`
  - `buildMultitransRequest(input: { uri: string; cseq: number; json: string; authorization?: string }): string`

- [ ] **Step 1: Write the failing tests**

Create `src/vigi/talk.test.ts`:

```ts
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { test } from 'node:test';
import {
  VIGI_TALK_CHANNEL,
  VIGI_TALK_PAYLOAD_TYPE,
  VIGI_TALK_SAMPLES_PER_PACKET,
  buildMultitransRequest,
  createVigiTalkSession,
  type VigiTalkDependencies,
  type VigiTalkSocket,
} from './talk.ts';

const sha256 = (s: string) => crypto.createHash('sha256').update(s).digest('hex');

class FakeSocket implements VigiTalkSocket {
  readonly written: Buffer[] = [];
  ended = false;
  destroyed = false;
  private handlers = new Map<string, Array<(arg?: unknown) => void>>();

  write(chunk: Buffer | string): boolean {
    this.written.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, 'utf8'));
    return true;
  }
  on(event: string, handler: (arg?: unknown) => void): this {
    const list = this.handlers.get(event) ?? [];
    list.push(handler);
    this.handlers.set(event, list);
    return this;
  }
  end(): void { this.ended = true; }
  destroy(): void { this.destroyed = true; }
  setTimeout(): void {}

  emit(event: string, arg?: unknown): void {
    for (const handler of this.handlers.get(event) ?? []) handler(arg);
  }
  /** Text of every non-binary chunk, for asserting on requests. */
  get requests(): string[] {
    return this.written
      .filter((chunk) => chunk[0] !== 0x24)
      .map((chunk) => chunk.toString('utf8'));
  }
  get rtpFrames(): Buffer[] {
    return this.written.filter((chunk) => chunk[0] === 0x24);
  }
}

const CHALLENGE =
  'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n' +
  'WWW-Authenticate: Digest realm="TP-LINK IP-Camera", nonce="n7", ' +
  'algorithm="SHA-256", qop="auth"\r\nContent-Length: 0\r\n\r\n';

const OK =
  'RTSP/1.0 200 OK\r\nCSeq: 2\r\nSession: ABCD\r\nContent-Length: 73\r\n\r\n' +
  '{"type":"response", "seq":1, "params":{"error_code":0, "session_id":"0"}}';

function harness(): { socket: FakeSocket; dependencies: VigiTalkDependencies } {
  const socket = new FakeSocket();
  return {
    socket,
    dependencies: { connect: () => socket },
  };
}

/** Drive the challenge/OK exchange as the session writes each request. */
function autoRespond(socket: FakeSocket): void {
  let replies = 0;
  const original = socket.write.bind(socket);
  socket.write = (chunk: Buffer | string): boolean => {
    const result = original(chunk);
    const text = Buffer.isBuffer(chunk) ? '' : String(chunk);
    if (text.startsWith('MULTITRANS')) {
      replies += 1;
      const reply = replies === 1 ? CHALLENGE : OK;
      queueMicrotask(() => socket.emit('data', Buffer.from(reply, 'binary')));
    }
    return result;
  };
}

const OPTIONS = { host: 'cam', pass: 'secret', streamPort: 554 };
const instantClock = { now: () => 0, sleep: async () => {} };

test('builds a MULTITRANS request with a JSON body and correct Content-Length', () => {
  const json = '{"type":"request"}';
  const request = buildMultitransRequest({
    uri: 'rtsp://cam/multitrans', cseq: 1, json,
  });
  assert.equal(
    request,
    'MULTITRANS rtsp://cam/multitrans RTSP/1.0\r\n'
      + 'CSeq: 1\r\n'
      + 'Content-Type: application/json\r\n'
      + `Content-Length: ${Buffer.byteLength(json)}\r\n`
      + '\r\n'
      + json,
  );
});

test('includes an Authorization header when one is supplied', () => {
  const request = buildMultitransRequest({
    uri: 'rtsp://cam/multitrans', cseq: 2, json: '{}', authorization: 'Digest x',
  });
  assert.match(request, /\r\nAuthorization: Digest x\r\n/);
});

test('opens no socket until the first send', () => {
  let connects = 0;
  const socket = new FakeSocket();
  createVigiTalkSession(OPTIONS, {
    connect: () => { connects += 1; return socket; },
  });
  assert.equal(connects, 0);
});

test('sends the talk request with the documented mode and authenticates with the vigi style', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));

  assert.equal(socket.requests.length, 2);
  assert.match(socket.requests[0], /^MULTITRANS rtsp:\/\/cam\/multitrans RTSP\/1\.0\r\n/);
  assert.match(
    socket.requests[0],
    /\{"type":"request","seq":"1","params":\{"method":"get","talk":\{"mode":"half_duplex"\}\}\}$/,
  );
  assert.ok(!socket.requests[0].includes('Authorization'));

  const authorization = /Authorization: (Digest [^\r]+)\r\n/.exec(socket.requests[1]);
  assert.ok(authorization, 'second request carries Authorization');
  assert.match(authorization[1], /qop="auth"/, 'qop is quoted for VIGI');
  assert.match(authorization[1], /nc="00000001"/, 'nc is quoted for VIGI');
  const a1 = sha256('admin:TP-LINK IP-Camera:secret');
  const a2 = sha256('MULTITRANS:rtsp://cam/multitrans');
  const cnonce = /cnonce="([0-9a-f]+)"/.exec(authorization[1])![1];
  assert.match(
    authorization[1],
    new RegExp(`response="${sha256(`${a1}:n7:00000001:${cnonce}:auth:${a2}`)}"`),
  );
});

test('honours an explicit aec mode', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSession(
    { ...OPTIONS, mode: 'aec', clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  assert.match(socket.requests[0], /"mode":"aec"/);
});

test('frames audio as interleaved RTP with payload type 8 on channel 0', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  const sent = await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET * 3, 0xd5));

  assert.equal(sent, 3);
  const frames = socket.rtpFrames;
  assert.equal(frames.length, 3);
  for (const frame of frames) {
    assert.equal(frame[0], 0x24);
    assert.equal(frame[1], VIGI_TALK_CHANNEL);
    assert.equal(frame.readUInt16BE(2), 12 + VIGI_TALK_SAMPLES_PER_PACKET);
    assert.equal(frame[4], 0x80, 'RTP version 2');
    assert.equal(frame[5] & 0x7f, VIGI_TALK_PAYLOAD_TYPE);
  }
  assert.equal(frames[0][5] & 0x80, 0x80, 'marker set on the first packet');
  assert.equal(frames[1][5] & 0x80, 0x00, 'marker clear afterwards');
  assert.equal(
    frames[1].readUInt32BE(8) - frames[0].readUInt32BE(8),
    VIGI_TALK_SAMPLES_PER_PACKET,
    'timestamp advances by one frame of samples',
  );
});

test('a short trailing frame is still sent', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  const sent = await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET + 40, 0xd5));
  assert.equal(sent, 2);
  assert.equal(socket.rtpFrames[1].readUInt16BE(2), 12 + 40);
});

test('reuses one talk session across successive sends', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  assert.equal(socket.requests.length, 2, 'handshake happened once');
  assert.equal(socket.rtpFrames.length, 2);
});

test('surfaces a device error_code from the talk reply', async () => {
  const socket = new FakeSocket();
  let replies = 0;
  const original = socket.write.bind(socket);
  socket.write = (chunk: Buffer | string): boolean => {
    const result = original(chunk);
    if (!Buffer.isBuffer(chunk) && String(chunk).startsWith('MULTITRANS')) {
      replies += 1;
      const body = '{"type":"response","seq":1,"params":{"error_code":-52410}}';
      const reply = replies === 1
        ? CHALLENGE
        : `RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Length: ${body.length}\r\n\r\n${body}`;
      queueMicrotask(() => socket.emit('data', Buffer.from(reply, 'binary')));
    }
    return result;
  };
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    { connect: () => socket },
  );
  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    { message: 'VIGI talk refused: error_code -52410' },
  );
});

test('surfaces a non-200 MULTITRANS status', async () => {
  const socket = new FakeSocket();
  let replies = 0;
  const original = socket.write.bind(socket);
  socket.write = (chunk: Buffer | string): boolean => {
    const result = original(chunk);
    if (!Buffer.isBuffer(chunk) && String(chunk).startsWith('MULTITRANS')) {
      replies += 1;
      const reply = replies === 1
        ? CHALLENGE
        : 'RTSP/1.0 401 Unauthorized\r\nCSeq: 2\r\nContent-Length: 0\r\n\r\n';
      queueMicrotask(() => socket.emit('data', Buffer.from(reply, 'binary')));
    }
    return result;
  };
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    { connect: () => socket },
  );
  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    { message: 'VIGI talk MULTITRANS failed: RTSP/1.0 401 Unauthorized' },
  );
});

test('close ends an opened socket and is safe before any send', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const unopened = createVigiTalkSession(OPTIONS, { connect: () => new FakeSocket() });
  await unopened.close();

  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  await session.close();
  assert.equal(socket.ended, true);
});

test('send after close is rejected', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSession(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.close();
  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    { message: 'VIGI talk session is closed' },
  );
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --experimental-transform-types --test src/vigi/talk.test.ts`
Expected: FAIL — `Cannot find module './talk.ts'`.

- [ ] **Step 3: Write `src/vigi/talk.ts`**

```ts
/**
 * VIGI OpenAPI talk stream.
 *
 * One MULTITRANS request opens an audio-send session on an RTSP-framed
 * connection; audio then flows on the same socket as interleaved RTP over TCP.
 * The socket is opened on the first send, not at construction: the caller
 * typically transcodes a file between opening a session and sending it, and no
 * keep-alive is defined for a talk session.
 */
import net from 'node:net';
import { SAMPLE_RATE, sendPacedFrames, type PacingClock } from '../backchannel.ts';
import { RtpPacketizer, interleave } from '../rtp/sender.ts';
import {
  digestAuthorization,
  generateCnonce,
  parseDigestChallenge,
  type DigestChallenge,
} from '../rtsp/digest.ts';

/** PCMA. The device supports G.711 only. */
export const VIGI_TALK_PAYLOAD_TYPE = 8;
/** The talk reply carries no interleaved_id; channel 0 is what works. */
export const VIGI_TALK_CHANNEL = 0;
export const VIGI_TALK_PACKET_MS = 20;
export const VIGI_TALK_SAMPLES_PER_PACKET = (SAMPLE_RATE * VIGI_TALK_PACKET_MS) / 1000;

const DEFAULT_TIMEOUT_MS = 8000;

export type VigiTalkMode = 'half_duplex' | 'aec';

/** The slice of net.Socket this module uses, so tests can supply a double. */
export interface VigiTalkSocket {
  write(chunk: Buffer | string): boolean;
  on(event: string, handler: (arg?: never) => void): unknown;
  end(): void;
  destroy(): void;
  setTimeout(ms: number, handler?: () => void): unknown;
}

export interface VigiTalkDependencies {
  connect(port: number, host: string): VigiTalkSocket;
}

export interface VigiTalkOptions {
  host: string;
  user?: string;
  pass: string;
  streamPort: number;
  mode?: VigiTalkMode;
  channel?: number;
  timeoutMs?: number;
  clock?: PacingClock;
}

export interface VigiTalkSession {
  readonly payloadType: number;
  readonly clockRate: number;
  readonly rtpChannel: number;
  /** Stream G.711 a-law bytes in real time. Resolves with packets sent. */
  send(g711: Buffer): Promise<number>;
  close(): Promise<void>;
}

export function buildMultitransRequest(input: {
  uri: string;
  cseq: number;
  json: string;
  authorization?: string;
}): string {
  const lines = [
    `MULTITRANS ${input.uri} RTSP/1.0`,
    `CSeq: ${input.cseq}`,
    'Content-Type: application/json',
    `Content-Length: ${Buffer.byteLength(input.json)}`,
  ];
  if (input.authorization) lines.push(`Authorization: ${input.authorization}`);
  return `${lines.join('\r\n')}\r\n\r\n${input.json}`;
}

interface RtspReply {
  statusLine: string;
  status: number;
  headers: string;
  body: string;
}

function parseReply(raw: string): RtspReply | undefined {
  const split = raw.indexOf('\r\n\r\n');
  if (split < 0) return undefined;
  const headers = raw.slice(0, split);
  const statusLine = headers.split('\r\n')[0];
  const lengthMatch = /\r\nContent-Length:\s*(\d+)/i.exec(headers);
  const length = lengthMatch ? Number(lengthMatch[1]) : 0;
  const body = raw.slice(split + 4);
  if (Buffer.byteLength(body, 'binary') < length) return undefined;
  const status = Number(/^RTSP\/\d\.\d\s+(\d+)/.exec(statusLine)?.[1] ?? 0);
  return { statusLine, status, headers, body: body.slice(0, length) };
}

const defaultDependencies: VigiTalkDependencies = {
  connect: (port, host) => net.connect(port, host) as unknown as VigiTalkSocket,
};

export function createVigiTalkSession(
  options: VigiTalkOptions,
  dependencies: VigiTalkDependencies = defaultDependencies,
): VigiTalkSession {
  const user = options.user ?? 'admin';
  const channel = options.channel ?? VIGI_TALK_CHANNEL;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const uri = `rtsp://${options.host}/multitrans`;
  const talkJson = JSON.stringify({
    type: 'request',
    seq: '1',
    params: { method: 'get', talk: { mode: options.mode ?? 'half_duplex' } },
  });

  let socket: VigiTalkSocket | undefined;
  let packetizer: RtpPacketizer | undefined;
  let closed = false;
  let opening: Promise<VigiTalkSocket> | undefined;

  function open(): Promise<VigiTalkSocket> {
    if (opening) return opening;
    opening = new Promise<VigiTalkSocket>((resolve, reject) => {
      const connection = dependencies.connect(options.streamPort, options.host);
      let buffer = '';
      let cseq = 1;
      let challenge: DigestChallenge | undefined;
      let settled = false;

      const settle = (error?: Error): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) {
          connection.destroy();
          reject(error);
        } else {
          socket = connection;
          resolve(connection);
        }
      };
      const timer = setTimeout(
        () => settle(new Error('VIGI talk MULTITRANS timeout')),
        timeoutMs,
      );

      const sendRequest = (authorization?: string): void => {
        connection.write(buildMultitransRequest({ uri, cseq, json: talkJson, authorization }));
      };

      connection.on('error', () => settle(new Error('VIGI talk connection failed')));
      connection.on('data', ((chunk: Buffer) => {
        buffer += chunk.toString('binary');
        const reply = parseReply(buffer);
        if (!reply) return;
        buffer = '';

        if (reply.status === 401 && !challenge) {
          const header = /\r\nWWW-Authenticate:\s*(Digest [^\r]+)/i.exec(reply.headers);
          if (!header) {
            settle(new Error('VIGI talk MULTITRANS failed: no Digest challenge'));
            return;
          }
          const parsed = parseDigestChallenge(header[1]);
          if (parsed === undefined || parsed === 'basic') {
            settle(new Error('VIGI talk MULTITRANS failed: no Digest challenge'));
            return;
          }
          challenge = parsed;
          cseq += 1;
          sendRequest(
            digestAuthorization({
              user,
              pass: options.pass,
              method: 'MULTITRANS',
              uri,
              challenge: parsed,
              nonceCount: 1,
              cnonce: generateCnonce(),
              style: 'vigi',
            }),
          );
          return;
        }

        if (reply.status !== 200) {
          settle(new Error(`VIGI talk MULTITRANS failed: ${reply.statusLine}`));
          return;
        }
        let code = 0;
        try {
          const parsed = JSON.parse(reply.body) as {
            params?: { error_code?: number };
          };
          code = parsed.params?.error_code ?? 0;
        } catch {
          settle(new Error('VIGI talk reply was not JSON'));
          return;
        }
        if (code !== 0) {
          settle(new Error(`VIGI talk refused: error_code ${code}`));
          return;
        }
        settle();
      }) as (arg?: never) => void);

      sendRequest();
    });
    return opening;
  }

  return {
    payloadType: VIGI_TALK_PAYLOAD_TYPE,
    clockRate: SAMPLE_RATE,
    rtpChannel: channel,

    async send(g711: Buffer): Promise<number> {
      if (closed) throw new Error('VIGI talk session is closed');
      const connection = await open();
      packetizer ??= new RtpPacketizer({
        payloadType: VIGI_TALK_PAYLOAD_TYPE,
        clockRate: SAMPLE_RATE,
      });
      const rtp = packetizer;
      function* frames() {
        for (
          let offset = 0;
          offset < g711.length;
          offset += VIGI_TALK_SAMPLES_PER_PACKET
        ) {
          const payload = g711.subarray(offset, offset + VIGI_TALK_SAMPLES_PER_PACKET);
          yield { payload, samples: payload.length };
        }
      }
      return sendPacedFrames(
        frames(),
        SAMPLE_RATE,
        (payload) => {
          connection.write(interleave(channel, rtp.build(payload, payload.length)));
        },
        options.clock,
      );
    },

    async close(): Promise<void> {
      if (closed) return;
      closed = true;
      socket?.end();
      socket = undefined;
    },
  };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --experimental-transform-types --test src/vigi/talk.test.ts`
Expected: PASS, 13 tests.

If `sendPacedFrames`'s signature rejects an optional `clock`, pass
`options.clock ?? systemClock` and export `systemClock` from
`src/backchannel.ts` — do not change `sendPacedFrames`'s default.

- [ ] **Step 5: Commit**

```bash
git add src/vigi/talk.ts src/vigi/talk.test.ts
git commit -m "feat(vigi): add the OpenAPI talk stream session"
```

---

### Task 4: Transport selection in openBackchannel

`openBackchannel` becomes the selector. The ONVIF body moves into its own function unchanged, and the "no backchannel" condition becomes a typed error so the fallback can key on it instead of matching a message string.

**Files:**
- Modify: `src/backchannel.ts:101-118` (`BackchannelSession`), `:116-118` (`BackchannelOptions`), `:329-...` (`openBackchannel`)
- Modify: `src/vigi/talk.ts` (no change needed — consumed as-is)
- Test: `src/backchannel.test.ts` (add cases; keep existing ones passing)

**Interfaces:**
- Consumes: `openVigiControl` / `VigiControlError` / `DEFAULT_VIGI_CONTROL_PORT` from Task 2; `createVigiTalkSession` from Task 3.
- Produces:
  - `BackchannelTransport = 'auto' | 'onvif' | 'vigi'`
  - `class BackchannelUnavailableError extends Error { readonly kind: 'no-sendonly-track' }` — message stays exactly `no sendonly backchannel audio track`
  - `BackchannelOptions { codec?: CodecPreference; transport?: BackchannelTransport; vigiControlPort?: number }`
  - `openOnvifBackchannel(host, user, pass, options): Promise<BackchannelSession>`
  - `openVigiBackchannel(host, user, pass, options): Promise<BackchannelSession>`
  - `openBackchannel` keeps its existing four-argument shape.

- [ ] **Step 1: Write the failing tests**

Append to `src/backchannel.test.ts`:

```ts
import {
  BackchannelUnavailableError,
  selectBackchannelTransport,
} from './backchannel.ts';

test('BackchannelUnavailableError keeps the historical message', () => {
  const error = new BackchannelUnavailableError();
  assert.equal(error.message, 'no sendonly backchannel audio track');
  assert.equal(error.kind, 'no-sendonly-track');
});

test('onvif transport returns the ONVIF session and never probes VIGI', async () => {
  const calls: string[] = [];
  const session = await selectBackchannelTransport('onvif', {
    openOnvif: async () => { calls.push('onvif'); return 'onvif-session' as never; },
    openVigi: async () => { calls.push('vigi'); return 'vigi-session' as never; },
  });
  assert.equal(session, 'onvif-session');
  assert.deepEqual(calls, ['onvif']);
});

test('vigi transport returns the VIGI session and never tries ONVIF', async () => {
  const calls: string[] = [];
  const session = await selectBackchannelTransport('vigi', {
    openOnvif: async () => { calls.push('onvif'); return 'onvif-session' as never; },
    openVigi: async () => { calls.push('vigi'); return 'vigi-session' as never; },
  });
  assert.equal(session, 'vigi-session');
  assert.deepEqual(calls, ['vigi']);
});

test('auto prefers ONVIF when a backchannel exists', async () => {
  const calls: string[] = [];
  const session = await selectBackchannelTransport('auto', {
    openOnvif: async () => { calls.push('onvif'); return 'onvif-session' as never; },
    openVigi: async () => { calls.push('vigi'); return 'vigi-session' as never; },
  });
  assert.equal(session, 'onvif-session');
  assert.deepEqual(calls, ['onvif'], 'VIGI is not probed when ONVIF succeeds');
});

test('auto falls back to VIGI only on the no-sendonly-track condition', async () => {
  const calls: string[] = [];
  const session = await selectBackchannelTransport('auto', {
    openOnvif: async () => {
      calls.push('onvif');
      throw new BackchannelUnavailableError();
    },
    openVigi: async () => { calls.push('vigi'); return 'vigi-session' as never; },
  });
  assert.equal(session, 'vigi-session');
  assert.deepEqual(calls, ['onvif', 'vigi']);
});

test('auto propagates a non-backchannel ONVIF failure without probing VIGI', async () => {
  const calls: string[] = [];
  await assert.rejects(
    selectBackchannelTransport('auto', {
      openOnvif: async () => {
        calls.push('onvif');
        throw new Error('ONVIF connect failed: request failed');
      },
      openVigi: async () => { calls.push('vigi'); return 'vigi-session' as never; },
    }),
    { message: 'ONVIF connect failed: request failed' },
  );
  assert.deepEqual(calls, ['onvif'], 'a real fault must not be masked by a VIGI attempt');
});

test('auto reports both attempts when VIGI also fails', async () => {
  await assert.rejects(
    selectBackchannelTransport('auto', {
      openOnvif: async () => { throw new BackchannelUnavailableError(); },
      openVigi: async () => { throw new Error('VIGI OpenAPI port 20443 unreachable'); },
    }),
    {
      message:
        'no audio send path: ONVIF backchannel absent (no sendonly track); '
        + 'VIGI OpenAPI port 20443 unreachable',
    },
  );
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --experimental-transform-types --test src/backchannel.test.ts`
Expected: FAIL — `selectBackchannelTransport` and `BackchannelUnavailableError` are not exported.

- [ ] **Step 3: Add the error, the selector, and the VIGI opener to `src/backchannel.ts`**

```ts
export type BackchannelTransport = 'auto' | 'onvif' | 'vigi';

/**
 * The camera answered a backchannel DESCRIBE but offered no sendonly audio
 * track. This is the one ONVIF outcome that `transport: 'auto'` treats as
 * "try the other transport"; every other failure propagates, so a network or
 * authentication fault is never reported as a missing vendor API.
 */
export class BackchannelUnavailableError extends Error {
  readonly kind = 'no-sendonly-track' as const;
  constructor() {
    super('no sendonly backchannel audio track');
    this.name = 'BackchannelUnavailableError';
  }
}

export interface BackchannelOptions {
  codec?: CodecPreference;
  transport?: BackchannelTransport;
  /** VIGI OpenAPI control port. Defaults to 20443. */
  vigiControlPort?: number;
}

/** @internal Exported for tests; the transport openers are injected. */
export async function selectBackchannelTransport(
  transport: BackchannelTransport,
  openers: {
    openOnvif(): Promise<BackchannelSession>;
    openVigi(): Promise<BackchannelSession>;
  },
): Promise<BackchannelSession> {
  if (transport === 'onvif') return openers.openOnvif();
  if (transport === 'vigi') return openers.openVigi();
  try {
    return await openers.openOnvif();
  } catch (error) {
    if (!(error instanceof BackchannelUnavailableError)) throw error;
    try {
      return await openers.openVigi();
    } catch (vigiError) {
      const detail = vigiError instanceof Error ? vigiError.message : String(vigiError);
      throw new Error(
        'no audio send path: ONVIF backchannel absent (no sendonly track); '
          + detail,
      );
    }
  }
}
```

In the existing ONVIF body, replace
`throw new Error('no sendonly backchannel audio track')` with
`throw new BackchannelUnavailableError()`, rename the function to
`openOnvifBackchannel`, and add:

```ts
export async function openVigiBackchannel(
  host: string,
  user = '',
  pass = '',
  options: BackchannelOptions = {},
): Promise<BackchannelSession> {
  const preference = options.codec ?? 'auto';
  if (preference !== 'auto' && preference !== 'pcma') {
    throw new Error(`VIGI talk supports G.711 a-law only, not ${preference}`);
  }
  const control = await openVigiControl({
    host,
    user: user || 'admin',
    pass,
    ...(options.vigiControlPort === undefined
      ? {}
      : { port: options.vigiControlPort }),
  });
  const audio = await control.getAudioCapability();
  if (!audio.speaker) throw new Error('VIGI OpenAPI reports no speaker');
  const streamPort = await control.getStreamPort();
  const talk = createVigiTalkSession({
    host, user: user || 'admin', pass, streamPort,
  });

  const codec: SendCodec = {
    name: 'pcma',
    payloadType: VIGI_TALK_PAYLOAD_TYPE,
    encoding: 'PCMA',
    clockRate: SAMPLE_RATE,
  };
  return {
    codec,
    variant: 'PCMA',
    payloadType: talk.payloadType,
    clockRate: talk.clockRate,
    rtpChannel: talk.rtpChannel,
    // No keep-alive exists for a talk session, and none is needed: the stream
    // socket is not opened until the first send.
    withKeepAlive: (operation) => operation(),
    send: (audioData) =>
      talk.send(Buffer.isBuffer(audioData) ? audioData : audioData.data),
    close: () => talk.close(),
  };
}

export function openBackchannel(
  host: string,
  user = '',
  pass = '',
  options: BackchannelOptions = {},
): Promise<BackchannelSession> {
  return selectBackchannelTransport(options.transport ?? 'auto', {
    openOnvif: () => openOnvifBackchannel(host, user, pass, options),
    openVigi: () => openVigiBackchannel(host, user, pass, options),
  });
}
```

If `EncodedAudio` does not expose a single `data` buffer, concatenate its
frames' payloads before calling `talk.send` — check `src/audio/transcode.ts`
for the actual field and use it; do not add a field.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npm run typecheck && npm test`
Expected: typecheck clean; the seven new selection tests pass and every existing
test still passes — in particular any test asserting the
`no sendonly backchannel audio track` message, which is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/backchannel.ts src/backchannel.test.ts
git commit -m "feat(backchannel): select between ONVIF and VIGI audio-send transports"
```

---

### Task 5: playFile option and CLI flag

**Files:**
- Modify: `src/cli.ts:75-82` (`PlaybackOptions`), `:235-248` (`playFile`), and the `play` command's argument parsing
- Modify: `src/index.ts` (exports)
- Test: `src/cli.test.ts`

**Interfaces:**
- Consumes: `BackchannelTransport`, `BackchannelOptions` from Task 4.
- Produces: `PlaybackOptions.transport?: BackchannelTransport`; CLI flag `--transport`.

- [ ] **Step 1: Write the failing tests**

Append to `src/cli.test.ts`:

```ts
test('playFile passes the transport through to openBackchannel', async () => {
  const seen: Array<Record<string, unknown>> = [];
  await playFile(
    { host: 'cam', file: '/tmp/a.wav', transport: 'vigi' },
    {
      openBackchannel: async (_h, _u, _p, options) => {
        seen.push(options as Record<string, unknown>);
        return {
          codec: { name: 'pcma', payloadType: 8, encoding: 'PCMA', clockRate: 8000 },
          payloadType: 8, clockRate: 8000, rtpChannel: 0,
          send: async () => 1,
          close: async () => {},
        } as never;
      },
      fileToG711: async () => Buffer.alloc(160),
      fileToRtpAudio: async () => ({
        data: Buffer.alloc(160), sampleCount: 160, clockRate: 8000, byteLength: 160,
        frames: [],
      }) as never,
      log: () => {},
    },
  );
  assert.equal(seen[0].transport, 'vigi');
});

test('playFile defaults the transport to auto', async () => {
  const seen: Array<Record<string, unknown>> = [];
  await playFile(
    { host: 'cam', file: '/tmp/a.wav' },
    {
      openBackchannel: async (_h, _u, _p, options) => {
        seen.push(options as Record<string, unknown>);
        return {
          codec: { name: 'pcma', payloadType: 8, encoding: 'PCMA', clockRate: 8000 },
          payloadType: 8, clockRate: 8000, rtpChannel: 0,
          send: async () => 1,
          close: async () => {},
        } as never;
      },
      fileToG711: async () => Buffer.alloc(160),
      fileToRtpAudio: async () => ({
        data: Buffer.alloc(160), sampleCount: 160, clockRate: 8000, byteLength: 160,
        frames: [],
      }) as never,
      log: () => {},
    },
  );
  assert.equal(seen[0].transport, 'auto');
});

test('the play command rejects an unknown --transport value', async () => {
  await assert.rejects(
    runCommand(['play', '--host', 'cam', '--file', '/tmp/a.wav', '--transport', 'nope'],
      stubCommandDependencies()),
    { message: 'invalid --transport value' },
  );
});
```

Reuse whatever `runCommand` / stub-dependency helper `src/cli.test.ts` already
defines; if it names them differently, use its names.

- [ ] **Step 2: Run to verify failure**

Run: `node --experimental-transform-types --test src/cli.test.ts`
Expected: FAIL — `transport` is not a `PlaybackOptions` property.

- [ ] **Step 3: Implement**

In `src/cli.ts`:

```ts
export interface PlaybackOptions {
  host: string;
  user?: string;
  pass?: string;
  file: string;
  volume?: number;
  codec?: CodecPreference;
  transport?: BackchannelTransport;
}
```

In `playFile`, add `transport = 'auto'` to the destructuring and pass it:

```ts
  const session = await dependencies.openBackchannel(host, user, pass, {
    codec,
    transport,
  });
```

In the `play` command parsing, beside the existing `--codec` handling:

```ts
    const transportArg = arg(commandArgs, 'transport', 'auto');
    if (transportArg !== 'auto' && transportArg !== 'onvif' && transportArg !== 'vigi') {
      throw new Error('invalid --transport value');
    }
```

and include `transport: transportArg` in the `playFile` options. Add
`--transport auto|onvif|vigi` to the `HELP` string at `src/cli.ts:23`.

In `src/index.ts`, extend the backchannel export block:

```ts
export {
  PACKET_MS,
  SAMPLE_RATE,
  BackchannelUnavailableError,
  openBackchannel,
  openOnvifBackchannel,
  openVigiBackchannel,
  sendPacedFrames,
  sendPacedG711,
} from './backchannel.ts';
export type {
  BackchannelOptions,
  BackchannelSession,
  BackchannelTransport,
  PacingClock,
} from './backchannel.ts';
export { openVigiControl, VigiControlError } from './vigi/control.ts';
export type {
  VigiAudioCapability,
  VigiControlOptions,
  VigiControlSession,
} from './vigi/control.ts';
export { createVigiTalkSession } from './vigi/talk.ts';
export type { VigiTalkMode, VigiTalkOptions, VigiTalkSession } from './vigi/talk.ts';
```

Add `"dist/vigi"` to the `files` array in `package.json` so the new directory
ships.

- [ ] **Step 4: Run to verify passing**

Run: `npm run typecheck && npm test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/cli.ts src/cli.test.ts src/index.ts package.json
git commit -m "feat(cli): add --transport to select the audio-send transport"
```

---

### Task 6: audioSend in the capability report

**Files:**
- Modify: `src/onvif/capabilities.ts:58-79` (`CameraCapabilityReport`) and the report assembly
- Test: `src/onvif/capabilities.test.ts`

**Interfaces:**
- Consumes: `openVigiControl` (Task 2).
- Produces: `CameraCapabilityReport.audioSend` and
  `CameraCapabilityAudioSend { detected: boolean | null; transport: 'onvif' | 'vigi' | null; onvifBackchannel: boolean | null; vigiTalk: boolean | null }`, plus
  `CameraCapabilityOptions.probeAudioSend?: boolean` (default `true`) and the injected probes on the existing dependencies object.

- [ ] **Step 1: Write the failing tests**

Append to `src/onvif/capabilities.test.ts`:

```ts
test('reports an ONVIF backchannel as the audio-send transport', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => true,
    probeVigiTalk: async () => { throw new Error('must not be probed'); },
  });
  assert.deepEqual(report.audioSend, {
    detected: true, transport: 'onvif', onvifBackchannel: true, vigiTalk: null,
  });
});

test('falls through to VIGI when no sendonly track is offered', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => false,
    probeVigiTalk: async () => true,
  });
  assert.deepEqual(report.audioSend, {
    detected: true, transport: 'vigi', onvifBackchannel: false, vigiTalk: true,
  });
});

test('reports no audio-send path when neither transport answers', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => false,
    probeVigiTalk: async () => false,
  });
  assert.deepEqual(report.audioSend, {
    detected: false, transport: null, onvifBackchannel: false, vigiTalk: false,
  });
});

test('a failed probe warns and leaves the fact null instead of failing the report', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => { throw new Error('describe blew up'); },
    probeVigiTalk: async () => false,
  });
  assert.equal(report.audioSend.onvifBackchannel, null);
  assert.equal(report.audioSend.detected, false);
  assert.ok(report.warnings.some((w) => w.operation === 'AudioSendProbe'));
});

test('probeAudioSend false leaves every audioSend fact null and runs no probe', async () => {
  let probed = false;
  const report = await capabilityReportWithProbes(
    {
      probeOnvifBackchannel: async () => { probed = true; return true; },
      probeVigiTalk: async () => { probed = true; return true; },
    },
    { probeAudioSend: false },
  );
  assert.equal(probed, false);
  assert.deepEqual(report.audioSend, {
    detected: null, transport: null, onvifBackchannel: null, vigiTalk: null,
  });
});
```

Write the `capabilityReportWithProbes` helper beside the file's existing
fixture helpers, building on whatever fake-device helper
`src/onvif/capabilities.test.ts` already uses, and passing the two probes on
the dependencies object.

- [ ] **Step 2: Run to verify failure**

Run: `node --experimental-transform-types --test src/onvif/capabilities.test.ts`
Expected: FAIL — `audioSend` does not exist on the report.

- [ ] **Step 3: Implement**

Add to the report type:

```ts
export interface CameraCapabilityAudioSend {
  detected: boolean | null;
  transport: 'onvif' | 'vigi' | null;
  onvifBackchannel: boolean | null;
  vigiTalk: boolean | null;
}
```

and `audioSend: CameraCapabilityAudioSend;` to `CameraCapabilityReport`, placed
after `media2` and before `warnings`.

Assemble it as optional enrichment, after the ONVIF facts are collected so the
device is already authenticated:

```ts
  const audioSend: CameraCapabilityAudioSend = {
    detected: null, transport: null, onvifBackchannel: null, vigiTalk: null,
  };
  if (options.probeAudioSend ?? true) {
    try {
      audioSend.onvifBackchannel = await dependencies.probeOnvifBackchannel();
    } catch (error) {
      warn('AudioSendProbe', error);
    }
    if (audioSend.onvifBackchannel === true) {
      audioSend.detected = true;
      audioSend.transport = 'onvif';
    } else {
      try {
        audioSend.vigiTalk = await dependencies.probeVigiTalk();
      } catch (error) {
        warn('AudioSendProbe', error);
      }
      if (audioSend.vigiTalk === true) {
        audioSend.detected = true;
        audioSend.transport = 'vigi';
      } else if (audioSend.onvifBackchannel !== null || audioSend.vigiTalk !== null) {
        audioSend.detected = false;
      }
    }
  }
```

Use the module's existing `warn` helper and warning shape — match how
`GetServices` warnings are already recorded so the `operation` field is
consistent.

The default `probeVigiTalk` implementation calls `openVigiControl` and returns
`(await control.getAudioCapability()).speaker`. The default
`probeOnvifBackchannel` performs the backchannel `DESCRIBE` and reports whether
a sendonly audio track is present, reusing `findBackchannelAudio` from
`src/rtsp/sdp.ts`.

- [ ] **Step 4: Run to verify passing**

Run: `npm run typecheck && npm test`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/onvif/capabilities.ts src/onvif/capabilities.test.ts
git commit -m "feat(capabilities): report which transport can send audio"
```

---

### Task 7: audiocheck, documentation, and the trademark notice

`src/audiocheck.ts` currently concludes that two-way audio is impossible on a
camera where it works. Fix that, then document the feature.

**Files:**
- Modify: `src/audiocheck.ts`
- Modify: `README.md`, `README.ko.md`, `python/README.md`, `rust/README.md` (English text in the three English READMEs, Korean in `README.ko.md`)
- Modify: `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`

- [ ] **Step 1: Update `audiocheck` to report both transports**

After the existing ONVIF section, add a VIGI section that calls
`openVigiControl` and prints `getAudioCapability`, then make the 결론 block
report the transport rather than a bare yes/no:

```ts
  console.log('\n=== 결론 ===');
  console.log(`  마이크(수신)        : ${yn(micSupported)}`);
  console.log(`  스피커 출력(송출)   : ${yn(onvifOutput || vigiSpeaker)}`);
  console.log(`  사용 가능한 전송    : ${transportLabel}`);
```

where `transportLabel` is `ONVIF 백채널`, `VIGI OpenAPI talk`, or `없음`. A
failed VIGI probe prints a note and leaves the label unchanged — this is a
diagnostic script and must never throw on a camera that simply has no OpenAPI.

- [ ] **Step 2: Run the script against a camera with no OpenAPI to confirm it does not throw**

Run: `ONVIF_PASSWORD='<password>' node --experimental-transform-types src/audiocheck.ts --host <onvif-only-camera> --user admin`
Expected: completes, prints `사용 가능한 전송`, no stack trace.

- [ ] **Step 3: Document the transport in all four READMEs**

Add a `### Audio Send Transports` section after the existing backchannel API
section. It must state, in your own words:

- `transport` accepts `'auto'` (default), `'onvif'`, `'vigi'`.
- `'auto'` tries ONVIF and falls back to VIGI only when the camera offers no sendonly track; every other failure propagates.
- VIGI requires OpenAPI enabled on the camera at Settings > Network Settings > OpenAPI, and the control port defaults to 20443.
- VIGI carries G.711 a-law only; an explicit non-G.711 codec is rejected.
- The library never changes the device speaker volume; the device default is 80, which is loud indoors, and it is set in the camera's own UI.
- VIGI support is model-dependent — link TP-Link's maintained device list at `https://www.tp-link.com/en/vigi-open-api/product-list/` rather than claiming every VIGI camera. VIGI NVRs use a different protocol and are not supported.

- [ ] **Step 4: Add the trademark notice**

Append to `THIRD_PARTY_NOTICES.md`:

```markdown
## Trademarks

TP-Link and VIGI are trademarks of TP-Link Systems Inc. This project is not
affiliated with, endorsed by, or sponsored by TP-Link. The VIGI OpenAPI
transport is an independent implementation written from TP-Link's publicly
published protocol documentation; no TP-Link code or documentation is
redistributed here.
```

- [ ] **Step 5: Add the CHANGELOG entry**

Under `## [Unreleased]`, in an `### Added` block:

```markdown
- A second audio-send transport: TP-Link's VIGI OpenAPI `talk` protocol, for
  cameras that have a speaker but expose no ONVIF backchannel. Select it with
  `transport: 'onvif' | 'vigi' | 'auto'` (`--transport` on the CLI); the default
  `'auto'` tries ONVIF first and falls back only when the camera offers no
  sendonly audio track, so a network or authentication fault is never reported
  as a missing vendor API. The capability report gains an `audioSend` block
  naming the transport that can reach a given camera.
```

- [ ] **Step 6: Verify and commit**

Run: `npm run typecheck && npm test`

```bash
git add src/audiocheck.ts README.md README.ko.md python/README.md rust/README.md THIRD_PARTY_NOTICES.md CHANGELOG.md
git commit -m "docs: document the VIGI audio-send transport and its constraints"
```

---

### Task 8: Cross-language request parity fixture

The Python and Rust plans assert against this file, so the three ports send
byte-identical VIGI requests. It is created here because TypeScript lands first.

**Files:**
- Create: `rust/tests/fixtures/vigi-request-parity.json`
- Modify: `src/vigi/talk.test.ts` (consume the fixture)

**Interfaces:**
- Consumes: `buildMultitransRequest` (Task 3), `digestAuthorization` (Task 1).
- Produces: the fixture, whose fields the sibling plans read.

- [ ] **Step 1: Write the fixture**

```json
{
  "host": "cam",
  "user": "admin",
  "pass": "secret",
  "streamPort": 554,
  "controlPort": 20443,
  "challenge": {
    "realm": "TP-LINK IP-Camera",
    "nonce": "n7",
    "algorithm": "SHA-256",
    "qop": "auth",
    "uri": "doAuth",
    "method": "POST"
  },
  "cnonce": "aaaabbbbccccdddd",
  "nonceCount": 1,
  "requests": {
    "doAuthChallenge": "{\"method\":\"doAuth\",\"params\":null}",
    "doAuthResponse": "{\"method\":\"doAuth\",\"params\":{\"nonce\":\"n7\",\"response\":\"<sha256>\"}}",
    "talkBody": "{\"type\":\"request\",\"seq\":\"1\",\"params\":{\"method\":\"get\",\"talk\":{\"mode\":\"half_duplex\"}}}",
    "multitransUnauthenticated": "MULTITRANS rtsp://cam/multitrans RTSP/1.0\r\nCSeq: 1\r\nContent-Type: application/json\r\nContent-Length: 78\r\n\r\n{\"type\":\"request\",\"seq\":\"1\",\"params\":{\"method\":\"get\",\"talk\":{\"mode\":\"half_duplex\"}}}",
    "authorizationHeader": "Digest username=\"admin\", realm=\"TP-LINK IP-Camera\", nonce=\"n7\", uri=\"rtsp://cam/multitrans\", qop=\"auth\", cnonce=\"aaaabbbbccccdddd\", nc=\"00000001\", response=\"<sha256>\""
  },
  "rtp": {
    "payloadType": 8,
    "channel": 0,
    "packetMs": 20,
    "samplesPerPacket": 160,
    "markerOnFirstPacket": true
  }
}
```

Replace each `<sha256>` with the digest computed from the fixture's own
`realm`, `nonce`, `cnonce`, `nonceCount`, `pass`, and `uri` — compute it, do not
invent it. Fix `Content-Length: 78` if `talkBody`'s actual byte length differs;
the number must match the string in the same file.

- [ ] **Step 2: Consume the fixture from the TypeScript test**

Append to `src/vigi/talk.test.ts`:

```ts
test('matches the cross-language request parity fixture', async () => {
  const fixture = JSON.parse(
    await readFile(
      new URL('../../rust/tests/fixtures/vigi-request-parity.json', import.meta.url),
      'utf8',
    ),
  );
  assert.equal(
    buildMultitransRequest({
      uri: `rtsp://${fixture.host}/multitrans`,
      cseq: 1,
      json: fixture.requests.talkBody,
    }),
    fixture.requests.multitransUnauthenticated,
  );
  assert.equal(
    digestAuthorization({
      user: fixture.user,
      pass: fixture.pass,
      method: 'MULTITRANS',
      uri: `rtsp://${fixture.host}/multitrans`,
      challenge: {
        realm: fixture.challenge.realm,
        nonce: fixture.challenge.nonce,
        qop: fixture.challenge.qop,
        algorithm: 'SHA-256',
      },
      nonceCount: fixture.nonceCount,
      cnonce: fixture.cnonce,
      style: 'vigi',
    }),
    fixture.requests.authorizationHeader,
  );
});
```

Add `import { readFile } from 'node:fs/promises';` and
`import { digestAuthorization } from '../rtsp/digest.ts';` at the top.

- [ ] **Step 3: Run to verify**

Run: `npm run typecheck && npm test`
Expected: all pass. If the fixture's expected strings disagree with the
implementation, fix the *fixture* — the implementation is what hardware
accepted.

- [ ] **Step 4: Commit**

```bash
git add rust/tests/fixtures/vigi-request-parity.json src/vigi/talk.test.ts
git commit -m "test(vigi): pin VIGI request bytes in a cross-language fixture"
```

---

### Task 9: Hardware verification

Automated tests cannot prove a camera emits sound. Run this once against real
hardware and record the result.

**Files:** none — this task changes no code.

- [ ] **Step 1: Confirm OpenAPI is enabled**

Run: `nc -z -G 3 <camera-ip> 20443 && echo open`
Expected: `open`. If not, enable it in the camera UI at
Settings > Network Settings > OpenAPI.

- [ ] **Step 2: Confirm the capability report names the VIGI transport**

Run:
```bash
ONVIF_PASSWORD='<password>' node --experimental-transform-types src/bin.ts \
  capabilities --host <camera-ip> --user admin | python3 -m json.tool | grep -A 5 audioSend
```
Expected: `"transport": "vigi"`, `"onvifBackchannel": false`, `"vigiTalk": true`.

- [ ] **Step 3: Lower the device speaker volume by hand**

In the camera's own UI, note the current volume and set it to about 20. The
device default of 80 is loud indoors. The library will not change it for you.

- [ ] **Step 4: Play a short file and confirm it is audible**

Run:
```bash
ONVIF_PASSWORD='<password>' node --experimental-transform-types src/bin.ts \
  play --host <camera-ip> --user admin --file /absolute/path/short.mp3 --volume 0.05
```
Expected: the CLI logs `pcma/8000 pt=8 ch=0`, reports packets sent, exits 0, and
sound comes out of the camera. Confirm audibility by ear — nothing else can.

- [ ] **Step 5: Confirm `--transport onvif` still fails cleanly on the same camera**

Run: the same command with `--transport onvif`
Expected: fails with `no sendonly backchannel audio track` and makes no OpenAPI
request.

- [ ] **Step 6: Restore the device volume**

Set the speaker volume back to what Step 3 recorded.

- [ ] **Step 7: Record the result**

Append the model, firmware, and outcome to the CHANGELOG entry from Task 7, the
way the PTZ work recorded its hardware verification.

```bash
git add CHANGELOG.md
git commit -m "docs: record VIGI transport hardware verification"
```

---

## Self-Review

**Spec coverage:** Transport selection → Task 4. Narrow fallback trigger → Task 4 (typed error + the propagation test). Lockout rules → Task 2 (single attempt, distinct errors) and Task 4 (ONVIF-first ordering makes rule 2 structural). Combined failure message → Task 4. Control channel → Task 2. Stream channel → Task 3. Lazy connect → Task 3. Detection predicate → Task 4's `openVigiBackchannel` (speaker check before stream port). Digest extraction, SHA-256, and the vendor parameter style → Task 1. Codec policy → Task 4. Capability report → Task 6. Public surface and CLI → Task 5. audiocheck, docs, trademark → Task 7. Parity fixture → Task 8. Manual hardware procedure → Task 9. Non-goals are absent by construction: no NVR code, no `setSpeakerVolume` call, no ODP, no volume mutation.

**Known gaps handed to the sibling plans:** the Python and Rust ports, and their consumption of the Task 8 fixture.

**Type consistency:** `DigestChallenge`, `DigestParameterStyle`, and `digestAuthorization` are defined in Task 1 and used with the same names in Tasks 3 and 8. `VigiControlSession.getStreamPort`/`getAudioCapability` are defined in Task 2 and called in Tasks 4 and 6. `createVigiTalkSession`, `VIGI_TALK_PAYLOAD_TYPE`, and `VIGI_TALK_SAMPLES_PER_PACKET` are defined in Task 3 and used in Tasks 4 and 8. `BackchannelUnavailableError` and `BackchannelTransport` are defined in Task 4 and used in Tasks 5 and 6. `CameraCapabilityAudioSend` is defined and used only in Task 6.

**Two places the implementer must check the codebase rather than trust this plan**, both flagged inline: whether `sendPacedFrames` accepts an optional clock (Task 3, Step 4) and what field of `EncodedAudio` holds its buffer (Task 4, Step 3).
