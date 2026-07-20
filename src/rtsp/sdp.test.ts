import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  findBackchannelAudio,
  parseSdp,
  pickSendCodec,
  type CodecPreference,
  type MediaDescription,
} from './sdp.ts';

function aacTrack(
  fmtp: string,
  clockRate = 8000,
  channels = 1,
): MediaDescription {
  const track = findBackchannelAudio(parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 97',
    'a=sendonly',
    `a=rtpmap:97 MPEG4-GENERIC/${clockRate}/${channels}`,
    `a=fmtp:97 ${fmtp}`,
    '',
  ].join('\r\n')));
  assert.ok(track);
  return track;
}

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
    name: 'pcma',
    payloadType: 8,
    encoding: 'PCMA',
    clockRate: 8000,
    channels: undefined,
  });
});

test('parses fmtp parameters and chooses supported codecs in compatibility order', () => {
  const sdp = parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 110 111 112 113 114',
    'a=control:trackID=5',
    'a=sendonly',
    'a=rtpmap:110 MPEG4-GENERIC/8000/1',
    'a=fmtp:110 streamtype=5; profile-level-id=1; mode=AAC-hbr; config=1588; SizeLength=13; IndexLength=3; IndexDeltaLength=3',
    'a=rtpmap:111 G726-40/8000',
    'a=rtpmap:112 G726-32/8000',
    'a=rtpmap:113 G726-24/8000',
    'a=rtpmap:114 G726-16/8000',
    '',
  ].join('\r\n'));

  const track = findBackchannelAudio(sdp);
  assert.ok(track);
  assert.deepEqual(track.fmtps[110], {
    payloadType: 110,
    parameters: {
      streamtype: '5',
      'profile-level-id': '1',
      mode: 'AAC-hbr',
      config: '1588',
      sizelength: '13',
      indexlength: '3',
      indexdeltalength: '3',
    },
  });
  assert.equal(pickSendCodec(track)?.name, 'g726-32');
  assert.equal(pickSendCodec(track, 'g726-24')?.payloadType, 113);
  assert.equal(pickSendCodec(track, 'g726-32')?.payloadType, 112);
  assert.equal(pickSendCodec(track, 'g726-40')?.payloadType, 111);

  const aac = pickSendCodec(track, 'aac');
  assert.equal(aac?.name, 'aac');
  assert.equal(aac?.clockRate, 8000);
  assert.equal(aac?.fmtp?.parameters.mode, 'AAC-hbr');
});

test('explicit codec preference never falls back to another offered codec', () => {
  const track = findBackchannelAudio(parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 0 8',
    'a=sendonly',
    'a=rtpmap:0 PCMU/8000',
    'a=rtpmap:8 PCMA/8000',
    '',
  ].join('\r\n')));
  assert.ok(track);

  const preferences: Array<[CodecPreference, string | undefined]> = [
    ['auto', 'pcma'],
    ['pcma', 'pcma'],
    ['pcmu', 'pcmu'],
    ['g726-32', undefined],
    ['aac', undefined],
  ];
  for (const [preference, expected] of preferences) {
    assert.equal(pickSendCodec(track, preference)?.name, expected);
  }
});

test('does not select an unsupported first SDP format', () => {
  const track = findBackchannelAudio(parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 96 97',
    'a=sendonly',
    'a=rtpmap:96 OPUS/48000/2',
    'a=rtpmap:97 telephone-event/8000',
    '',
  ].join('\r\n')));
  assert.ok(track);
  assert.equal(pickSendCodec(track), undefined);
});

test('recognizes MP4A-LATM but fails clearly instead of treating it as AAC-hbr', () => {
  const track = findBackchannelAudio(parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 96',
    'a=sendonly',
    'a=rtpmap:96 MP4A-LATM/16000/1',
    'a=fmtp:96 profile-level-id=15; object=2; cpresent=0; config=40002810',
    '',
  ].join('\r\n')));
  assert.ok(track);
  assert.throws(
    () => pickSendCodec(track),
    /MP4A-LATM.*not supported.*RFC 3640 MPEG4-GENERIC AAC-hbr/i,
  );
});

test('rejects MPEG4-GENERIC when its fmtp is not AAC-hbr packetizable', () => {
  const track = findBackchannelAudio(parseSdp([
    'v=0',
    'm=audio 0 RTP/AVP 97',
    'a=sendonly',
    'a=rtpmap:97 MPEG4-GENERIC/8000/1',
    'a=fmtp:97 mode=AAC-lbr; config=1588; SizeLength=6; IndexLength=2',
    '',
  ].join('\r\n')));
  assert.ok(track);
  assert.throws(
    () => pickSendCodec(track, 'aac'),
    /MPEG4-GENERIC.*AAC-hbr.*SizeLength=13.*IndexLength=3/i,
  );
});

