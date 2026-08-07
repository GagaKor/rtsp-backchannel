import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import * as cli from './cli.ts';
import type {
  CommandDependencies as PublicCommandDependencies,
  PlaybackDependencies as PublicPlaybackDependencies,
} from './cli.ts';
import { fileToG711 } from './audio/transcode.ts';
import { openBackchannel } from './backchannel.ts';

test('prints TypeScript playback help without opening a camera connection', () => {
  const result = spawnSync(
    process.execPath,
    ['--experimental-transform-types', 'src/cli.ts', '--help'],
    { encoding: 'utf8' },
  );

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--file/);
  assert.match(result.stdout, /--volume/);
  assert.match(result.stdout, /default: 0\.05/);
  assert.match(result.stdout, /--codec <name>/);
  assert.match(result.stdout, /SDP codec negotiation/);
  assert.match(result.stdout, /real-time pacing/);
});

test('documents the capabilities command in global and command help without exposing values', () => {
  for (const argv of [
    ['--help'],
    ['capabilities', '--help', '--pass', 'help-only-secret'],
  ]) {
    const result = spawnSync(
      process.execPath,
      ['--experimental-transform-types', 'src/cli.ts', ...argv],
      { encoding: 'utf8' },
    );

    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /rtsp-backchannel capabilities/);
    assert.match(result.stdout, /--host <camera>/);
    assert.match(result.stdout, /--user <user>/);
    assert.match(result.stdout, /--pass <password>/);
    assert.match(result.stdout, /--device-url <url>/);
    assert.match(result.stdout, /--timeout-ms <ms>/);
    assert.doesNotMatch(`${result.stdout}\n${result.stderr}`, /help-only-secret/);
  }
});

test('rejects a capability terminator before honoring a trailing help flag', () => {
  const result = spawnSync(
    process.execPath,
    [
      '--experimental-transform-types',
      'src/cli.ts',
      'capabilities',
      '--',
      '--help',
    ],
    { encoding: 'utf8' },
  );

  assert.equal(result.status, 1);
  assert.match(result.stderr, /capabilities does not accept an argument terminator/);
  assert.doesNotMatch(result.stdout, /Usage: rtsp-backchannel/);
});

test('rejects a missing capability password before honoring help as its value', () => {
  const result = spawnSync(
    process.execPath,
    [
      '--experimental-transform-types',
      'src/cli.ts',
      'capabilities',
      '--host',
      'camera.local',
      '--pass',
      '--help',
    ],
    { encoding: 'utf8' },
  );

  assert.equal(result.status, 1);
  assert.match(result.stderr, /missing value for --pass/);
  assert.doesNotMatch(result.stdout, /Usage: rtsp-backchannel/);
});

test('runs the dedicated npm binary entry point', () => {
  const result = spawnSync(
    process.execPath,
    ['--experimental-transform-types', 'src/bin.ts', '--help'],
    { encoding: 'utf8' },
  );

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Usage: rtsp-backchannel/);
  assert.match(result.stdout, /--file/);
});

test('keeps credential-like capability hosts out of npm bin errors', () => {
  const result = spawnSync(
    process.execPath,
    [
      '--experimental-transform-types',
      'src/bin.ts',
      'capabilities',
      '--host',
      'viewer:top-secret@camera',
      '--timeout-ms',
      '1',
    ],
    { encoding: 'utf8' },
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /ONVIF connect failed/);
  assert.doesNotMatch(
    `${result.stdout}\n${result.stderr}`,
    /viewer|secret|@camera/,
  );
});

test('keeps fileToRtpAudio optional for legacy dependency injection', () => {
  const dependencies: PublicPlaybackDependencies = {
    openBackchannel,
    fileToG711,
    log: () => {},
  };

  assert.equal(dependencies.fileToRtpAudio, undefined);
});

