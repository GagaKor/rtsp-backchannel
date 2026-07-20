import assert from 'node:assert/strict';
import net from 'node:net';
import { after, before, test } from 'node:test';
import { BACKCHANNEL_REQUIRE, RtspClient } from './backchannelClient.ts';

interface CapturedRequest {
  method: string;
  uri: string;
  headers: Record<string, string>;
}

class BackpressureSocket extends net.Socket {
  readonly frames: Buffer[] = [];

  override write(buffer: Uint8Array | string): boolean;
  override write(
    buffer: Uint8Array | string,
    callback?: (error?: Error | null) => void,
  ): boolean;
  override write(
    buffer: Uint8Array | string,
    encoding?: BufferEncoding,
    callback?: (error?: Error | null) => void,
  ): boolean;
  override write(buffer: Uint8Array | string): boolean {
    this.frames.push(Buffer.isBuffer(buffer) ? Buffer.from(buffer) : Buffer.from(buffer));
    return false;
  }
}

async function withAuthChallenge(
  challenge: string,
  user: string,
  run: (client: RtspClient, captured: CapturedRequest[]) => Promise<void>,
): Promise<CapturedRequest[]> {
  const captured: CapturedRequest[] = [];
  const challengeServer = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const raw = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = raw.split('\r\n');
        const [method, uri] = requestLine.split(' ');
        const headers: Record<string, string> = {};
        for (const line of headerLines) {
          const colon = line.indexOf(':');
          if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon + 1).trim();
        }
        captured.push({ method, uri, headers });
        if (!headers['authorization']) {
          socket.write(
            `RTSP/1.0 401 Unauthorized\r\nCSeq: ${headers['cseq']}\r\n` +
              `WWW-Authenticate: ${challenge}\r\nContent-Length: 0\r\n\r\n`,
          );
        } else {
          socket.write(
            `RTSP/1.0 200 OK\r\nCSeq: ${headers['cseq']}\r\nContent-Length: 0\r\n\r\n`,
          );
        }
      }
    });
  });
  await new Promise<void>((resolve) => challengeServer.listen(0, '127.0.0.1', resolve));
  const address = challengeServer.address();
  assert.ok(address && typeof address !== 'string');
  const client = new RtspClient('127.0.0.1', address.port, user, 'secret');
  try {
    await client.connect();
    await run(client, captured);
  } finally {
    client.close();
    await new Promise<void>((resolve, reject) =>
      challengeServer.close((error) => (error ? reject(error) : resolve())),
    );
  }
  return captured;
}

async function withRtspResponder(
  respond: (request: CapturedRequest, ordinal: number, socket: net.Socket) => void,
  run: (client: RtspClient, captured: CapturedRequest[]) => Promise<void>,
  timeoutMs = 100,
): Promise<CapturedRequest[]> {
  const captured: CapturedRequest[] = [];
  const responseServer = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const raw = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = raw.split('\r\n');
        const [method, uri] = requestLine.split(' ');
        const headers: Record<string, string> = {};
        for (const line of headerLines) {
          const colon = line.indexOf(':');
          if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon + 1).trim();
        }
        const request = { method, uri, headers };
        captured.push(request);
        respond(request, captured.length, socket);
      }
    });
  });
  await new Promise<void>((resolve) => responseServer.listen(0, '127.0.0.1', resolve));
  const address = responseServer.address();
  assert.ok(address && typeof address !== 'string');
  const client = new RtspClient('127.0.0.1', address.port, '', '', timeoutMs);
  try {
    await client.connect();
    await run(client, captured);
  } finally {
    client.close();
    await new Promise<void>((resolve, reject) =>
      responseServer.close((error) => (error ? reject(error) : resolve())),
    );
  }
  return captured;
}

const requests: CapturedRequest[] = [];
let server: net.Server;
let port: number;

