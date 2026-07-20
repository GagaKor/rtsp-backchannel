import assert from 'node:assert/strict';
import { test } from 'node:test';

import { parseProfiles } from './deviceClient.ts';
import { getStreamUris, type StreamLookupDependencies } from './streams.ts';

test('parses profile names and audio capabilities', () => {
  assert.deepEqual(
    parseProfiles(`
      <trt:GetProfilesResponse xmlns:trt="urn:media" xmlns:tt="urn:schema">
        <trt:Profiles token="main&amp;special">
          <tt:Name>Main Stream</tt:Name>
          <tt:AudioEncoderConfiguration/>
          <tt:AudioOutputConfiguration/>
        </trt:Profiles>
      </trt:GetProfilesResponse>
    `),
    [{
      token: 'main&special',
      name: 'Main Stream',
      hasAudioEncoder: true,
      hasAudioOutput: true,
      hasAudioSource: false,
    }],
  );
});

test('returns every profile URI without embedded RTSP credentials', async () => {
  const calls: unknown[] = [];
  const dependencies: StreamLookupDependencies = {
    createDevice: (host, user, pass, options) => {
      calls.push({ host, user, pass, options });
      return {
        connect: async () => {},
        getProfiles: async () => [
          { token: 'main', name: 'Main Stream', hasAudioEncoder: true, hasAudioOutput: false, hasAudioSource: true },
          { token: 'sub', name: 'Sub Stream', hasAudioEncoder: false, hasAudioOutput: false, hasAudioSource: false },
        ],
        getStreamUri: async (token) =>
          token === 'main'
            ? 'RTSP://admin:p@ss@[2001:db8::10]:8554/live?channel=1&stream=main'
            : 'rTsPs://viewer:s%40fe@camera.example:322/live?channel=1&stream=sub',
      };
    },
  };

  const streams = await getStreamUris(
    {
      host: 'camera',
      user: 'admin@example.com',
      pass: 'p@ss:/?#[]',
      deviceUrls: ['http://camera/onvif/device_service'],
      timeoutMs: 1_500,
    },
    dependencies,
  );

  assert.deepEqual(calls, [{
    host: 'camera',
    user: 'admin@example.com',
    pass: 'p@ss:/?#[]',
    options: { deviceUrls: ['http://camera/onvif/device_service'], timeoutMs: 1_500 },
  }]);
  assert.deepEqual(streams, [
    {
      profileToken: 'main',
      profileName: 'Main Stream',
      uri: 'RTSP://[2001:db8::10]:8554/live?channel=1&stream=main',
    },
    {
      profileToken: 'sub',
      profileName: 'Sub Stream',
      uri: 'rTsPs://camera.example:322/live?channel=1&stream=sub',
    },
  ]);
});

test('leaves credential-free, non-RTSP, and malformed stream URIs unchanged', async () => {
  const uris = [
    'rtsp://[2001:db8::20]:554/live@archive?token=a@b',
    'https://user:pass@example.com/video',
    'rtsp://user:pass@[2001:db8::30/live',
  ];
  const dependencies: StreamLookupDependencies = {
    createDevice: () => ({
      connect: async () => {},
      getProfiles: async () => uris.map((_, index) => ({
        token: String(index),
        hasAudioEncoder: false,
        hasAudioOutput: false,
        hasAudioSource: false,
      })),
      getStreamUri: async (token) => uris[Number(token)],
    }),
  };

  const streams = await getStreamUris(
    { host: 'camera', user: '', pass: '' },
    dependencies,
  );

  assert.deepEqual(streams.map((stream) => stream.uri), uris);
});
