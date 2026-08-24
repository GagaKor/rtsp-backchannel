import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import * as library from './index.ts';
import type {
  AdtsFrame,
  AudioCodecName,
  BackchannelOptions,
  CameraCapabilityAudioSend,
  CameraCapabilityOptions,
  CameraCapabilityProfile,
  CameraCapabilityReport,
  CameraCapabilityService,
  CameraCapabilityWarning,
  CodecPreference,
  EncodedAudio,
  EncodedAudioFrame,
  PtzNode,
  PtzServiceCapabilities,
  PtzSession,
  PtzSessionOptions,
  PtzSpaces,
  PtzStatus,
  PtzVector,
  SendCodec,
} from './index.ts';

test('exports the supported npm library surface from one entry point', () => {
  assert.equal(typeof library.playFile, 'function');
  assert.equal(typeof library.openBackchannel, 'function');
  assert.equal(typeof library.fileToG711, 'function');
  assert.equal(typeof library.fileToRtpAudio, 'function');
  assert.equal(typeof library.parseAdtsFrames, 'function');
  assert.equal(typeof library.aacRfc3640Payload, 'function');
  assert.equal(typeof library.sendPacedFrames, 'function');
  assert.equal(typeof library.pcm16ToG711, 'function');
  assert.equal(typeof library.linearToALaw, 'function');
  assert.equal(typeof library.discoverDevices, 'function');
  assert.equal(typeof library.getStreamUris, 'function');
  assert.equal(typeof library.getCameraCapabilities, 'function');
  assert.equal(typeof library.openPtzSession, 'function');
  assert.equal(library.SAMPLE_RATE, 8000);
  assert.equal(library.PACKET_MS, 40);
});

test('exports the codec-neutral public API types', () => {
  const name: AudioCodecName = 'pcma';
  const preference: CodecPreference = name;
  const codec: SendCodec = {
    name,
    payloadType: 8,
    encoding: 'PCMA',
    clockRate: 8000,
  };
  const frame: EncodedAudioFrame = { payload: Buffer.alloc(320), samples: 320 };
  const adtsFrame: AdtsFrame = { ...frame, sampleRate: 8000, channels: 1 };
  const audio: EncodedAudio = {
    codec: name,
    clockRate: codec.clockRate,
    frames: [frame],
    byteLength: frame.payload.length,
    sampleCount: frame.samples,
  };
  const options: BackchannelOptions = { codec: preference };

  assert.deepEqual(
    [audio.codec, adtsFrame.sampleRate, options.codec],
    ['pcma', 8000, 'pcma'],
  );
});

test('exports the complete camera capability report contract', () => {
  const options: CameraCapabilityOptions = {
    host: 'camera.local',
    user: 'operator',
    pass: 'example-only',
    deviceUrls: ['http://camera.local/onvif/device_service'],
    timeoutMs: 8_000,
  };
  const service: CameraCapabilityService = {
    namespace: 'http://www.onvif.org/ver20/media/wsdl',
    xaddr: 'http://camera.local/onvif/media2',
    version: { major: 2, minor: 0 },
  };
  const profile: CameraCapabilityProfile = {
    token: 'main',
    source: 'media2',
    hasAudioEncoder: true,
    hasAudioOutput: false,
    hasAudioSource: true,
    ptzConfigurationToken: 'ptz-main',
    ptzNodeToken: 'node-main',
  };
  const serviceCapabilities: PtzServiceCapabilities = { moveStatus: true };
  const spaces: PtzSpaces = {
    absolutePanTilt: true,
    absoluteZoom: false,
    relativePanTilt: false,
    relativeZoom: false,
    continuousPanTilt: true,
    continuousZoom: true,
  };
  const node: PtzNode = {
    token: 'node-main',
    spaces,
    auxiliaryCommands: [],
  };
  const warning: CameraCapabilityWarning = {
    operation: 'PTZ GetNodes',
    message: 'request timeout',
  };
  const audioSend: CameraCapabilityAudioSend = {
    detected: true,
    transport: 'onvif',
    onvifBackchannel: true,
    vigiTalk: null,
  };
  const report: CameraCapabilityReport = {
    device: { manufacturer: 'Example Camera Vendor' },
    scopes: ['onvif://www.onvif.org/Profile/Streaming'],
    declaredProfiles: ['S'],
    serviceDiscovery: 'getServices',
    services: [service],
    profiles: [profile],
    ptz: {
      detected: true,
      panTiltSupported: true,
      zoomSupported: true,
      profileTokens: ['main'],
      serviceCapabilities,
      nodes: [node],
    },
    media2: {
      detected: true,
      encodings: ['H264', 'H265'],
      h265Supported: true,
    },
    audioSend,
    warnings: [warning],
  };
  const getCapabilities: (
    value: CameraCapabilityOptions,
  ) => Promise<CameraCapabilityReport> = library.getCameraCapabilities;

  assert.equal(typeof getCapabilities, 'function');
  assert.deepEqual([options.host, report.ptz.nodes[0]?.spaces, report.warnings[0]], [
    'camera.local',
    spaces,
    warning,
  ]);
});