test('uses the built-in RTP encoder when an injected dependency omits it', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'onvif-cli-ffmpeg-'));
  const fakeFfmpeg = path.join(directory, 'ffmpeg');
  await writeFile(
    fakeFfmpeg,
    `#!${process.execPath}\nprocess.stdout.write(Buffer.alloc(1280));\n`,
  );
  await chmod(fakeFfmpeg, 0o755);
  const previousPath = process.env.PATH;
  process.env.PATH = `${directory}${path.delimiter}${previousPath ?? ''}`;
  let sent: unknown;
  let closed = 0;

  try {
    const dependencies: PublicPlaybackDependencies = {
      openBackchannel: async () => ({
        codec: {
          name: 'pcma',
          payloadType: 8,
          encoding: 'PCMA',
          clockRate: 8000,
        },
        variant: 'PCMA',
        payloadType: 8,
        clockRate: 8000,
        rtpChannel: 0,
        send: async (audio) => {
          sent = audio;
          return 2;
        },
        close: async () => {
          closed++;
        },
      }),
      fileToG711: async () => {
        throw new Error('legacy encoder must not be used');
      },
      log: () => {},
    };

    const packets = await cli.playFile(
      { host: 'camera', file: 'announcement.wav' },
      dependencies,
    );

    assert.equal(packets, 2);
    assert.equal(closed, 1);
    assert.ok(sent && !Buffer.isBuffer(sent));
    assert.deepEqual(
      (sent as { codec: string; sampleCount: number }).codec,
      'pcma',
    );
    assert.equal((sent as { sampleCount: number }).sampleCount, 640);
  } finally {
    process.env.PATH = previousPath;
    await rm(directory, { recursive: true, force: true });
  }
});

test('parses the validated 0.05 volume default and rejects invalid gain', () => {
  type Parsed = { volume: number };
  type Parser = (argv: string[]) => Parsed;
  const parse = (cli as unknown as { parseCliArgs?: Parser }).parseCliArgs;
  assert.ok(parse);

  const required = ['--host', 'camera', '--pass', 'secret', '--file', 'event.mp3'];
  assert.equal(parse(required).volume, 0.05);
  for (const volume of ['nan', '-0.1', '1.1']) {
    assert.throws(
      () => parse([...required, '--volume', volume]),
      /volume must be finite and between 0 and 1/,
    );
  }
});

test('requires a target but defaults credentials to empty and codec to auto', () => {
  const previous = process.env.ONVIF_PASSWORD;
  delete process.env.ONVIF_PASSWORD;
  try {
    assert.throws(
      () => cli.parseCliArgs(['--pass', 'secret', '--file', 'event.mp3']),
      /missing --host/,
    );
    const parsed = cli.parseCliArgs(['--host', 'camera', '--file', 'event.mp3']) as {
      user: string;
      pass: string;
      codec?: string;
    };
    assert.equal(parsed.user, '');
    assert.equal(parsed.pass, '');
    assert.equal(parsed.codec, 'auto');
  } finally {
    if (previous === undefined) delete process.env.ONVIF_PASSWORD;
    else process.env.ONVIF_PASSWORD = previous;
  }
});

test('accepts only public codec preference values', () => {
  const required = ['--host', 'camera', '--file', 'event.mp3'];
  const codecs = [
    'auto', 'pcma', 'pcmu', 'g726-16', 'g726-24', 'g726-32', 'g726-40', 'aac',
  ];
  for (const codec of codecs) {
    const parsed = cli.parseCliArgs([...required, '--codec', codec]) as unknown as {
      codec?: string;
    };
    assert.equal(parsed.codec, codec);
  }
  assert.throws(
    () => cli.parseCliArgs([...required, '--codec', 'opus']),
    /codec must be one of/,
  );
});

test('uses ONVIF_PASSWORD when --pass is omitted', () => {
  type Parsed = { pass: string };
  type Parser = (argv: string[]) => Parsed;
  const parse = (cli as unknown as { parseCliArgs?: Parser }).parseCliArgs;
  assert.ok(parse);
  const previous = process.env.ONVIF_PASSWORD;
  process.env.ONVIF_PASSWORD = 'environment-secret';
  try {
    assert.equal(
      parse(['--host', 'camera', '--file', 'event.mp3']).pass,
      'environment-secret',
    );
  } finally {
    if (previous === undefined) delete process.env.ONVIF_PASSWORD;
    else process.env.ONVIF_PASSWORD = previous;
  }
});

interface FakeSession {
  variant: 'PCMA';
  payloadType: number;
  rtpChannel: number;
  send(audio: Buffer): Promise<number>;
  close(): Promise<void>;
}

interface PlaybackDependencies {
  openBackchannel(host: string, user: string, pass: string): Promise<FakeSession>;
  fileToG711(file: string, variant: 'PCMA', volume: number): Promise<Buffer>;
  log(message: string): void;
}

