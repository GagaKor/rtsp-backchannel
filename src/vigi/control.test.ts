import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { readFile } from 'node:fs/promises';
import https from 'node:https';
import { test } from 'node:test';
import {
  VigiControlError,
  openVigiControl,
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

test('reports a locked account from the challenge leg by name', async () => {
  // A device that has already locked the OpenAPI account answers the challenge
  // with an error code and no challenge block at all. Reporting that as
  // "invalid challenge" hides the one diagnostic that matters most here --
  // describeErrorCode has had the right sentence all along, the challenge path
  // just could not reach it.
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], { challenge: { method: 'doAuth', errCode: -10022 } }),
    ),
    (error: unknown) => {
      assert.ok(error instanceof VigiControlError);
      assert.equal(error.code, -10022);
      assert.match(error.message, /account is locked by retry limit/);
      return true;
    },
  );
});

test('reports an unauthorized challenge leg by name', async () => {
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], { challenge: { method: 'doAuth', errCode: -10002 } }),
    ),
    /VIGI OpenAPI request was unauthorized/,
  );
});

test('keeps the generic message when a challenge-less reply carries no error code', async () => {
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], { challenge: { method: 'doAuth', errCode: 0 } }),
    ),
    /invalid VIGI doAuth challenge/,
  );
});

test('still accepts the good-path challenge, which carries errCode -10020', async () => {
  // The challenge leg legitimately reports "authentication failed" alongside
  // the nonce -- that is what a challenge is. Routing this reply through
  // requireSuccess would reject every successful handshake.
  const posts: RecordedPost[] = [];
  const session = await openVigiControlWithDependencies(OPTIONS, fakeControl(posts));
  assert.equal(session.stok, 'STOK1');
});

test('strips a port already carried by host instead of building an invalid URL', async () => {
  // `host` is documented as a hostname and the control port has its own
  // option, but callers reach this through APIs that accept `host:port` --
  // `openBackchannel('cam:2020', ...)` and `getCameraCapabilities({host})`
  // both forward it verbatim, and that form works on the ONVIF side. So a
  // caller who names an ONVIF port gets `https://cam:2020:20443` here, which
  // throws `Invalid URL`, which the audioSend probe reports as "no VIGI
  // speaker" -- a false negative produced by the caller's port choice alone.
  const posts: RecordedPost[] = [];
  await openVigiControlWithDependencies({ ...OPTIONS, host: 'cam:2020' }, fakeControl(posts));
  assert.equal(posts[0].url, 'https://cam:20443');
});

test('keeps a bracketed IPv6 literal intact', async () => {
  const posts: RecordedPost[] = [];
  await openVigiControlWithDependencies(
    { ...OPTIONS, host: '[2001:db8::1]:2020' },
    fakeControl(posts),
  );
  assert.equal(posts[0].url, 'https://[2001:db8::1]:20443');
});

