import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
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

/**
 * The repo root, derived from this file rather than from `process.cwd()`.
 *
 * The build and the CommonJS probe below both resolve paths relative to their
 * working directory: `npm run build` picks the package to build from it, and
 * `require('rtsp-backchannel')` resolves through Node's package
 * self-reference, which keys off the *referrer's* package scope. Run
 * `node --test` from anywhere but the repo root and the probe silently loads a
 * different copy of the package -- a published one from a parent project's
 * node_modules, say -- while still reporting green.
 */
const REPO_ROOT = fileURLToPath(new URL('..', import.meta.url));

/** Spawn budget for the two npm/node subprocesses, so a hang fails the run. */
const SUBPROCESS_TIMEOUT_MS = 300_000;

let buildResult: ReturnType<typeof spawnSync> | undefined;

/**
 * Builds `dist/` at most once per run. Two tests need it and node:test runs
 * top-level tests in a file sequentially, so the second `npm run build` was a
 * whole extra npm + tsc process producing output that was already fresh.
 */
function buildOnce(): void {
  buildResult ??= spawnSync('npm', ['run', 'build'], {
    encoding: 'utf8',
    cwd: REPO_ROOT,
    // Windows resolves `npm` to `npm.cmd` through PATHEXT, which spawnSync
    // does not apply -- without a shell it fails with ENOENT.
    shell: process.platform === 'win32',
    timeout: SUBPROCESS_TIMEOUT_MS,
  });
  assert.equal(buildResult.status, 0, spawnFailure(buildResult));
}

/**
 * A failure message that survives a spawn that never ran. `spawnSync` reports
 * ENOENT and timeouts through `error` with `status: null` and no stderr, so
 * asserting on stderr alone yields the message `null`.
 */
function spawnFailure(result: ReturnType<typeof spawnSync>): string {
  return result.error
    ? `${result.error.message} (signal ${String(result.signal)})`
    : String(result.stderr || result.stdout);
}

test('exports the supported npm library surface from one entry point', () => {
  // Every export is a compatibility commitment (0.4.0 dropped
  // openOnvifBackchannel/openVigiBackchannel for exactly this reason: they
  // were redundant with openBackchannel(..., { transport })). A presence-only
  // check can never catch an unintended addition, only a removal, so this
  // asserts the exact runtime export set — an unreviewed new export fails the
  // suite the same way a removed one would.
  assert.deepEqual(Object.keys(library).sort(), [
    'BackchannelUnavailableError',
    'PACKET_MS',
    'SAMPLE_RATE',
    'VigiControlError',
    'aacRfc3640Payload',
    'createVigiTalkSession',
    'discoverDevices',
    'fileToG711',
    'fileToRtpAudio',
    'generateTonePcm',
    'getCameraCapabilities',
    'getStreamUris',
    'linearToALaw',
    'linearToMuLaw',
    'openBackchannel',
    'openPtzSession',
    'openVigiControl',
    'parseAdtsFrames',
    'pcm16ToG711',
    'playFile',
    'sendPacedFrames',
    'sendPacedG711',
  ]);
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
  assert.equal(typeof library.openVigiControl, 'function');
  assert.equal(typeof library.VigiControlError, 'function');
  assert.equal(typeof library.createVigiTalkSession, 'function');
  assert.equal(typeof library.BackchannelUnavailableError, 'function');
  assert.equal(typeof library.generateTonePcm, 'function');
  assert.equal(typeof library.linearToMuLaw, 'function');
  assert.equal(library.SAMPLE_RATE, 8000);
  assert.equal(library.PACKET_MS, 40);
});