type PlayFile = (
  options: {
    host: string;
    user: string;
    pass: string;
    file: string;
    volume: number;
  },
  dependencies: PlaybackDependencies,
) => Promise<number>;

function playFile(): PlayFile {
  const candidate = (cli as unknown as { playFile?: PlayFile }).playFile;
  assert.ok(candidate);
  return candidate;
}

test('passes volume 0.05 to the TypeScript encoder and sends the result once', async () => {
  const encoded = Buffer.alloc(640, 0xd5);
  let closed = 0;
  const dependencies: PlaybackDependencies = {
    openBackchannel: async (host, user, pass) => {
      assert.deepEqual([host, user, pass], ['camera', 'admin', 'secret']);
      return {
        variant: 'PCMA',
        payloadType: 8,
        rtpChannel: 6,
        send: async (audio) => {
          assert.equal(audio, encoded);
          return 2;
        },
        close: async () => {
          closed++;
        },
      };
    },
    fileToG711: async (file, variant, volume) => {
      assert.deepEqual([file, variant, volume], ['event.mp3', 'PCMA', 0.05]);
      return encoded;
    },
    log: () => {},
  };

  const packets = await playFile()(
    {
      host: 'camera',
      user: 'admin',
      pass: 'secret',
      file: 'event.mp3',
      volume: 0.05,
    },
    dependencies,
  );

  assert.equal(packets, 2);
  assert.equal(closed, 1);
});

test('passes codec preference through negotiation and sends codec-neutral file frames', async () => {
  const codec = {
    name: 'g726-32' as const,
    payloadType: 97,
    encoding: 'G726-32',
    clockRate: 8000,
  };
  const encoded = {
    codec: 'g726-32' as const,
    clockRate: 8000,
    frames: [{ payload: Buffer.alloc(160), samples: 320 }],
    byteLength: 160,
    sampleCount: 320,
  };
  const calls: unknown[] = [];
  const modernPlayFile = cli.playFile as unknown as (
    options: {
      host: string;
      user?: string;
      pass?: string;
      file: string;
      volume?: number;
      codec?: string;
    },
    dependencies: {
      openBackchannel(
        host: string,
        user?: string,
        pass?: string,
        options?: { codec?: string },
      ): Promise<{
        codec: typeof codec;
        payloadType: number;
        clockRate: number;
        rtpChannel: number;
        send(audio: typeof encoded): Promise<number>;
        close(): Promise<void>;
      }>;
      fileToG711(): Promise<Buffer>;
      fileToRtpAudio(
        file: string,
        selected: typeof codec,
        volume: number,
      ): Promise<typeof encoded>;
      log(message: string): void;
    },
  ) => Promise<number>;

  const packets = await modernPlayFile(
    {
      host: 'rtsp://embedded:secret@camera/live',
      file: 'event.mp3',
      codec: 'g726-32',
    },
    {
      openBackchannel: async (host, user, pass, options) => {
        calls.push({ host, user, pass, options });
        return {
          codec,
          payloadType: 97,
          clockRate: 8000,
          rtpChannel: 2,
          send: async (audio) => {
            assert.equal(audio, encoded);
            return 1;
          },
          close: async () => {},
        };
      },
      fileToG711: async () => {
        throw new Error('legacy encoder must not be used');
      },
      fileToRtpAudio: async (file, selected, volume) => {
        assert.deepEqual([file, selected, volume], ['event.mp3', codec, 0.05]);
        return encoded;
      },
      log: () => {},
    },
  );

  assert.equal(packets, 1);
  assert.deepEqual(calls, [{
    host: 'rtsp://embedded:secret@camera/live',
    user: '',
    pass: '',
    options: { codec: 'g726-32' },
  }]);
});

