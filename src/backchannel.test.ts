import assert from 'node:assert/strict';
import net from 'node:net';
import { test } from 'node:test';
import * as backchannel from './backchannel.ts';
import { RtpPacketizer } from './rtp/sender.ts';

interface TestClock {
  now(): number;
  sleep(milliseconds: number): Promise<void>;
}

type PacedSender = (
  audio: Buffer,
  sendPacket: (payload: Buffer) => void,
  clock: TestClock,
  beforePacket?: () => Promise<void>,
) => Promise<number>;

function pacedSender(): PacedSender {
  const candidate = (backchannel as unknown as { sendPacedG711?: PacedSender }).sendPacedG711;
  assert.ok(candidate);
  return candidate;
}

type PacedFrameSender = (
  frames: ReadonlyArray<{ payload: Buffer; samples: number }>,
  clockRate: number,
  sendPacket: (payload: Buffer, samples: number) => void | Promise<void>,
  clock: TestClock,
  beforePacket?: () => Promise<void>,
) => Promise<number>;

function pacedFrameSender(): PacedFrameSender {
  const candidate = (backchannel as unknown as {
    sendPacedFrames?: PacedFrameSender;
  }).sendPacedFrames;
  assert.ok(candidate);
  return candidate;
}

type SessionCloser = (
  rtsp: {
    teardown(uri: string): Promise<unknown>;
    close(): void;
  },
  streamUri: string,
) => Promise<void>;

function sessionCloser(): SessionCloser {
  const candidate = (backchannel as unknown as { closeRtspSession?: SessionCloser })
    .closeRtspSession;
  assert.ok(candidate);
  return candidate;
}

interface ParsedRtspTarget {
  host: string;
  port: number;
  uri: string;
  user: string;
  pass: string;
}

type ParseRtspTarget = (
  target: string,
  user?: string,
  pass?: string,
) => ParsedRtspTarget;

function rtspTargetParser(): ParseRtspTarget {
  const candidate = (backchannel as unknown as { parseRtspTarget?: ParseRtspTarget })
    .parseRtspTarget;
  assert.ok(candidate);
  return candidate;
}

function rtspTargetDisplay(): (target: string) => string {
  const candidate = (backchannel as unknown as {
    displayRtspTarget?: (target: string) => string;
  }).displayRtspTarget;
  assert.ok(candidate);
  return candidate;
}

function rtspCredentialRedactor(): (text: string) => string {
  const candidate = (backchannel as unknown as {
    redactRtspCredentials?: (text: string) => string;
  }).redactRtspCredentials;
  assert.ok(candidate);
  return candidate;
}

type ResolveTrackUri = (
  baseUri: string,
  contentBase: string | undefined,
  control: string,
) => string;

function trackUriResolver(): ResolveTrackUri {
  const candidate = (backchannel as unknown as {
    resolveTrackUri?: ResolveTrackUri;
  }).resolveTrackUri;
  assert.ok(candidate);
  return candidate;
}

test('sends G.711 in fixed 40ms packets and waits through the final sample', async () => {
  let now = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };
  const sizes: number[] = [];
  const sentAt: number[] = [];

  const count = await pacedSender()(
    Buffer.alloc(700),
    (payload) => {
      sizes.push(payload.length);
      sentAt.push(now);
    },
    clock,
  );

  assert.equal(count, 3);
  assert.deepEqual(sizes, [320, 320, 60]);
  assert.deepEqual(sentAt, [0, 40, 80]);
  assert.equal(now, 87.5);
});

test('rebases pacing after a full packet interval of lateness', async () => {
  let now = 0;
  let sleepCount = 0;
  const sleeps: number[] = [];
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      sleeps.push(milliseconds);
      now += milliseconds;
      if (sleepCount++ === 0) now += 45;
    },
  };
  const sentAt: number[] = [];

  await pacedSender()(Buffer.alloc(960), () => sentAt.push(now), clock);

  assert.deepEqual(sentAt, [0, 85, 125]);
  assert.deepEqual(sleeps, [40, 40, 40]);
});

test('rebases when RTSP maintenance delays a packet', async () => {
  let now = 0;
  let maintenanceCalls = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };
  const sentAt: number[] = [];

  await pacedSender()(
    Buffer.alloc(640),
    () => sentAt.push(now),
    clock,
    async () => {
      maintenanceCalls++;
      if (maintenanceCalls === 2) now += 45;
    },
  );

  assert.equal(maintenanceCalls, 2);
  assert.deepEqual(sentAt, [0, 85]);
  assert.equal(now, 125);
});

