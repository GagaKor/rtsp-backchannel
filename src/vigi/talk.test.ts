import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { digestAuthorization } from '../rtsp/digest.ts';
import {
  VIGI_TALK_CHANNEL,
  VIGI_TALK_PACKET_MS,
  VIGI_TALK_PAYLOAD_TYPE,
  VIGI_TALK_SAMPLES_PER_PACKET,
  buildMultitransRequest,
  createVigiTalkSessionWithDependencies,
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
  once(event: string, handler: (arg?: unknown) => void): this {
    const wrapped: { (arg?: unknown): void; listener?: typeof handler } = (arg?: unknown) => {
      this.off(event, wrapped);
      handler(arg);
    };
    // Mirrors node:events: EventEmitter.removeListener() unwraps a once()
    // listener via this property so callers can pass the original handler
    // to off(), not the internal wrapper. Without it, the writeInterleaved
    // cleanup()'s off() calls below would silently fail to match and every
    // once() listener would leak for the life of the fake.
    wrapped.listener = handler;
    return this.on(event, wrapped);
  }
  off(event: string, handler: (arg?: unknown) => void): this {
    const list = this.handlers.get(event);
    if (list) {
      this.handlers.set(
        event,
        list.filter((candidate) => {
          const wrapped = candidate as { listener?: typeof handler };
          return candidate !== handler && wrapped.listener !== handler;
        }),
      );
    }
    return this;
  }
  end(): void { this.ended = true; }
  destroy(): void { this.destroyed = true; }
  setTimeout(): void {}

  emit(event: string, arg?: unknown): void {
    // Snapshot: a fired 'once' handler mutates the list via off() as part of
    // its own invocation, so iterating the live array would skip whichever
    // handler follows the one that just removed itself.
    for (const handler of [...(this.handlers.get(event) ?? [])]) handler(arg);
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
// A clock whose `now` never advances deadlocks sendPacedFrames's final flush
// wait (it awaits `now >= deadline`, which a constant `now` never reaches).
// Advance it on sleep, as backchannel.test.ts's fake clock does, so pacing
// resolves on the microtask queue instead of real wall-clock time.
let instantClockNow = 0;
const instantClock = {
  now: () => instantClockNow,
  sleep: async (milliseconds: number) => { instantClockNow += milliseconds; },
};

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
  createVigiTalkSessionWithDependencies(OPTIONS, {
    connect: () => { connects += 1; return socket; },
  });
  assert.equal(connects, 0);
});

test('sends the talk request with the documented mode and authenticates with the vigi style', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
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
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, mode: 'aec', clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  assert.match(socket.requests[0], /"mode":"aec"/);
});

test('frames audio as interleaved RTP with payload type 8 on channel 0', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
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
  const session = createVigiTalkSessionWithDependencies(
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
  const session = createVigiTalkSessionWithDependencies(
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
  const session = createVigiTalkSessionWithDependencies(
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
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    { connect: () => socket },
  );
  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    { message: 'VIGI talk MULTITRANS failed: RTSP/1.0 401 Unauthorized' },
  );
});

test('close destroys an opened socket and is safe before any send', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const unopened = createVigiTalkSessionWithDependencies(OPTIONS, { connect: () => new FakeSocket() });
  await unopened.close();

  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  await session.close();
  // Destroy, not end(): VIGI documents no keep-alive and no guaranteed FIN
  // reply, so a half-close (end()) would leave the handle referenced and
  // could hang a CLI run after playback. Matches RtspClient.close() on the
  // ONVIF path.
  assert.equal(socket.destroyed, true);
  assert.equal(socket.ended, false);
});

test('a write error partway through a send rejects instead of resolving with a packet count', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );

  // Open the session with one successful frame first, exactly like the
  // regression this guards: the failure lands mid-stream, after the
  // MULTITRANS handshake has already settled and the old permanent 'error'
  // listener had gone dead and would have swallowed it.
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));

  // Simulate a mid-stream ECONNRESET on the very next RTP write. The error
  // is emitted synchronously from inside write(), which fires after
  // writeInterleaved's once('error', ...) listener is already attached (it
  // is attached before write() is called), so this is deterministic — no
  // dependence on microtask ordering.
  const original = socket.write.bind(socket);
  socket.write = (chunk: Buffer | string): boolean => {
    if (Buffer.isBuffer(chunk) && chunk[0] === 0x24) {
      socket.emit('error', new Error('ECONNRESET'));
      return true;
    }
    return original(chunk);
  };

  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    /VIGI talk write failed: ECONNRESET/,
  );
});