before(async () => {
  server = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const raw = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = raw.split('\r\n');
        const [method, uri] = requestLine.split(' ');
        const headers: Record<string, string> = {};
        for (const line of headerLines) {
          const colon = line.indexOf(':');
          if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon + 1).trim();
        }
        requests.push({ method, uri, headers });

        const cseq = headers['cseq'];
        if (method === 'SETUP') {
          const channel = requests.filter((r) => r.method === 'SETUP').length === 1 ? '0-1' : '2-3';
          socket.write(
            `RTSP/1.0 200 OK\r\nCSeq: ${cseq}\r\nSession: test-session;timeout=60\r\n` +
              `Transport: RTP/AVP/TCP;unicast;interleaved=${channel}\r\nContent-Length: 0\r\n\r\n`,
          );
        } else {
          // Exercise response parsing when an incoming interleaved RTP frame
          // arrives before the RTSP response.
          socket.write(Buffer.from([0x24, 0x00, 0x00, 0x02, 0xaa, 0xbb]));
          socket.write(`RTSP/1.0 200 OK\r\nCSeq: ${cseq}\r\nContent-Length: 0\r\n\r\n`);
        }
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('test server did not bind a TCP port');
  port = address.port;
});

after(async () => {
  await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())));
});

test('sets up one RTSP session and starts ONVIF backchannel with PLAY', async () => {
  requests.length = 0;
  const client = new RtspClient('127.0.0.1', port, 'admin', 'pass');
  await client.connect();
  try {
    await client.setup('rtsp://camera/trackID=0', { rtpChannel: 0 });
    const backchannel = await client.setup('rtsp://camera/trackID=5', {
      rtpChannel: 2,
      backchannel: true,
    });
    const play = await client.play('rtsp://camera/live');
    assert.equal(play.status, 200);
    assert.equal(backchannel.rtpChannel, 2);
    assert.equal(client.sessionTimeoutSeconds, 60);
    const keepalive = await client.keepAlive('rtsp://camera/live');
    assert.equal(keepalive.status, 200);
    await client.teardown('rtsp://camera/live');
  } finally {
    client.close();
  }

  assert.equal(requests[0].method, 'SETUP');
  assert.equal(requests[0].headers['session'], undefined);
  assert.equal(requests[0].headers['require'], undefined);

  assert.equal(requests[1].method, 'SETUP');
  assert.equal(requests[1].headers['session'], 'test-session');
  assert.equal(requests[1].headers['require'], BACKCHANNEL_REQUIRE);

  assert.equal(requests[2].method, 'PLAY');
  assert.equal(requests[2].headers['session'], 'test-session');
  assert.equal(requests[2].headers['range'], 'npt=now-');
  assert.equal(requests[2].headers['require'], BACKCHANNEL_REQUIRE);
  assert.equal(requests[3].method, 'OPTIONS');
  assert.equal(requests[3].headers['session'], 'test-session');
  assert.equal(requests[4].method, 'TEARDOWN');
  assert.ok(requests.every((request) => request.headers['authorization'] === undefined));
});

test('adds RTSP Digest authorization only after a 401 challenge', async () => {
  const captured: CapturedRequest[] = [];
  const challengeServer = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const raw = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = raw.split('\r\n');
        const [method, uri] = requestLine.split(' ');
        const headers: Record<string, string> = {};
        for (const line of headerLines) {
          const colon = line.indexOf(':');
          if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon + 1).trim();
        }
        captured.push({ method, uri, headers });
        if (!headers['authorization']) {
          socket.write(
            `RTSP/1.0 401 Unauthorized\r\nCSeq: ${headers['cseq']}\r\n` +
              'WWW-Authenticate: Digest realm="camera", nonce="abc123", qop="auth"\r\n' +
              'Content-Length: 0\r\n\r\n',
          );
        } else {
          socket.write(
            `RTSP/1.0 200 OK\r\nCSeq: ${headers['cseq']}\r\nContent-Length: 0\r\n\r\n`,
          );
        }
      }
    });
  });
  await new Promise<void>((resolve) => challengeServer.listen(0, '127.0.0.1', resolve));
  const address = challengeServer.address();
  assert.ok(address && typeof address !== 'string');
  const client = new RtspClient('127.0.0.1', address.port, 'admin', 'secret');

  try {
    await client.connect();
    const response = await client.options('rtsp://camera/live');
    assert.equal(response.status, 200);
  } finally {
    client.close();
    await new Promise<void>((resolve, reject) =>
      challengeServer.close((error) => (error ? reject(error) : resolve())),
    );
  }

  assert.equal(captured.length, 2);
  assert.equal(captured[0].headers['authorization'], undefined);
  assert.match(captured[1].headers['authorization'], /^Digest /);
  assert.match(captured[1].headers['authorization'], /username="admin"/);
  assert.match(captured[1].headers['authorization'], /uri="rtsp:\/\/camera\/live"/);
});