test('rejects a host that is not a usable authority', async () => {
  await assert.rejects(
    openVigiControlWithDependencies({ ...OPTIONS, host: 'not a host' }, fakeControl([])),
    /invalid VIGI host/,
  );
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
  // No challenge block plus errCode -10020 is the device saying the
  // credentials are wrong, so it is reported as that rather than as a
  // malformed reply. The bare "invalid challenge" wording is reserved for a
  // reply that offers no error code either -- see the test above.
  await assert.rejects(
    openVigiControlWithDependencies(
      OPTIONS,
      fakeControl([], { challenge: { method: 'doAuth', errCode: -10020 } }),
    ),
    { message: 'VIGI OpenAPI authentication failed' },
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

test('destroys the request and response sockets when a real HTTPS post times out', async () => {
  // The default (non-injected) transport, postJsonOverHttps, is not part of
  // this module's public seam, so it is exercised here through
  // openVigiControl rather than exported just to make it reachable.
  // node:https is a CJS built-in, so every importer in this process shares
  // the same exports object — patching https.request here is visible to
  // control.ts's own `import https from 'node:https'` for the life of this
  // test, and is restored in `finally` regardless of outcome.
  const originalRequest = https.request;
  let requestDestroyedWith: unknown;
  let responseDestroyedWith: unknown;
  let deliverResponse: (response: unknown) => void = () => {};
  const fakeResponse = {
    on() {
      return fakeResponse;
    },
    destroy(error?: Error) {
      responseDestroyedWith = error;
    },
  };
  const fakeRequest = {
    on() {
      return fakeRequest;
    },
    end() {
      // A response arrives but never finishes, so it is still live when
      // the timeout fires below.
      deliverResponse(fakeResponse);
    },
    destroy(error?: Error) {
      requestDestroyedWith = error;
    },
  };
  https.request = ((_options: unknown, listener: (response: unknown) => void) => {
    deliverResponse = listener;
    return fakeRequest;
  }) as unknown as typeof https.request;

  try {
    await assert.rejects(
      openVigiControl({ host: 'cam', pass: 'secret', timeoutMs: 5 }),
      { message: 'VIGI OpenAPI request timeout' },
    );
  } finally {
    https.request = originalRequest;
  }

  assert.ok(requestDestroyedWith instanceof Error);
  assert.ok(responseDestroyedWith instanceof Error);
});

test('never lets the pooled keep-alive agent hand a call a stale socket', async () => {
  // This camera closes its TCP connection after answering each request. The
  // default https.globalAgent pools keep-alive sockets, so a later call
  // (e.g. the doAuth response that follows the doAuth challenge) can be
  // handed a socket the peer already closed and fail with ECONNRESET
  // ("socket hang up") — reproduced against real hardware. postJsonOverHttps
  // must pass agent: false on every request so each gets its own fresh
  // socket; this guards against that regressing silently, since every other
  // test here injects a fake postJson and never reaches the real transport.
  const originalRequest = https.request;
  const capturedOptions: https.RequestOptions[] = [];
  https.request = ((
    options: https.RequestOptions,
    listener: (response: unknown) => void,
  ) => {
    capturedOptions.push(options);
    const dataHandlers: Array<(chunk: Buffer) => void> = [];
    const endHandlers: Array<() => void> = [];
    const fakeResponse = {
      on(event: string, handler: (...args: unknown[]) => void) {
        if (event === 'data') dataHandlers.push(handler as (chunk: Buffer) => void);
        if (event === 'end') endHandlers.push(handler as () => void);
        return fakeResponse;
      },
    };
    const fakeRequest = {
      on() {
        return fakeRequest;
      },
      end() {
        listener(fakeResponse);
        // A challenge with no `authenticate` field is enough to make
        // openVigiControl reject right away, with exactly one HTTP call.
        // Which message it rejects with is another test's business; this one
        // only cares that exactly one request went out, with agent: false.
        const body = Buffer.from(
          JSON.stringify({ method: 'doAuth', errCode: -10020 }),
          'utf8',
        );
        for (const handler of dataHandlers) handler(body);
        for (const handler of endHandlers) handler();
      },
      destroy() {},
    };
    return fakeRequest;
  }) as unknown as typeof https.request;

  try {
    await assert.rejects(
      openVigiControl({ host: 'cam', pass: 'secret' }),
      { message: 'VIGI OpenAPI authentication failed' },
    );
  } finally {
    https.request = originalRequest;
  }

  assert.equal(capturedOptions.length, 1);
  assert.equal(capturedOptions[0].agent, false);
});

test('matches the cross-language doAuth parity fixture', async () => {
  // `doAuthChallenge`, `doAuthResponse` and `controlPort` had no reader in any
  // language: control.ts could change the doAuth body shape and the fixture
  // that exists to catch exactly that would stay green, leaving the Python and
  // Rust ports to implement against stale data.
  const fixture = JSON.parse(
    await readFile(
      new URL('../../rust/tests/fixtures/vigi-request-parity.json', import.meta.url),
      'utf8',
    ),
  );
  const posts: RecordedPost[] = [];
  await openVigiControlWithDependencies(
    { host: fixture.host, user: fixture.user, pass: fixture.pass },
    fakeControl(posts, {
      challenge: {
        method: 'doAuth',
        authenticate: {
          realm: fixture.challenge.realm,
          nonce: fixture.challenge.nonce,
          algorithm: fixture.challenge.algorithm,
          uri: fixture.challenge.uri,
          method: fixture.challenge.method,
        },
        errCode: -10020,
      },
    }),
  );

  assert.equal(posts.length, 2);
  assert.equal(JSON.stringify(posts[0].body), fixture.requests.doAuthChallenge);
  assert.equal(JSON.stringify(posts[1].body), fixture.requests.doAuthResponse);
  // The default control port is part of the wire contract the sibling ports
  // reproduce, so pin it here rather than to a bare literal.
  assert.equal(posts[0].url, `https://${fixture.host}:${fixture.controlPort}`);
});