test('keeps the RTSP session alive while codec-neutral file encoding runs', async () => {
  const codec = {
    name: 'g726-32' as const,
    payloadType: 97,
    encoding: 'G726-32',
    clockRate: 8000,
  };
  const encoded = {
    codec: 'g726-32' as const,
    clockRate: 8000,
    frames: [{ payload: Buffer.alloc(160), samples: 320 }],
    byteLength: 160,
    sampleCount: 320,
  };
  const events: string[] = [];

  const packets = await cli.playFile(
    { host: 'camera', file: 'event.mp3' },
    {
      openBackchannel: async () => ({
        codec,
        payloadType: 97,
        clockRate: 8000,
        rtpChannel: 2,
        async withKeepAlive<T>(operation: () => Promise<T>): Promise<T> {
          events.push('maintain:start');
          const result = await operation();
          events.push('maintain:end');
          return result;
        },
        send: async (audio) => {
          assert.equal(audio, encoded);
          events.push('send');
          return 1;
        },
        close: async () => {
          events.push('close');
        },
      }),
      fileToG711: async () => {
        throw new Error('legacy encoder must not be used');
      },
      fileToRtpAudio: async () => {
        events.push('encode');
        return encoded;
      },
      log: () => {},
    },
  );

  assert.equal(packets, 1);
  assert.deepEqual(events, [
    'maintain:start',
    'encode',
    'maintain:end',
    'send',
    'close',
  ]);
});

test('surfaces an encoding keepalive failure and still closes the session', async () => {
  const codec = {
    name: 'pcma' as const,
    payloadType: 8,
    encoding: 'PCMA',
    clockRate: 8000,
  };
  const encoded = {
    codec: 'pcma' as const,
    clockRate: 8000,
    frames: [{ payload: Buffer.alloc(320), samples: 320 }],
    byteLength: 320,
    sampleCount: 320,
  };
  let sends = 0;
  let closes = 0;

  await assert.rejects(
    cli.playFile(
      { host: 'camera', file: 'event.mp3' },
      {
        openBackchannel: async () => ({
          codec,
          variant: 'PCMA',
          payloadType: 8,
          clockRate: 8000,
          rtpChannel: 0,
          async withKeepAlive<T>(operation: () => Promise<T>): Promise<T> {
            await operation();
            throw new Error('RTSP keepalive 500 Session Expired');
          },
          send: async () => {
            sends++;
            return 1;
          },
          close: async () => {
            closes++;
          },
        }),
        fileToG711: async () => Buffer.alloc(0),
        fileToRtpAudio: async () => encoded,
        log: () => {},
      },
    ),
    /RTSP keepalive 500 Session Expired/,
  );

  assert.equal(sends, 0);
  assert.equal(closes, 1);
});

test('keeps credential-bearing RTSP targets out of playback and stream logs', async () => {
  const logs: string[] = [];
  const codec = {
    name: 'pcma' as const,
    payloadType: 8,
    encoding: 'PCMA',
    clockRate: 8000,
  };
  const encoded = {
    codec: 'pcma' as const,
    clockRate: 8000,
    frames: [{ payload: Buffer.alloc(320), samples: 320 }],
    byteLength: 320,
    sampleCount: 320,
  };
  const rawTarget = 'rtsp://camera-user:p@ss@camera.test/live';
  const dependencies: PublicCommandDependencies = {
    openBackchannel: async (host) => {
      assert.equal(host, rawTarget);
      return {
        codec,
        variant: 'PCMA',
        payloadType: 8,
        clockRate: 8000,
        rtpChannel: 0,
        send: async () => 1,
        close: async () => {},
      };
    },
    fileToG711: async () => Buffer.alloc(0),
    fileToRtpAudio: async () => encoded,
    log: (message) => logs.push(message),
    discoverDevices: async () => [],
    getStreamUris: async () => [{
      profileToken: 'main',
      uri: 'rtsp://stream-user:p%40ss@camera.test/live',
    }],
    getCameraCapabilities: async () => {
      throw new Error('capabilities should not run');
    },
  };

  await cli.playFile({ host: rawTarget, file: 'tone.wav' }, dependencies);
  assert.match(logs[0], /rtsp:\/\/camera\.test\/live/);
  assert.ok(logs.every((message) => !/camera-user|p@ss/.test(message)));

  logs.length = 0;
  await cli.main(['streams', '--host', 'camera.test'], dependencies);
  assert.deepEqual(JSON.parse(logs[0]), {
    profileToken: 'main',
    uri: 'rtsp://camera.test/live',
  });
  assert.doesNotMatch(logs[0], /stream-user|p%40ss/);
});