test('a socket close partway through a send rejects rather than reporting success', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));

  // Mirrors the write-error test above, but for a camera that drops the TCP
  // connection outright instead of erroring first.
  const original = socket.write.bind(socket);
  socket.write = (chunk: Buffer | string): boolean => {
    if (Buffer.isBuffer(chunk) && chunk[0] === 0x24) {
      socket.emit('close');
      return false;
    }
    return original(chunk);
  };

  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    /VIGI talk connection closed during send/,
  );
});

test('send after close is rejected', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.close();
  await assert.rejects(
    session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5)),
    { message: 'VIGI talk session is closed' },
  );
});

test('a single data event carrying the 200 OK plus trailing bytes still opens', async () => {
  const socket = new FakeSocket();
  let replies = 0;
  const original = socket.write.bind(socket);
  socket.write = (chunk: Buffer | string): boolean => {
    const result = original(chunk);
    const text = Buffer.isBuffer(chunk) ? '' : String(chunk);
    if (text.startsWith('MULTITRANS')) {
      replies += 1;
      // The OK reply arrives coalesced with bytes that belong to whatever
      // comes next on the wire (a keepalive, the start of downlink audio).
      const reply = replies === 1 ? CHALLENGE : `${OK}TRAILING-BYTES-NOT-PART-OF-THIS-REPLY`;
      queueMicrotask(() => socket.emit('data', Buffer.from(reply, 'binary')));
    }
    return result;
  };
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    { connect: () => socket },
  );

  const sent = await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  assert.equal(sent, 1, 'the trailing bytes did not stop the session opening');
  assert.equal(socket.rtpFrames.length, 1);
});

test('bytes arriving after the handshake settles are ignored, not reprocessed', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  assert.equal(socket.requests.length, 2, 'handshake done: one challenge, one authenticated request');

  // Simulate stray post-handshake traffic on the same socket: downlink audio
  // (arbitrary binary, never framed as an RTSP reply) and a duplicate of the
  // camera's own success reply (as a keepalive echo might look). Neither
  // should be fed back into the handshake state machine.
  assert.doesNotThrow(() => socket.emit('data', Buffer.alloc(4096, 0xaa)));
  assert.doesNotThrow(() => socket.emit('data', Buffer.from(OK, 'binary')));

  // The session must still work normally afterwards: no re-authentication,
  // no extra MULTITRANS request, and audio still frames correctly.
  const sent = await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  assert.equal(sent, 1);
  assert.equal(socket.requests.length, 2, 'no additional handshake request was triggered');
  assert.equal(socket.rtpFrames.length, 2);
});

test('close during an in-flight open aborts the handshake and destroys the connection', async () => {
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, clock: instantClock },
    dependencies,
  );

  const sendPromise = session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));
  const closePromise = session.close();

  await assert.rejects(sendPromise, { message: 'VIGI talk session is closed' });
  await closePromise;
  assert.equal(socket.destroyed, true, 'the pending connection was destroyed, not leaked');
  assert.equal(socket.requests.length, 1, 'closed before the challenge reply was ever handled');
});

test('ignores interleaved audio arriving before the MULTITRANS reply', async () => {
  // In aec mode the camera may push downlink audio on the same connection.
  // Those `$`-framed binary frames were concatenated straight into the text
  // parse buffer, so parseReply scanned for the first \r\n\r\n anywhere in
  // the binary and produced a garbage statusLine -- failing the open and
  // putting raw device bytes into the error message.
  const { socket, dependencies } = harness();
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, mode: 'aec', clock: instantClock },
    dependencies,
  );
  const opening = session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));

  // One RTP-over-TCP frame whose payload happens to contain a CRLFCRLF, which
  // is all parseReply needs to mistake it for the end of a header block.
  const payload = Buffer.concat([
    Buffer.from([0x80, 0x08, 0x00, 0x01]),
    Buffer.from('\r\n\r\n', 'binary'),
    Buffer.from([0xd5, 0xd5, 0xd5, 0xd5]),
  ]);
  const header = Buffer.alloc(4);
  header[0] = 0x24;
  header[1] = 0x00;
  header.writeUInt16BE(payload.length, 2);
  await Promise.resolve();
  socket.emit('data', Buffer.concat([header, payload]));
  // The real reply follows; the handshake must still complete on it.
  socket.emit('data', Buffer.from(CHALLENGE, 'binary'));
  await Promise.resolve();
  socket.emit('data', Buffer.from(OK, 'binary'));

  assert.equal(await opening, 1);
});