test('does not export the removed transport-specific backchannel openers', () => {
  // openOnvifBackchannel and openVigiBackchannel are redundant with
  // openBackchannel(host, user, pass, { transport: 'onvif' | 'vigi' }).
  // They still exist as exports of src/backchannel.ts itself for the
  // internal tests, but must not reappear on the curated public surface.
  assert.equal((library as Record<string, unknown>).openOnvifBackchannel, undefined);
  assert.equal((library as Record<string, unknown>).openVigiBackchannel, undefined);
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

test('declares an installable npm package with ESM and CommonJS entry points', () => {
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
  // Both conditions resolve to the same ESM file: CommonJS callers reach it
  // through Node's require(esm), unflagged since 22.12.0. Pointing them at one
  // artifact is what makes the dual-package hazard impossible rather than
  // merely unlikely -- a process can only ever hold one module instance.
  assert.equal(manifest.exports['.'].require, './dist/index.js');
  // A resolver that offers neither condition -- a restricted --conditions set,
  // or a bundler with its own conditionNames -- matched nothing and hit the
  // same ERR_PACKAGE_PATH_NOT_EXPORTED the require condition was added to fix.
  // `default` must stay last: conditions are matched in declaration order.
  assert.equal(manifest.exports['.'].default, './dist/index.js');
  assert.deepEqual(Object.keys(manifest.exports['.']), [
    'types', 'import', 'require', 'default',
  ]);
  // Tooling that introspects installed packages (ESLint import resolvers,
  // read-pkg-up-style helpers, several bundler plugins) resolves the manifest
  // by subpath; without this it gets ERR_PACKAGE_PATH_NOT_EXPORTED.
  assert.equal(manifest.exports['./package.json'], './package.json');
  // `import` works on every Node 22, so engines allows all of them. The 22.12
  // floor belongs to require(esm) alone and is documented, not enforced here:
  // narrowing engines only warns the CommonJS user (npm's EBADENGINE is a
  // warning by default) while hard-failing ESM-only installs under
  // engine-strict. See docs/decisions/2026-08-25-cjs-support-boundaries.md.
  assert.equal(manifest.engines.node, '>=22');
  // #36's "raised the minimum Node.js version from 22 to 22.12.0" entry
  // outlived the change it described, because nothing tied the CHANGELOG to
  // the manifest -- 289 tests passed with the two contradicting each other.
  // Any engines range quoted in the unreleased section must be the real one,
  // and the real one must appear, so a future bump cannot land silently.
  const changelog = readFileSync('CHANGELOG.md', 'utf8');
  const unreleasedStart = changelog.indexOf('## [Unreleased]');
  assert.notEqual(unreleasedStart, -1, 'CHANGELOG must keep an [Unreleased] section');
  const nextRelease = changelog.indexOf('\n## [', unreleasedStart + 1);
  const unreleased = changelog.slice(
    unreleasedStart,
    nextRelease === -1 ? undefined : nextRelease,
  );
  for (const [, quoted] of unreleased.matchAll(/`(>=[\d.]+)`/g)) {
    assert.equal(
      quoted,
      manifest.engines.node,
      `CHANGELOG quotes engines ${quoted}, manifest says ${manifest.engines.node}`,
    );
  }
  assert.ok(
    unreleased.includes(`\`${manifest.engines.node}\``),
    `the unreleased CHANGELOG must state the current engines range (${manifest.engines.node})`,
  );
  // npm rewrites the lockfile's own engines block from the manifest on the next
  // install, so a stale value here is a guaranteed unrelated diff in someone
  // else's PR -- and RELEASING.md requires the two files to agree.
  assert.equal(lockfile.packages[''].engines.node, manifest.engines.node);
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
    // Both module entry points, and the consumer-side cause of the
    // MODULE_TYPELESS_PACKAGE_JSON warning, have to be documented in both
    // languages -- the warning names the reader's own package.json, so a
    // reader who only has the Korean README still needs to be told that.
    assert.match(readme, /require\('rtsp-backchannel'\)/);
    assert.match(readme, /"type": "module"/);
    assert.match(readme, /MODULE_TYPELESS_PACKAGE_JSON/);
    assert.match(readme, /\.mjs/);
    // The require(esm) floor, stated as a literal because it is a Node fact
    // rather than a manifest one -- engines deliberately does not carry it.
    assert.match(readme, /22\.12/);
    // ...and the engines range itself, derived so a future bump cannot leave
    // the READMEs stale while the manifest assertion above still passes.
    const minimum = JSON.parse(readFileSync('package.json', 'utf8'))
      .engines.node.replace(/^>=/, '');
    assert.ok(
      readme.includes(`Node.js ${minimum} `) || readme.includes(`Node.js ${minimum}\n`),
      `both READMEs must state the engines minimum (Node.js ${minimum})`,
    );
    // The two CommonJS caveats the single-artifact design imposes: TypeScript
    // needs nodenext resolution, and require() hands back a sealed namespace.
    assert.match(readme, /nodenext/);
    assert.match(readme, /TS1479/);
    assert.match(readme, /Cannot redefine property/);
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
  buildOnce();

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

  // vigi/talk.ts and vigi/control.ts hide their injection seams the same
  // way: createVigiTalkSession and openVigiControl take no dependencies
  // parameter, and VigiTalkSocket/VigiTalkDependencies/VigiControlDependencies
  // live behind the internal-only *WithDependencies variants. package.json
  // ships the whole dist/vigi directory (not just index.d.ts), so a leak
  // here is reachable by a deep import even though src/index.ts never
  // re-exports these types — exactly the gap this closes.
  const vigiTalkDeclaration = readFileSync('dist/vigi/talk.d.ts', 'utf8');
  assert.doesNotMatch(
    vigiTalkDeclaration,
    /VigiTalkSocket|VigiTalkDependencies|WithDependencies/,
  );
  assert.match(
    vigiTalkDeclaration,
    /export declare function createVigiTalkSession\(options: VigiTalkOptions\): VigiTalkSession;/,
  );

  const vigiControlDeclaration = readFileSync('dist/vigi/control.d.ts', 'utf8');
  assert.doesNotMatch(
    vigiControlDeclaration,
    /VigiControlDependencies|WithDependencies/,
  );
  assert.match(
    vigiControlDeclaration,
    /export declare function openVigiControl\(options: VigiControlOptions\): Promise<VigiControlSession>;/,
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

test('loads from CommonJS with the same export surface as ESM', () => {
  buildOnce();

  const probe = spawnSync(
    process.execPath,
    [
      '-e',
      // Requiring by package name rather than by path is what puts the
      // exports "require" condition under test: a path require bypasses
      // exports entirely and would keep passing if the condition were dropped.
      "const loaded = require('rtsp-backchannel');\n" +
        'process.stdout.write(JSON.stringify({\n' +
        '  playFile: typeof loaded.playFile,\n' +
        '  openPtzSession: typeof loaded.openPtzSession,\n' +
        '  sampleRate: loaded.SAMPLE_RATE,\n' +
        "  keys: Object.keys(loaded).filter((key) => key !== 'default').sort(),\n" +
        '}));\n',
    ],
    { encoding: 'utf8', cwd: REPO_ROOT, timeout: SUBPROCESS_TIMEOUT_MS },
  );

  // package.json exposes one artifact under both the import and require
  // conditions, so CommonJS callers arrive here through Node's require(esm).
  // That path refuses an asynchronous module outright
  // (ERR_REQUIRE_ASYNC_MODULE), which makes this assertion the guard that
  // keeps top-level await out of the published graph: adding one anywhere
  // reachable from index.ts breaks every require() consumer, and this test
  // is what says so before a release does.
  assert.equal(probe.status, 0, spawnFailure(probe));

  const loaded = JSON.parse(probe.stdout);
  assert.equal(loaded.playFile, 'function');
  assert.equal(loaded.openPtzSession, 'function');
  assert.equal(loaded.sampleRate, library.SAMPLE_RATE);
  assert.deepEqual(
    loaded.keys,
    Object.keys(library)
      .filter((key) => key !== 'default')
      .sort(),
  );
});