test('awaits an asynchronous packet write before pacing the next frame', async () => {
  let now = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };
  let releaseWrite = () => {};
  const firstWrite = new Promise<void>((resolve) => {
    releaseWrite = resolve;
  });
  const sentAt: number[] = [];

  const sending = pacedFrameSender()(
    [
      { payload: Buffer.alloc(320), samples: 320 },
      { payload: Buffer.alloc(320), samples: 320 },
    ],
    8000,
    async () => {
      sentAt.push(now);
      if (sentAt.length === 1) await firstWrite;
    },
    clock,
  );
  await new Promise<void>((resolve) => setImmediate(resolve));
  try {
    assert.deepEqual(sentAt, [0]);
  } finally {
    releaseWrite();
  }

  assert.equal(await sending, 2);
  assert.deepEqual(sentAt, [0, 40]);
  assert.equal(now, 80);
});

test('rejects fractional frame sample ticks before sending', async () => {
  let sent = 0;
  let now = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };

  await assert.rejects(
    pacedFrameSender()(
      [{ payload: Buffer.alloc(1), samples: 1.5 }],
      8000,
      () => {
        sent++;
      },
      clock,
    ),
    /audio frame samples must be a positive integer/,
  );
  assert.equal(sent, 0);
});

test('paces AAC access units by RTP clock ticks and advances timestamps by 1024', async () => {
  let now = 0;
  const clock: TestClock = {
    now: () => now,
    sleep: async (milliseconds) => {
      now += milliseconds;
    },
  };
  const packetizer = new RtpPacketizer({
    payloadType: 110,
    clockRate: 8000,
    ssrc: 1,
    seqStart: 10,
    timestampStart: 50_000,
  });
  const sentAt: number[] = [];
  const timestamps: number[] = [];
  const payloads = [
    Buffer.from([0x00, 0x10, 0x00, 0x10, 0xaa, 0xbb]),
    Buffer.from([0x00, 0x10, 0x00, 0x08, 0xcc]),
  ];

  const count = await pacedFrameSender()(
    payloads.map((payload) => ({ payload, samples: 1024 })),
    8000,
    (payload, samples) => {
      sentAt.push(now);
      const packet = packetizer.build(payload, samples);
      timestamps.push(packet.readUInt32BE(4));
      assert.deepEqual(packet.subarray(12), payload);
    },
    clock,
  );

  assert.equal(count, 2);
  assert.deepEqual(sentAt, [0, 128]);
  assert.deepEqual(timestamps, [50_000, 51_024]);
  assert.equal(now, 256);
});

test('closes the RTSP socket when TEARDOWN fails', async () => {
  const calls: string[] = [];
  const rtsp = {
    async teardown(uri: string): Promise<unknown> {
      calls.push(`teardown:${uri}`);
      throw new Error('TEARDOWN failed');
    },
    close(): void {
      calls.push('close');
    },
  };

  await assert.rejects(
    sessionCloser()(rtsp, 'rtsp://camera/live'),
    /TEARDOWN failed/,
  );
  assert.deepEqual(calls, ['teardown:rtsp://camera/live', 'close']);
});

test('parses final-@ RTSP userinfo, decodes it, and applies non-empty overrides', () => {
  const target = 'rtsp://camera%20user:p%40ss@word@example.test:8554/live?stream=1';

  assert.deepEqual(rtspTargetParser()(target), {
    host: 'example.test',
    port: 8554,
    uri: 'rtsp://example.test:8554/live?stream=1',
    user: 'camera user',
    pass: 'p@ss@word',
  });
  assert.deepEqual(rtspTargetParser()(target, 'explicit', ''), {
    host: 'example.test',
    port: 8554,
    uri: 'rtsp://example.test:8554/live?stream=1',
    user: 'explicit',
    pass: 'p@ss@word',
  });
  assert.deepEqual(rtspTargetParser()(target, '', 'override'), {
    host: 'example.test',
    port: 8554,
    uri: 'rtsp://example.test:8554/live?stream=1',
    user: 'camera user',
    pass: 'override',
  });
});

test('redacts raw and percent-encoded RTSP credentials for display', () => {
  const raw = 'rtsp://camera-user:p@ss@camera.test/live';
  const encoded = 'rtsp://camera-user:p%40ss@camera.test/live';

  assert.equal(rtspTargetDisplay()(raw), 'rtsp://camera.test/live');
  assert.equal(rtspTargetDisplay()(encoded), 'rtsp://camera.test/live');
  const message = rtspCredentialRedactor()(`raw=${raw} encoded=${encoded}`);
  assert.equal(
    message,
    'raw=rtsp://camera.test/live encoded=rtsp://camera.test/live',
  );
  assert.doesNotMatch(message, /camera-user|p@ss|p%40ss/);
});