test('waits for the rest of an interleaved frame split across chunks', async () => {
  // TCP splits wherever it likes. If the buffer ends mid-frame, the bytes
  // present may already contain a CRLFCRLF, so the parse has to be deferred
  // until the frame's declared length has actually arrived -- not merely
  // skipped over whatever is on hand.
  const { socket, dependencies } = harness();
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, mode: 'aec', clock: instantClock },
    dependencies,
  );
  const opening = session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));

  const payload = Buffer.concat([
    Buffer.from('\r\n\r\nRTSP/1.0 500 Bogus\r\n\r\n', 'binary'),
    Buffer.alloc(8, 0xd5),
  ]);
  const header = Buffer.alloc(4);
  header[0] = 0x24;
  header.writeUInt16BE(payload.length, 2);
  const frame = Buffer.concat([header, payload]);

  await Promise.resolve();
  // Split so the first chunk stops two bytes short of the declared length.
  socket.emit('data', frame.subarray(0, frame.length - 2));
  socket.emit('data', frame.subarray(frame.length - 2));
  socket.emit('data', Buffer.from(CHALLENGE, 'binary'));
  await Promise.resolve();
  socket.emit('data', Buffer.from(OK, 'binary'));

  assert.equal(await opening, 1);
});

test('waits for an interleaved header split across chunks', async () => {
  const { socket, dependencies } = harness();
  const session = createVigiTalkSessionWithDependencies(
    { ...OPTIONS, mode: 'aec', clock: instantClock },
    dependencies,
  );
  const opening = session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET, 0xd5));

  await Promise.resolve();
  // Two bytes of a four-byte header: the length is not yet knowable.
  socket.emit('data', Buffer.from([0x24, 0x00]));
  socket.emit('data', Buffer.concat([Buffer.from([0x00, 0x04]), Buffer.alloc(4, 0xd5)]));
  socket.emit('data', Buffer.from(CHALLENGE, 'binary'));
  await Promise.resolve();
  socket.emit('data', Buffer.from(OK, 'binary'));

  assert.equal(await opening, 1);
});

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

  // Above, `talkBody` is only fed in as an *input*, so the body this module
  // actually produces was never compared to it -- talk.ts could change shape
  // and the fixture meant to catch that would stay green. Drive a real
  // handshake and compare the bytes that went out.
  const { socket, dependencies } = harness();
  autoRespond(socket);
  const session = createVigiTalkSessionWithDependencies(
    {
      host: fixture.host,
      user: fixture.user,
      pass: fixture.pass,
      streamPort: fixture.streamPort,
      clock: instantClock,
    },
    dependencies,
  );
  await session.send(Buffer.alloc(VIGI_TALK_SAMPLES_PER_PACKET * 2, 0xd5));
  assert.equal(socket.requests[0], fixture.requests.multitransUnauthenticated);
  // The retry's Authorization cannot be compared byte for byte: the session
  // generates a fresh random cnonce, and the fixture's header is pinned by the
  // direct digestAuthorization() call above. Assert the parts the fixture does
  // fix -- realm, nonce, qop and the first nonce count -- and that the retry
  // resends the same body rather than a rebuilt one.
  assert.match(socket.requests[1], /^MULTITRANS rtsp:\/\/cam\/multitrans RTSP\/1\.0\r\n/);
  assert.ok(socket.requests[1].includes(`realm="${fixture.challenge.realm}"`));
  assert.ok(socket.requests[1].includes(`nonce="${fixture.challenge.nonce}"`));
  assert.ok(socket.requests[1].includes(`qop="${fixture.challenge.qop}"`));
  assert.ok(socket.requests[1].includes('nc="00000001"'));
  assert.ok(socket.requests[1].endsWith(fixture.requests.talkBody));

  // The rtp block had no reader in any language. These are the wire constants
  // the Python and Rust ports must reproduce byte for byte.
  assert.equal(VIGI_TALK_PAYLOAD_TYPE, fixture.rtp.payloadType);
  assert.equal(VIGI_TALK_CHANNEL, fixture.rtp.channel);
  assert.equal(VIGI_TALK_PACKET_MS, fixture.rtp.packetMs);
  assert.equal(VIGI_TALK_SAMPLES_PER_PACKET, fixture.rtp.samplesPerPacket);

  // ...and that those constants reach the wire: `$`, channel, 16-bit length,
  // then the RTP header whose second byte carries the marker bit and PT.
  const frames = socket.rtpFrames;
  assert.equal(frames.length, 2);
  assert.equal(frames[0][0], 0x24);
  assert.equal(frames[0][1], fixture.rtp.channel);
  assert.equal(frames[0][5] & 0x7f, fixture.rtp.payloadType);
  assert.equal((frames[0][5] & 0x80) !== 0, fixture.rtp.markerOnFirstPacket);
  assert.equal(
    (frames[1][5] & 0x80) !== 0,
    false,
    'the marker marks the start of talk, so only the first packet carries it',
  );
});