test('closes the RTSP session when file conversion fails', async () => {
  let closed = 0;
  const dependencies: PlaybackDependencies = {
    openBackchannel: async () => ({
      variant: 'PCMA',
      payloadType: 8,
      rtpChannel: 6,
      send: async () => 0,
      close: async () => {
        closed++;
      },
    }),
    fileToG711: async () => {
      throw new Error('decode failed');
    },
    log: () => {},
  };

  await assert.rejects(
    playFile()(
      {
        host: 'camera',
        user: 'admin',
        pass: 'secret',
        file: 'broken.mp3',
        volume: 0.05,
      },
      dependencies,
    ),
    /decode failed/,
  );
  assert.equal(closed, 1);
});

test('preserves playback and cleanup errors when both fail', async () => {
  const dependencies: PlaybackDependencies = {
    openBackchannel: async () => ({
      variant: 'PCMA',
      payloadType: 8,
      rtpChannel: 6,
      send: async () => 0,
      close: async () => {
        throw new Error('TEARDOWN failed');
      },
    }),
    fileToG711: async () => {
      throw new Error('decode failed');
    },
    log: () => {},
  };

  await assert.rejects(
    playFile()(
      {
        host: 'camera',
        user: 'admin',
        pass: 'secret',
        file: 'broken.mp3',
        volume: 0.05,
      },
      dependencies,
    ),
    (error: unknown) => {
      assert.ok(error instanceof AggregateError);
      assert.match(error.message, /decode failed/);
      assert.match(error.message, /TEARDOWN failed/);
      assert.deepEqual(
        error.errors.map((entry) => (entry as Error).message),
        ['decode failed', 'TEARDOWN failed'],
      );
      return true;
    },
  );
});

interface CommandDependencies extends PlaybackDependencies {
  discoverDevices(options: unknown): Promise<unknown[]>;
  getStreamUris(options: unknown): Promise<unknown[]>;
  getCameraCapabilities(options: unknown): Promise<unknown>;
}

type CommandMain = (
  argv: string[],
  dependencies: CommandDependencies,
) => Promise<void>;

function commandMain(): CommandMain {
  const candidate = (cli as unknown as { main?: CommandMain }).main;
  assert.ok(candidate);
  return candidate;
}

function commandDependencies(logs: string[]): CommandDependencies {
  return {
    openBackchannel: async () => {
      throw new Error('playback should not run');
    },
    fileToG711: async () => {
      throw new Error('playback should not run');
    },
    log: (message) => logs.push(message),
    discoverDevices: async () => [],
    getStreamUris: async () => [],
    getCameraCapabilities: async () => {
      throw new Error('capabilities should not run');
    },
  };
}

test('dispatches discover and streams commands as JSON Lines', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  dependencies.discoverDevices = async (options) => {
    assert.deepEqual(options, {
      timeoutMs: 1_500,
      cidrs: ['10.128.10.0/24', '192.168.20.0/24'],
      ports: [80, 8000],
      concurrency: 16,
    });
    return [{ ip: '10.128.10.141', xaddrs: [], scopes: [], name: 'Front Door' }];
  };

  await commandMain()(
    [
      'discover',
      '--timeout-ms',
      '1500',
      '--cidr',
      '10.128.10.0/24',
      '--cidr',
      '192.168.20.0/24',
      '--port',
      '80',
      '--port',
      '8000',
      '--concurrency',
      '16',
    ],
    dependencies,
  );
  assert.deepEqual(JSON.parse(logs.pop() ?? ''), {
    ip: '10.128.10.141',
    xaddrs: [],
    scopes: [],
    name: 'Front Door',
  });

  dependencies.getStreamUris = async (options) => {
    assert.deepEqual(options, {
      host: 'camera',
      user: 'admin',
      pass: 'p@ss:/?#[]',
      deviceUrls: ['http://camera/onvif/device_service'],
    });
    return [{ profileToken: 'main', profileName: 'Main', uri: 'rtsp://camera/live' }];
  };
  await commandMain()(
    [
      'streams', '--host', 'camera', '--user', 'admin', '--pass', 'p@ss:/?#[]',
      '--device-url', 'http://camera/onvif/device_service',
    ],
    dependencies,
  );
  assert.deepEqual(JSON.parse(logs.pop() ?? ''), {
    profileToken: 'main',
    profileName: 'Main',
    uri: 'rtsp://camera/live',
  });
});