test('exports the complete PTZ session contract', () => {
  const options: PtzSessionOptions = {
    host: 'camera.local',
    user: 'operator',
    pass: 'example-only',
    profileToken: 'main',
    deviceUrls: ['http://camera.local/onvif/device_service'],
    timeoutMs: 8_000,
    defaultMoveTimeoutMs: 1_000,
  };
  const vector: PtzVector = { x: 0.5, y: -0.5 };
  const status: PtzStatus = {
    panTilt: vector,
    zoom: 0.25,
    panTiltMoveStatus: 'MOVING',
    zoomMoveStatus: 'IDLE',
    utcTime: '2026-08-10T00:00:00Z',
  };
  const node: PtzNode = {
    token: 'node-main',
    spaces: {
      absolutePanTilt: true,
      absoluteZoom: false,
      relativePanTilt: false,
      relativeZoom: false,
      continuousPanTilt: true,
      continuousZoom: true,
    },
    auxiliaryCommands: [],
  };
  const session: PtzSession = {
    profileToken: options.profileToken as string,
    node,
    continuousMove: async () => {},
    absoluteMove: async () => {},
    relativeMove: async () => {},
    stop: async () => {},
    getStatus: async () => status,
    close: async () => {},
  };
  const openSession: (
    value: PtzSessionOptions,
  ) => Promise<PtzSession> = library.openPtzSession;

  assert.equal(typeof openSession, 'function');
  assert.deepEqual([options.host, session.node.spaces, session.profileToken], [
    'camera.local',
    node.spaces,
    'main',
  ]);
});

test('declares an installable npm package with ESM types and CLI exports', () => {
  const manifest = JSON.parse(readFileSync('package.json', 'utf8'));
  const lockfile = JSON.parse(readFileSync('package-lock.json', 'utf8'));

  assert.equal(manifest.name, 'rtsp-backchannel');
  assert.notEqual(manifest.private, true);
  assert.deepEqual(manifest.files, [
    'dist/index.*',
    'dist/bin.*',
    'dist/cli.*',
    'dist/backchannel.*',
    'dist/audio',
    'dist/onvif',
    'dist/rtp',
    'dist/rtsp',
    'dist/vigi',
    'README.md',
    'README.ko.md',
    'LICENSE',
    'LICENSE-MIT',
    'LICENSE-APACHE',
    'THIRD_PARTY_NOTICES.md',
  ]);
  assert.equal(manifest.license, 'MIT OR Apache-2.0');
  assert.equal(lockfile.packages[''].license, manifest.license);
  assert.equal(manifest.main, './dist/index.js');
  assert.equal(manifest.types, './dist/index.d.ts');
  assert.equal(manifest.exports['.'].import, './dist/index.js');
  assert.equal(manifest.exports['.'].types, './dist/index.d.ts');
  assert.equal(manifest.bin['rtsp-backchannel'], 'dist/bin.js');
  assert.equal(
    manifest.repository.url,
    'git+https://github.com/GagaKor/rtsp-backchannel.git',
  );
  assert.equal(
    manifest.homepage,
    'https://github.com/GagaKor/rtsp-backchannel#readme',
  );
  assert.equal(
    manifest.bugs.url,
    'https://github.com/GagaKor/rtsp-backchannel/issues',
  );
  assert.equal(manifest.scripts.build, 'tsc -p tsconfig.build.json');
  assert.equal(manifest.dependencies?.[manifest.name], undefined);
});

test('ships separate English and Korean TypeScript documentation', () => {
  const english = readFileSync('README.md', 'utf8');
  const korean = readFileSync('README.ko.md', 'utf8');

  assert.match(english, /TypeScript/);
  assert.match(english, /README\.ko\.md/);
  assert.doesNotMatch(english, /```(?:python|rust)/);
  assert.match(korean, /TypeScript/);
  assert.match(korean, /README\.md/);
  assert.doesNotMatch(korean, /```(?:python|rust)/);
  for (const readme of [english, korean]) {
    assert.match(readme, /cidrs/);
    assert.match(readme, /10\.0\.0\.0\/24/);
    assert.match(readme, /10\.128\.0\.10/);
    assert.match(readme, /--cidr/);
    assert.match(readme, /getCameraCapabilities/);
    assert.match(readme, /declaredProfiles/);
    assert.match(readme, /Profile T/);
    assert.match(readme, /media2\.detected/);
    assert.match(readme, /h265Supported/);
    assert.match(readme, /panTiltSupported/);
    assert.match(readme, /warnings/);
    assert.match(readme, /timeoutMs/);
    assert.match(readme, /true/);
    assert.match(readme, /false/);
    assert.match(readme, /null/);
    assert.match(readme, /ONVIF_PASSWORD/);
    assert.match(readme, /capabilities/);
    assert.match(readme, /--device-url/);
  }
});