test('keeps malformed RTSP parse errors credential-free and rejects port zero', () => {
  assert.throws(
    () => rtspTargetParser()('rtsp://admin:s%ZZecret@camera.test/live'),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /bad percent-encoding in RTSP userinfo/);
      assert.doesNotMatch(error.message, /admin|s%ZZecret|camera\.test/);
      return true;
    },
  );
  assert.throws(
    () => rtspTargetParser()('rtsp://admin:port-secret@camera.test:0/live'),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /RTSP port must be between 1 and 65535/);
      assert.doesNotMatch(error.message, /admin|port-secret|camera\.test/);
      return true;
    },
  );
});

test('resolves SDP controls with URI semantics and strips credentials', () => {
  const resolve = trackUriResolver();
  const stream = 'rtsp://stream-user:stream-pass@camera.test/live/main?profile=1';

  assert.equal(
    resolve(stream, undefined, 'rtsp://track-user:p%40ss@camera.test/absolute/audio'),
    'rtsp://camera.test/absolute/audio',
  );
  assert.equal(
    resolve(stream, undefined, '/tracks/audio'),
    'rtsp://camera.test/tracks/audio',
  );
  assert.equal(
    resolve(stream, undefined, '?track=audio'),
    'rtsp://camera.test/live/main?track=audio',
  );
  assert.equal(
    resolve(
      stream,
      'rtsp://base-user:p@ss@camera.test/root/session/',
      '../trackID=5',
    ),
    'rtsp://camera.test/root/trackID=5',
  );
  assert.equal(
    resolve(stream, undefined, 'trackID=5'),
    'rtsp://camera.test/live/main/trackID=5',
  );
});

test('strips IPv6 brackets for the socket host but retains them in the RTSP URI', () => {
  assert.deepEqual(
    rtspTargetParser()('rtsp://user:pass@[2001:db8::1]:8554/live'),
    {
      host: '2001:db8::1',
      port: 8554,
      uri: 'rtsp://[2001:db8::1]:8554/live',
      user: 'user',
      pass: 'pass',
    },
  );
});