test('invokes capabilities exactly once and logs the native report as one JSON line', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  const report = {
    device: { manufacturer: 'Example Camera Vendor', model: 'Model A' },
    scopes: ['onvif://www.onvif.org/Profile/Streaming'],
    declaredProfiles: ['S'],
    serviceDiscovery: 'getServices',
    services: [],
    profiles: [],
    ptz: {
      detected: null,
      panTiltSupported: null,
      zoomSupported: null,
      profileTokens: [],
      nodes: [],
    },
    events: { detected: false, topics: [] },
    media2: { detected: true, encodings: ['H265'], h265Supported: true },
    warnings: [],
  };
  let calls = 0;
  dependencies.getCameraCapabilities = async (options) => {
    calls++;
    assert.deepEqual(options, {
      host: 'camera.local',
      user: 'operator',
      pass: 'command-only-secret',
      deviceUrls: [
        'http://camera.local/onvif/device_service',
        'http://camera.local:8000/onvif/device_service',
      ],
      timeoutMs: 2_500,
    });
    return report;
  };

  await commandMain()(
    [
      'capabilities',
      '--host', 'camera.local',
      '--user', 'operator',
      '--pass', 'command-only-secret',
      '--device-url', 'http://camera.local/onvif/device_service',
      '--device-url', 'http://camera.local:8000/onvif/device_service',
      '--timeout-ms', '2500',
    ],
    dependencies,
  );

  assert.equal(calls, 1);
  assert.deepEqual(logs, [JSON.stringify(report)]);
  assert.doesNotMatch(logs[0] ?? '', /command-only-secret/);
});

test('applies capability credential defaults and omits absent optional client settings', async () => {
  const previous = process.env.ONVIF_PASSWORD;
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  const calls: unknown[] = [];
  dependencies.getCameraCapabilities = async (options) => {
    calls.push(options);
    return {
      device: {}, scopes: [], declaredProfiles: [], serviceDiscovery: 'unavailable',
      services: [], profiles: [],
      ptz: {
        detected: null, panTiltSupported: null, zoomSupported: null,
        profileTokens: [], nodes: [],
      },
      events: { detected: null, topics: [] },
      media2: { detected: null, encodings: [], h265Supported: null },
      warnings: [],
    };
  };

  try {
    process.env.ONVIF_PASSWORD = 'environment-only-secret';
    await commandMain()(['capabilities', '--host', 'camera.local'], dependencies);
    await commandMain()(
      ['capabilities', '--host', 'camera.local', '--pass', 'explicit-secret'],
      dependencies,
    );
    await commandMain()(
      ['capabilities', '--host', 'camera.local', '--pass', ''],
      dependencies,
    );
    delete process.env.ONVIF_PASSWORD;
    await commandMain()(['capabilities', '--host', 'camera.local'], dependencies);
  } finally {
    if (previous === undefined) delete process.env.ONVIF_PASSWORD;
    else process.env.ONVIF_PASSWORD = previous;
  }

  assert.deepEqual(calls, [
    { host: 'camera.local', user: '', pass: 'environment-only-secret' },
    { host: 'camera.local', user: '', pass: 'explicit-secret' },
    { host: 'camera.local', user: '', pass: '' },
    { host: 'camera.local', user: '', pass: '' },
  ]);
  assert.equal(logs.length, 4);
  assert.ok(logs.every((line) => !/environment-only-secret|explicit-secret/.test(line)));
});