test('requires streamtype=5 for MPEG4-GENERIC AAC-hbr', () => {
  const base =
    'mode=AAC-hbr; config=1588; SizeLength=13; IndexLength=3; ' +
    'IndexDeltaLength=3';

  assert.throws(
    () => pickSendCodec(aacTrack(base), 'aac'),
    /streamtype=5/i,
  );
  assert.throws(
    () => pickSendCodec(aacTrack(`streamtype=4; ${base}`), 'aac'),
    /streamtype=5/i,
  );
  assert.equal(
    pickSendCodec(aacTrack(`StreamType=5; ${base}`), 'aac')?.name,
    'aac',
  );
});

test('requires complete AAC-hbr AU header lengths and 1024 constantDuration', () => {
  const base =
    'streamtype=5; mode=AAC-hbr; config=1588; SizeLength=13; IndexLength=3';
  assert.throws(
    () => pickSendCodec(aacTrack(base), 'aac'),
    /IndexDeltaLength=3/,
  );
  assert.throws(
    () => pickSendCodec(aacTrack(`${base}; IndexDeltaLength=2`), 'aac'),
    /IndexDeltaLength=3/,
  );
  assert.throws(
    () => pickSendCodec(
      aacTrack(`${base}; IndexDeltaLength=3; constantDuration=960`),
      'aac',
    ),
    /constantDuration must be 1024/,
  );
  assert.equal(
    pickSendCodec(
      aacTrack(`${base}; IndexDeltaLength=3; constantDuration=1024`),
      'aac',
    )?.name,
    'aac',
  );
});

test('accepts only AAC-LC AudioSpecificConfig matching the rtpmap', () => {
  const fmtp = (config: string) =>
    `streamtype=5; mode=AAC-hbr; config=${config}; SizeLength=13; ` +
    'IndexLength=3; IndexDeltaLength=3';

  assert.equal(pickSendCodec(aacTrack(fmtp('1588')), 'aac')?.name, 'aac');
  const invalid: Array<{
    config: string;
    clockRate?: number;
    channels?: number;
    error: RegExp;
  }> = [
    {
      config: '15',
      error: /AudioSpecificConfig must contain at least 2 bytes/,
    },
    {
      config: '0d88',
      error: /audioObjectType 1 is not supported.*AAC-LC.*2/,
    },
    {
      config: '1788',
      error: /explicit sampling frequency is not supported/,
    },
    {
      config: '1588',
      clockRate: 16000,
      error: /sample rate 8000 does not match rtpmap clock rate 16000/,
    },
    {
      config: '1580',
      error: /channelConfiguration 0 is not supported/,
    },
    {
      config: '1588',
      channels: 2,
      error: /channel count 1 does not match rtpmap channel count 2/,
    },
  ];
  for (const { config, clockRate = 8000, channels = 1, error } of invalid) {
    assert.throws(
      () => pickSendCodec(aacTrack(fmtp(config), clockRate, channels), 'aac'),
      error,
    );
  }
});

test('rejects AAC-LC AudioSpecificConfig flags incompatible with 1024-sample framing', () => {
  const fmtp = (config: string) =>
    `streamtype=5; mode=AAC-hbr; config=${config}; SizeLength=13; ` +
    'IndexLength=3; IndexDeltaLength=3';
  const invalid = [
    ['158c', /frameLengthFlag=1 selects 960-sample AAC-LC frames/],
    ['158a', /dependsOnCoreCoder=1 is unsupported/],
    ['1589', /extensionFlag=1 is unsupported/],
  ] as const;

  for (const [config, error] of invalid) {
    assert.throws(
      () => pickSendCodec(aacTrack(fmtp(config)), 'aac'),
      error,
    );
  }
});

test('rejects non-mono PCMA, PCMU, and G.726 offers', () => {
  const codecs = [
    ['pcma', 'PCMA'],
    ['pcmu', 'PCMU'],
    ['g726-16', 'G726-16'],
    ['g726-24', 'G726-24'],
    ['g726-32', 'G726-32'],
    ['g726-40', 'G726-40'],
  ] as const;
  const payloadTypes = codecs.map((_, index) => 96 + index);
  const track = findBackchannelAudio(parseSdp([
    'v=0',
    `m=audio 0 RTP/AVP ${payloadTypes.join(' ')}`,
    'a=sendonly',
    ...codecs.map(
      ([, encoding], index) => `a=rtpmap:${payloadTypes[index]} ${encoding}/8000/2`,
    ),
    '',
  ].join('\r\n')));
  assert.ok(track);

  assert.equal(pickSendCodec(track), undefined);
  for (const [preference] of codecs) {
    assert.equal(pickSendCodec(track, preference), undefined);
  }
});