test('preserves the keepalive deadline across encoding and paced RTP send', async () => {
  const requests: Array<{ method: string; uri: string }> = [];
  const media: Array<{ channel: number; rtp: Buffer }> = [];
  const wireEvents: string[] = [];
  let serverPort = 0;
  const server = net.createServer((socket) => {
    let input = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      input = Buffer.concat([input, chunk]);
      while (true) {
        if (input[0] === 0x24) {
          if (input.length < 4) return;
          const frameLength = input.readUInt16BE(2);
          if (input.length < frameLength + 4) return;
          media.push({
            channel: input[1],
            rtp: Buffer.from(input.subarray(4, frameLength + 4)),
          });
          wireEvents.push('RTP');
          input = input.subarray(frameLength + 4);
          continue;
        }
        const end = input.indexOf('\r\n\r\n');
        if (end < 0) return;
        const request = input.subarray(0, end).toString('utf8');
        input = input.subarray(end + 4);
        const [requestLine, ...headerLines] = request.split('\r\n');
        const [method, uri] = requestLine.split(' ');
        requests.push({ method, uri });
        wireEvents.push(method);
        const cseq = headerLines
          .find((line) => line.toLowerCase().startsWith('cseq:'))
          ?.slice('cseq:'.length)
          .trim();
        if (method === 'DESCRIBE') {
          const body = [
            'v=0',
            'm=audio 0 RTP/AVP 96 97 110',
            'a=sendonly',
            `a=control:rtsp://track-user:track-pass@127.0.0.1:${serverPort}/live/trackID=5`,
            'a=rtpmap:96 OPUS/48000/2',
            'a=rtpmap:97 G726-32/8000',
            'a=rtpmap:110 MPEG4-GENERIC/8000/1',
            'a=fmtp:110 streamtype=5; mode=AAC-hbr; config=1588; ' +
              'SizeLength=13; IndexLength=3; IndexDeltaLength=3; constantDuration=1024',
            '',
          ].join('\r\n');
          socket.write(
            `RTSP/1.0 200 OK\r\nCSeq: ${cseq}\r\nContent-Type: application/sdp\r\n` +
              `Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`,
          );
        } else if (method === 'SETUP') {
          socket.write(
            `RTSP/1.0 200 OK\r\nCSeq: ${cseq}\r\nSession: direct-session;timeout=2\r\n` +
              'Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nContent-Length: 0\r\n\r\n',
          );
        } else {
          socket.write(`RTSP/1.0 200 OK\r\nCSeq: ${cseq}\r\nContent-Length: 0\r\n\r\n`);
        }
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  serverPort = address.port;
  const sanitized = `rtsp://127.0.0.1:${serverPort}/live`;
  const target = `rtsp://embedded:p@ss@127.0.0.1:${address.port}/live`;
  const firstAac = Buffer.from([0x00, 0x10, 0x00, 0x08, 0xaa]);
  const secondAac = Buffer.from([0x00, 0x10, 0x00, 0x08, 0xbb]);

  try {
    const session = await backchannel.openBackchannel(target);
    const negotiated = session as unknown as {
      codec: { name: string; clockRate: number };
      withKeepAlive<T>(operation: () => Promise<T>): Promise<T>;
      send(audio: {
        codec: string;
        clockRate: number;
        frames: Array<{ payload: Buffer; samples: number }>;
        byteLength: number;
        sampleCount: number;
      }): Promise<number>;
      close(): Promise<void>;
    };
    try {
      assert.equal(negotiated.codec.name, 'g726-32');
      assert.equal(negotiated.codec.clockRate, 8000);
      assert.equal(
        await negotiated.withKeepAlive(
          () => new Promise<string>((resolve) => setTimeout(() => resolve('encoded'), 900)),
        ),
        'encoded',
      );
      const first = Buffer.alloc(160, 0x11);
      const second = Buffer.alloc(160, 0x22);
      const third = Buffer.alloc(160, 0x33);
      const fourth = Buffer.alloc(160, 0x44);
      assert.equal(await negotiated.send({
        codec: 'g726-32',
        clockRate: 8000,
        frames: [
          { payload: first, samples: 320 },
          { payload: second, samples: 320 },
          { payload: third, samples: 320 },
          { payload: fourth, samples: 320 },
        ],
        byteLength: 640,
        sampleCount: 1280,
      }), 4);
    } finally {
      await session.close();
    }

    const aacSession = await backchannel.openBackchannel(target, '', '', { codec: 'aac' });
    assert.equal(aacSession.codec.name, 'aac');
    assert.equal(await aacSession.send({
      codec: 'aac',
      clockRate: 8000,
      frames: [
        { payload: firstAac, samples: 1024 },
        { payload: secondAac, samples: 1024 },
      ],
      byteLength: firstAac.length + secondAac.length,
      sampleCount: 2048,
    }), 2);
    await aacSession.close();
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }

  assert.deepEqual(requests.map(({ method }) => method), [
    'OPTIONS',
    'DESCRIBE',
    'SETUP',
    'PLAY',
    'OPTIONS',
    'TEARDOWN',
    'OPTIONS',
    'DESCRIBE',
    'SETUP',
    'PLAY',
    'TEARDOWN',
  ]);
  assert.deepEqual(requests.map(({ uri }) => uri), [
    sanitized,
    sanitized,
    `${sanitized}/trackID=5`,
    sanitized,
    sanitized,
    sanitized,
    sanitized,
    sanitized,
    `${sanitized}/trackID=5`,
    sanitized,
    sanitized,
  ]);
  assert.ok(requests.every(({ uri }) => !uri.includes('embedded')));
  assert.ok(requests.every(({ uri }) => !uri.includes('p@ss')));
  assert.equal(media.length, 6);
  assert.deepEqual(media.map(({ channel }) => channel), [0, 0, 0, 0, 0, 0]);
  assert.equal(media[0].rtp[1], 0x80 | 97);
  assert.equal(media[1].rtp[1], 97);
  assert.equal(
    (media[1].rtp.readUInt32BE(4) - media[0].rtp.readUInt32BE(4)) >>> 0,
    320,
  );
  assert.deepEqual(media[0].rtp.subarray(12), Buffer.alloc(160, 0x11));
  assert.deepEqual(media[1].rtp.subarray(12), Buffer.alloc(160, 0x22));
  assert.equal(media[4].rtp[1], 0x80 | 110);
  assert.equal(media[5].rtp[1], 0x80 | 110);
  assert.equal(
    (media[5].rtp.readUInt32BE(4) - media[4].rtp.readUInt32BE(4)) >>> 0,
    1024,
  );
  assert.deepEqual(media[4].rtp.subarray(12), firstAac);
  assert.deepEqual(media[5].rtp.subarray(12), secondAac);

  const firstPlay = wireEvents.indexOf('PLAY');
  const firstTeardown = wireEvents.indexOf('TEARDOWN', firstPlay + 1);
  const activeSession = wireEvents.slice(firstPlay + 1, firstTeardown);
  const keepAlive = activeSession.indexOf('OPTIONS');
  assert.ok(keepAlive >= 0, 'expected keepalive at the original session deadline');
  assert.ok(activeSession.slice(0, keepAlive).includes('RTP'));
  assert.ok(activeSession.slice(keepAlive + 1).includes('RTP'));
});