test('rejects unsupported RTSP Digest algorithms and qop modes explicitly', async () => {
  const cases = [
    {
      challenge: 'Digest realm="camera", nonce="abc123", algorithm=SHA-256',
      error: /unsupported RTSP Digest algorithm: SHA-256/,
    },
    {
      challenge: 'Digest realm="camera", nonce="abc123", qop="auth-int"',
      error: /unsupported RTSP Digest qop: auth-int/,
    },
  ];
  for (const { challenge, error } of cases) {
    const captured = await withAuthChallenge(challenge, 'admin', async (client) => {
      await assert.rejects(client.options('rtsp://camera/live'), error);
    });
    assert.equal(captured.length, 1);
  }
});

test('selects Digest auth qop and safely quotes the username', async () => {
  const captured = await withAuthChallenge(
    'Digest realm="camera", nonce="abc123", algorithm=MD5, qop="auth-int, auth"',
    'cam"era\\operator',
    async (client) => {
      assert.equal((await client.options('rtsp://camera/live')).status, 200);
    },
  );

  assert.equal(captured.length, 2);
  assert.match(captured[1].headers['authorization'], /qop=auth(?:,|$)/);
  assert.match(
    captured[1].headers['authorization'],
    /username="cam\\"era\\\\operator"/,
  );
});

test('rejects control characters in a Digest username before retrying', async () => {
  const captured = await withAuthChallenge(
    'Digest realm="camera", nonce="abc123", qop="auth"',
    'admin\r\nInjected: yes',
    async (client) => {
      await assert.rejects(
        client.options('rtsp://camera/live'),
        /RTSP Digest username contains control characters/,
      );
    },
  );

  assert.equal(captured.length, 1);
});

test('increments Digest nonce count and resets it for a new nonce', async () => {
  const captured: CapturedRequest[] = [];
  const nonceServer = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const raw = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = raw.split('\r\n');
        const [method, uri] = requestLine.split(' ');
        const headers: Record<string, string> = {};
        for (const line of headerLines) {
          const colon = line.indexOf(':');
          if (colon > 0) headers[line.slice(0, colon).toLowerCase()] = line.slice(colon + 1).trim();
        }
        captured.push({ method, uri, headers });
        const sendChallenge = !headers['authorization'] || captured.length === 4;
        const nonce = captured.length === 4 ? 'nonce-two' : 'nonce-one';
        socket.write(
          sendChallenge
            ? `RTSP/1.0 401 Unauthorized\r\nCSeq: ${headers['cseq']}\r\n` +
                `WWW-Authenticate: Digest realm="camera", nonce="${nonce}", qop="auth"\r\n` +
                'Content-Length: 0\r\n\r\n'
            : `RTSP/1.0 200 OK\r\nCSeq: ${headers['cseq']}\r\nContent-Length: 0\r\n\r\n`,
        );
      }
    });
  });
  await new Promise<void>((resolve) => nonceServer.listen(0, '127.0.0.1', resolve));
  const address = nonceServer.address();
  assert.ok(address && typeof address !== 'string');
  const client = new RtspClient('127.0.0.1', address.port, 'admin', 'secret');

  try {
    await client.connect();
    assert.equal((await client.options('rtsp://camera/one')).status, 200);
    assert.equal((await client.options('rtsp://camera/two')).status, 200);
    assert.equal((await client.options('rtsp://camera/three')).status, 200);
  } finally {
    client.close();
    await new Promise<void>((resolve, reject) =>
      nonceServer.close((error) => (error ? reject(error) : resolve())),
    );
  }

  assert.deepEqual(
    captured
      .map((request) => request.headers['authorization'])
      .filter((authorization): authorization is string => authorization !== undefined)
      .map((authorization) => ({
        nonce: /nonce="([^"]+)"/.exec(authorization)?.[1],
        nc: /nc=([0-9a-f]{8})/i.exec(authorization)?.[1],
      })),
    [
      { nonce: 'nonce-one', nc: '00000001' },
      { nonce: 'nonce-one', nc: '00000002' },
      { nonce: 'nonce-one', nc: '00000003' },
      { nonce: 'nonce-two', nc: '00000001' },
    ],
  );
});