test('rejects missing or flag-shaped values for every capability option', async () => {
  const previous = process.env.ONVIF_PASSWORD;
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  let calls = 0;
  dependencies.getCameraCapabilities = async () => {
    calls++;
    return {};
  };
  const cases: Array<{ option: string; argv: string[] }> = [
    {
      option: 'pass',
      argv: ['capabilities', '--host', 'camera.local', '--pass'],
    },
    {
      option: 'pass',
      argv: ['capabilities', '--host', 'camera.local', '--pass', '--timeout-ms', '50'],
    },
    {
      option: 'pass',
      argv: ['capabilities', '--host', 'camera.local', '--pass', '-h'],
    },
    {
      option: 'device-url',
      argv: ['capabilities', '--host', 'camera.local', '--device-url'],
    },
    {
      option: 'device-url',
      argv: ['capabilities', '--host', 'camera.local', '--device-url', ''],
    },
    {
      option: 'device-url',
      argv: [
        'capabilities', '--host', 'camera.local',
        '--device-url', 'http://device-one/onvif/device_service',
        '--device-url',
      ],
    },
    {
      option: 'device-url',
      argv: [
        'capabilities', '--host', 'camera.local',
        '--device-url', '--timeout-ms', '50',
      ],
    },
    {
      option: 'device-url',
      argv: ['capabilities', '--host', 'camera.local', '--device-url', '-h'],
    },
    {
      option: 'host',
      argv: ['capabilities', '--host'],
    },
    {
      option: 'host',
      argv: ['capabilities', '--host', ''],
    },
    {
      option: 'host',
      argv: ['capabilities', '--host', '--timeout-ms', '50'],
    },
    {
      option: 'host',
      argv: ['capabilities', '--host', '-h'],
    },
    {
      option: 'user',
      argv: ['capabilities', '--host', 'camera.local', '--user'],
    },
    {
      option: 'user',
      argv: ['capabilities', '--host', 'camera.local', '--user', ''],
    },
    {
      option: 'user',
      argv: ['capabilities', '--host', 'camera.local', '--user', '--timeout-ms', '50'],
    },
    {
      option: 'user',
      argv: ['capabilities', '--host', 'camera.local', '--user', '-h'],
    },
    {
      option: 'timeout-ms',
      argv: ['capabilities', '--host', 'camera.local', '--timeout-ms'],
    },
    {
      option: 'timeout-ms',
      argv: ['capabilities', '--host', 'camera.local', '--timeout-ms', ''],
    },
    {
      option: 'timeout-ms',
      argv: ['capabilities', '--host', 'camera.local', '--timeout-ms', '--user', 'operator'],
    },
    {
      option: 'timeout-ms',
      argv: ['capabilities', '--host', 'camera.local', '--timeout-ms', '-h'],
    },
  ];

  try {
    process.env.ONVIF_PASSWORD = 'strict-environment-secret';
    const outcomes: string[] = [];
    for (const { argv } of cases) {
      try {
        await commandMain()(argv, dependencies);
        outcomes.push('resolved');
      } catch (error) {
        outcomes.push(error instanceof Error ? error.message : String(error));
      }
    }

    assert.deepEqual(
      outcomes,
      cases.map(({ option }) => `missing value for --${option}`),
    );
    assert.doesNotMatch(
      JSON.stringify(outcomes),
      /strict-environment-secret|camera\.local|device-one/,
    );
  } finally {
    if (previous === undefined) delete process.env.ONVIF_PASSWORD;
    else process.env.ONVIF_PASSWORD = previous;
  }

  assert.equal(calls, 0);
  assert.deepEqual(logs, []);
});

test('rejects unknown capability options and positionals before dispatch without reflection', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  let calls = 0;
  dependencies.getCameraCapabilities = async () => {
    calls++;
    return {};
  };
  const cases = [
    ['--unknown=attached-control-secret'],
    ['--timeout-mss', 'misspelled-control-secret'],
    ['positional-control-secret'],
  ];

  for (const unknownArguments of cases) {
    await assert.rejects(
      commandMain()(
        [
          'capabilities', '--host', 'camera.local', '--pass', 'password-control-secret',
          ...unknownArguments,
        ],
        dependencies,
      ),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, 'unknown capabilities argument');
        assert.doesNotMatch(
          error.message,
          /attached-control-secret|misspelled-control-secret|positional-control-secret|password-control-secret|camera\.local/,
        );
        return true;
      },
    );
  }

  assert.equal(calls, 0);
  assert.deepEqual(logs, []);
});

test('rejects missing capability hosts and non-positive or non-finite timeouts safely', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  let calls = 0;
  dependencies.getCameraCapabilities = async () => {
    calls++;
    throw new Error('capabilities should not run');
  };

  await assert.rejects(
    commandMain()(
      ['capabilities', '--pass', 'validation-only-secret'],
      dependencies,
    ),
    (error: unknown) => {
      assert.match(String(error), /missing --host/);
      assert.doesNotMatch(String(error), /validation-only-secret/);
      return true;
    },
  );
  for (const timeout of ['0', '-1', 'NaN', 'Infinity']) {
    await assert.rejects(
      commandMain()(
        [
          'capabilities', '--host', 'camera.local',
          '--pass', 'validation-only-secret', '--timeout-ms', timeout,
        ],
        dependencies,
      ),
      (error: unknown) => {
        assert.match(String(error), /timeout-ms must be finite and greater than 0/);
        assert.doesNotMatch(String(error), /validation-only-secret/);
        return true;
      },
    );
  }

  assert.equal(calls, 0);
  assert.deepEqual(logs, []);
});

