import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import * as cli from './cli.ts';

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
  assert.match(result.stdout, /40 ms/);
});

test('runs the dedicated npm binary entry point', () => {
  const result = spawnSync(
    process.execPath,
    ['--experimental-transform-types', 'src/bin.ts', '--help'],
    { encoding: 'utf8' },
  );

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Usage: onvif-backchannel/);
  assert.match(result.stdout, /--file/);
});

test('parses the validated 0.05 volume default and rejects invalid gain', () => {
  type Parsed = { volume: number };
  type Parser = (argv: string[]) => Parsed;
  const parse = (cli as unknown as { parseCliArgs?: Parser }).parseCliArgs;
  assert.ok(parse);

  assert.equal(parse(['--file', 'event.mp3']).volume, 0.05);
  for (const volume of ['nan', '-0.1', '1.1']) {
    assert.throws(
      () => parse(['--file', 'event.mp3', '--volume', volume]),
      /volume must be finite and between 0 and 1/,
    );
  }
});

test('uses ONVIF_PASSWORD when --pass is omitted', () => {
  type Parsed = { pass: string };
  type Parser = (argv: string[]) => Parsed;
  const parse = (cli as unknown as { parseCliArgs?: Parser }).parseCliArgs;
  assert.ok(parse);
  const previous = process.env.ONVIF_PASSWORD;
  process.env.ONVIF_PASSWORD = 'environment-secret';
  try {
    assert.equal(parse(['--file', 'event.mp3']).pass, 'environment-secret');
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
      interfaces: ['10.0.0.10', '192.168.0.20'],
    });
    return [{ ip: '10.128.10.141', xaddrs: [], scopes: [], name: 'Front Door' }];
  };

  await commandMain()(
    ['discover', '--timeout-ms', '1500', '--interface', '10.0.0.10', '--interface', '192.168.0.20'],
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