test('rejects a response whose CSeq does not match its request', async () => {
  await withRtspResponder(
    (_request, _ordinal, socket) => {
      socket.write('RTSP/1.0 200 OK\r\nCSeq: 999\r\nContent-Length: 0\r\n\r\n');
    },
    async (client) => {
      await assert.rejects(
        client.options('rtsp://camera/cseq'),
        /RTSP response CSeq 999 does not match request CSeq 1/,
      );
      assert.throws(() => client.rawSocket, /not connected/);
    },
  );
});

test('destroys the connection on timeout before a queued request can consume a late response', async () => {
  const captured = await withRtspResponder(
    (request, ordinal, socket) => {
      if (ordinal !== 1) return;
      setTimeout(() => {
        if (socket.destroyed) return;
        socket.write(
          `RTSP/1.0 200 Late\r\nCSeq: ${request.headers['cseq']}\r\n` +
            'Content-Length: 0\r\n\r\n',
        );
      }, 50);
    },
    async (client) => {
      const first = client.options('rtsp://camera/slow');
      const queued = client.options('rtsp://camera/queued');
      await assert.rejects(first, /RTSP response timeout/);
      await assert.rejects(queued, /not connected/);
      await new Promise<void>((resolve) => setTimeout(resolve, 60));
    },
    15,
  );

  assert.deepEqual(captured.map((request) => request.uri), ['rtsp://camera/slow']);
});

test('rejects invalid and negative RTSP Content-Length values', async () => {
  for (const contentLength of ['-1', 'not-a-number', '1.5']) {
    await withRtspResponder(
      (request, _ordinal, socket) => {
        socket.write(
          `RTSP/1.0 200 OK\r\nCSeq: ${request.headers['cseq']}\r\n` +
            `Content-Length: ${contentLength}\r\n\r\n`,
        );
      },
      async (client) => {
        await assert.rejects(
          client.options('rtsp://camera/invalid-length'),
          /invalid RTSP Content-Length/,
        );
        assert.throws(() => client.rawSocket, /not connected/);
      },
    );
  }
});

test('rejects oversized RTSP response headers and bodies', async () => {
  await withRtspResponder(
    (request, _ordinal, socket) => {
      socket.write(
        `RTSP/1.0 200 OK\r\nCSeq: ${request.headers['cseq']}\r\nX-Fill: ` +
          'a'.repeat(70_000),
      );
    },
    async (client) => {
      await assert.rejects(
        client.options('rtsp://camera/oversized-header'),
        /RTSP response header exceeds 65536 bytes/,
      );
      assert.throws(() => client.rawSocket, /not connected/);
    },
  );

  await withRtspResponder(
    (request, _ordinal, socket) => {
      socket.write(
        `RTSP/1.0 200 OK\r\nCSeq: ${request.headers['cseq']}\r\n` +
          `Content-Length: ${8 * 1024 * 1024 + 1}\r\n\r\n`,
      );
    },
    async (client) => {
      await assert.rejects(
        client.options('rtsp://camera/oversized-body'),
        /RTSP response body exceeds 8388608 bytes/,
      );
      assert.throws(() => client.rawSocket, /not connected/);
    },
  );
});

test('rejects malformed RTSP status and header framing', async () => {
  const cases = [
    {
      response: 'NOT-RTSP 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n',
      error: /invalid RTSP response status line/,
    },
    {
      response: 'RTSP/1.0 200 OK\r\nCSeq: 1\r\nMalformed-Header\r\n\r\n',
      error: /malformed RTSP response header/,
    },
  ];
  for (const { response, error } of cases) {
    await withRtspResponder(
      (_request, _ordinal, socket) => socket.write(response),
      async (client) => {
        await assert.rejects(client.options('rtsp://camera/malformed'), error);
        assert.throws(() => client.rawSocket, /not connected/);
      },
    );
  }
});

