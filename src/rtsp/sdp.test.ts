import assert from 'node:assert/strict';
import { test } from 'node:test';
import { findBackchannelAudio, parseSdp, pickSendCodec } from './sdp.ts';

test('prefers G.711A PCMA when both G.711 variants are offered', () => {
  const sdp = parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 0 8',
    'a=control:trackID=5',
    'a=sendonly',
    'a=rtpmap:0 PCMU/8000',
    'a=rtpmap:8 PCMA/8000',
    '',
  ].join('\r\n'));

  const track = findBackchannelAudio(sdp);
  assert.ok(track);

  assert.deepEqual(pickSendCodec(track), {
    payloadType: 8,
    encoding: 'PCMA',
    clockRate: 8000,
    channels: undefined,
  });
});