test('documents exact Media2 discovery and warning guarantees in both languages', () => {
  const english = readFileSync('README.md', 'utf8');
  const korean = readFileSync('README.ko.md', 'utf8');

  assert.match(
    english,
    /`media2\.detected` reports only whether a successful `GetServices` response advertised a Media2 service/,
  );
  assert.match(
    english,
    /can remain `true` when Media2 enrichment\s+requests fail/,
  );
  assert.match(
    korean,
    /`media2\.detected`는 성공한 `GetServices` 응답이 Media2 서비스를 광고했는지만 나타냅니다/,
  );
  assert.match(
    korean,
    /Media2 보강 요청이 실패해도 `true`로 유지될 수 있습니다/,
  );

  for (const readme of [english, korean]) {
    assert.match(readme, /`media2\.h265Supported`/);
    assert.doesNotMatch(readme, /`h265Supported`/);
    assert.match(
      readme,
      /`GetCapabilities`[\s\S]{0,300}`media2\.detected`[\s\S]{0,120}`media2\.h265Supported`[\s\S]{0,120}`null`/,
    );
    assert.match(readme, /`warning\.message`[\s\S]{0,300}generic canonical/);
    assert.match(readme, /`warning\.message`[\s\S]{0,300}credentials/);
    assert.match(readme, /`warning\.message`[\s\S]{0,300}WSSE digest material/);
    assert.match(readme, /`warning\.message`[\s\S]{0,300}URL userinfo/);
    assert.match(readme, /`warning\.message`[\s\S]{0,300}raw or real camera response payload/);
  }

  assert.match(
    english,
    /Initial connection and authentication failures are fatal[\s\S]{0,100}reject/,
  );
  assert.match(
    korean,
    /최초 연결 또는\s+인증 실패는 치명적이며[\s\S]{0,100}reject/,
  );
});

test('ships clean public declarations without capability parser or injection seams', () => {
  const build = spawnSync('npm', ['run', 'build'], { encoding: 'utf8' });
  assert.equal(build.status, 0, build.stderr || build.stdout);

  const discoveryDeclaration = readFileSync('dist/onvif/discovery.d.ts', 'utf8');
  assert.doesNotMatch(discoveryDeclaration, /DiscoveryDependencies/);
  assert.match(
    discoveryDeclaration,
    /discoverDevices\(options\?: DiscoveryOptions\): Promise<DiscoveredDevice\[\]>/,
  );

  // ptz.ts hides its injection seam the same way discovery.ts and
  // capabilities.ts hide theirs: openPtzSession takes options only, and
  // PtzSessionDependencies/PtzSessionDevice live behind the internal-only
  // openPtzSessionWithDependencies. This is the ptz.d.ts counterpart of the
  // discovery.d.ts and index.d.ts/cli.d.ts assertions above and below — the
  // gap this closes is exactly what let PtzSessionDependencies and
  // PtzSessionDevice leak into shipped declarations undetected.
  const ptzDeclaration = readFileSync('dist/onvif/ptz.d.ts', 'utf8');
  assert.doesNotMatch(ptzDeclaration, /PtzSessionDependencies|PtzSessionDevice|WithDependencies/);
  assert.match(
    ptzDeclaration,
    /export declare function openPtzSession\(options: PtzSessionOptions\): Promise<PtzSession>;/,
  );

  const indexDeclaration = readFileSync('dist/index.d.ts', 'utf8');
  const cliDeclaration = readFileSync('dist/cli.d.ts', 'utf8');
  assert.match(indexDeclaration, /export \{ getCameraCapabilities \}/);
  for (const typeName of [
    'CameraCapabilityAudioSend',
    'CameraCapabilityOptions',
    'CameraCapabilityProfile',
    'CameraCapabilityReport',
    'CameraCapabilityService',
    'CameraCapabilityWarning',
    'PtzNode',
    'PtzServiceCapabilities',
    'PtzSpaces',
  ]) {
    assert.match(indexDeclaration, new RegExp(`\\b${typeName}\\b`));
  }
  assert.match(cliDeclaration, /getCameraCapabilities: typeof getCameraCapabilities/);

  assert.match(indexDeclaration, /export \{ openPtzSession \}/);
  for (const typeName of [
    'PtzSession',
    'PtzSessionOptions',
    'PtzStatus',
    'PtzVector',
  ]) {
    assert.match(indexDeclaration, new RegExp(`\\b${typeName}\\b`));
  }

  const publicDeclarations = `${indexDeclaration}\n${cliDeclaration}`;
  assert.doesNotMatch(
    publicDeclarations,
    /CameraCapability(?:Dependencies|Device)|OnvifRawResponse|getCameraCapabilitiesWithDependencies/,
  );
  assert.doesNotMatch(
    publicDeclarations,
    /OnvifResponseError|ParsedServiceDiscovery|ParsedPtzNodes|parseScopesResponse|parseServicesResponse|parseCapabilitiesResponse|selectService|parseMedia1ProfilesResponse|parseMedia2ProfilesResponse|parsePtz|parseMedia2OptionsResponse/,
  );
  assert.doesNotMatch(
    publicDeclarations,
    /PtzResponseError|formatPtzNumber|formatPtzDuration/,
  );
});
