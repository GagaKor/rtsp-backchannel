import assert from 'node:assert/strict';
import net from 'node:net';
import { after, before, test } from 'node:test';
import { BACKCHANNEL_REQUIRE, RtspClient } from './backchannelClient.ts';

interface CapturedRequest {
  method: string;
  uri: string;
  headers: Record<string, string>;
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
});