test('enforces an inclusive 24-hour capability timeout for separate and attached forms', async () => {
  assert.equal(Number('86400000.000000001'), 86_400_000);
  assert.ok(Number('86400000.00000001') > 86_400_000);
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  const calls: unknown[] = [];
  dependencies.getCameraCapabilities = async (options) => {
    calls.push(options);
    return {};
  };

  for (const timeoutArguments of [
    ['--timeout-ms', '86400000'],
    ['--timeout-ms=86400000'],
    ['--timeout-ms', '86400000.000000001'],
    ['--timeout-ms=86400000.000000001'],
  ]) {
    await commandMain()(
      ['capabilities', '--host', 'camera.local', ...timeoutArguments],
      dependencies,
    );
  }

  assert.deepEqual(calls, Array.from({ length: 4 }, () => ({
    host: 'camera.local', user: '', pass: '', timeoutMs: 86_400_000,
  })));
});

test('rejects fractional and huge capability timeouts before dispatch without reflecting inputs', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  let calls = 0;
  dependencies.getCameraCapabilities = async () => {
    calls++;
    throw new Error('capabilities should not run');
  };
  const cases = [
    ['--timeout-ms', '86400000.00000001'],
    ['--timeout-ms=86400000.00000001'],
    ['--timeout-ms', '86400000.00000049'],
    ['--timeout-ms=86400000.00000049'],
    ['--timeout-ms', '86400001'],
    ['--timeout-ms=86400001'],
    ['--timeout-ms', '1e22'],
    ['--timeout-ms=1e22'],
  ];

  for (const timeoutArguments of cases) {
    const timeoutMarker = timeoutArguments.at(-1)!.replace('--timeout-ms=', '');
    await assert.rejects(
      commandMain()(
        [
          'capabilities', '--host', 'camera.local',
          '--pass', 'huge-password-secret', ...timeoutArguments,
        ],
        dependencies,
      ),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, 'timeout-ms exceeds the 24-hour maximum');
        assert.doesNotMatch(error.message, new RegExp(timeoutMarker));
        assert.doesNotMatch(error.message, /huge-password-secret/);
        return true;
      },
    );
  }

  assert.equal(calls, 0);
  assert.deepEqual(logs, []);
});

test('rejects a bare capability terminator as control without exposing nearby secrets', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  let calls = 0;
  dependencies.getCameraCapabilities = async () => {
    calls++;
    return {};
  };
  const cases = [
    {
      argv: [
        'capabilities', '--host', 'camera.local', '--pass', 'control-password-secret',
        '--', '--pass=trailing-attached-secret',
      ],
      message: 'capabilities does not accept an argument terminator',
    },
    {
      argv: [
        'capabilities', '--host', 'camera.local', '--pass', '--',
        '--pass=trailing-attached-secret',
      ],
      message: 'missing value for --pass',
    },
  ];

  for (const { argv, message } of cases) {
    await assert.rejects(
      commandMain()(argv, dependencies),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, message);
        assert.doesNotMatch(
          error.message,
          /control-password-secret|trailing-attached-secret|camera\.local/,
        );
        return true;
      },
    );
  }

  assert.equal(calls, 0);
  assert.deepEqual(logs, []);
});

test('keeps safe separate and attached hyphen-leading capability passwords opaque', async () => {
  const logs: string[] = [];
  const dependencies = commandDependencies(logs);
  const calls: unknown[] = [];
  dependencies.getCameraCapabilities = async (options) => {
    calls.push(options);
    return {};
  };

  for (const passwordArguments of [
    ['--pass', '--separate-password-secret'],
    ['--pass=--attached-password-secret'],
    ['--pass=--'],
    ['--pass='],
  ]) {
    await commandMain()(
      ['capabilities', '--host', 'camera.local', ...passwordArguments],
      dependencies,
    );
  }

  assert.deepEqual(calls, [
    { host: 'camera.local', user: '', pass: '--separate-password-secret' },
    { host: 'camera.local', user: '', pass: '--attached-password-secret' },
    { host: 'camera.local', user: '', pass: '--' },
    { host: 'camera.local', user: '', pass: '' },
  ]);
  assert.ok(logs.every((line) => !/password-secret/.test(line)));
});
