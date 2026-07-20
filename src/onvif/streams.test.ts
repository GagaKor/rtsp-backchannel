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

test('returns every profile URI unchanged and keeps credentials transport-only', async () => {
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
            ? 'rtsp://camera/live?channel=1&stream=main'
            : 'rtsp://camera/live?channel=1&stream=sub',
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
    { profileToken: 'main', profileName: 'Main Stream', uri: 'rtsp://camera/live?channel=1&stream=main' },
    { profileToken: 'sub', profileName: 'Sub Stream', uri: 'rtsp://camera/live?channel=1&stream=sub' },
  ]);
  assert.ok(streams.every((stream) => !stream.uri.includes('p@ss')));
});
