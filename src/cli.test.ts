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