test('serializes concurrent RTSP requests so each caller owns its response', async () => {
  let firstResponseSent = false;
  let overlapped = false;
  const seen: string[] = [];
  const delayedServer = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const raw = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = raw.split('\r\n');
        const [, uri] = requestLine.split(' ');
        const cseq = headerLines
          .find((line) => line.toLowerCase().startsWith('cseq:'))
          ?.slice('cseq:'.length)
          .trim();
        seen.push(uri);
        const ordinal = seen.length;
        if (ordinal === 2 && !firstResponseSent) overlapped = true;
        setTimeout(() => {
          if (ordinal === 1) firstResponseSent = true;
          socket.write(
            `RTSP/1.0 200 ${ordinal === 1 ? 'First' : 'Second'}\r\n` +
              `CSeq: ${cseq}\r\nContent-Length: 0\r\n\r\n`,
          );
        }, ordinal === 1 ? 25 : 0);
      }
    });
  });
  await new Promise<void>((resolve) => delayedServer.listen(0, '127.0.0.1', resolve));
  const address = delayedServer.address();
  assert.ok(address && typeof address !== 'string');
  const client = new RtspClient('127.0.0.1', address.port, '', '', 200);

  try {
    await client.connect();
    const first = client.options('rtsp://camera/first');
    const second = client.options('rtsp://camera/second');
    const responses = await Promise.all([first, second]);
    assert.deepEqual(responses.map((response) => response.statusLine), [
      'RTSP/1.0 200 First',
      'RTSP/1.0 200 Second',
    ]);
  } finally {
    client.close();
    await new Promise<void>((resolve, reject) =>
      delayedServer.close((error) => (error ? reject(error) : resolve())),
    );
  }

  assert.equal(overlapped, false);
  assert.deepEqual(seen, ['rtsp://camera/first', 'rtsp://camera/second']);
});

test('waits for interleaved RTP drain and bounds socket errors and stalls', async () => {
  type SendInterleaved = (frame: Buffer) => Promise<void>;

  const drainedClient = new RtspClient('camera.test', 554, '', '', 25);
  const drainedSocket = new BackpressureSocket();
  (drainedClient as unknown as { socket: net.Socket }).socket = drainedSocket;
  const sendDrained = drainedClient.sendInterleaved.bind(drainedClient) as unknown as SendInterleaved;
  let settled = false;
  const pending = sendDrained(Buffer.from([0x24, 0, 0, 0])).then(() => {
    settled = true;
  });
  await Promise.resolve();
  assert.equal(settled, false);
  drainedSocket.emit('drain');
  await pending;
  assert.equal(settled, true);
  assert.equal(drainedSocket.listenerCount('drain'), 0);
  assert.equal(drainedSocket.listenerCount('error'), 0);

  const failedClient = new RtspClient('camera.test', 554, '', '', 25);
  const failedSocket = new BackpressureSocket();
  (failedClient as unknown as { socket: net.Socket }).socket = failedSocket;
  const sendFailed = failedClient.sendInterleaved.bind(failedClient) as unknown as SendInterleaved;
  const failed = sendFailed(Buffer.from([0x24, 0, 0, 0]));
  failedSocket.emit('error', new Error('socket failed'));
  await assert.rejects(failed, /RTSP interleaved write failed: socket failed/);

  const stalledClient = new RtspClient('camera.test', 554, '', '', 10);
  const stalledSocket = new BackpressureSocket();
  (stalledClient as unknown as { socket: net.Socket }).socket = stalledSocket;
  const sendStalled = stalledClient.sendInterleaved.bind(stalledClient) as unknown as SendInterleaved;
  await assert.rejects(
    sendStalled(Buffer.from([0x24, 0, 0, 0])),
    /RTSP interleaved write timeout after 10 ms/,
  );
  assert.equal(stalledSocket.destroyed, true);
  assert.equal(stalledSocket.listenerCount('drain'), 0);
  assert.equal(stalledSocket.listenerCount('error'), 0);
});
