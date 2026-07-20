import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { chmod, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { test } from 'node:test';
import { fileToG711 } from './transcode.ts';
import * as transcode from './transcode.ts';

interface TestCodec {
  name: 'pcma' | 'pcmu' | 'g726-16' | 'g726-24' | 'g726-32' | 'g726-40' | 'aac';
  payloadType: number;
  encoding: string;
  clockRate: number;
  channels?: number;
  fmtp?: {
    payloadType: number;
    parameters: Record<string, string>;
  };
}

interface EncodedAudio {
  codec: string;
  clockRate: number;
  frames: Array<{ payload: Buffer; samples: number }>;
  byteLength: number;
  sampleCount: number;
}

type FileToRtpAudio = (
  file: string,
  codec: TestCodec,
  volume?: number,
) => Promise<EncodedAudio>;

type ParsedAdtsFrame = {
  payload: Buffer;
  samples: number;
  sampleRate: number;
  channels: number;
};

function fileEncoder(): FileToRtpAudio {
  const candidate = (transcode as unknown as { fileToRtpAudio?: FileToRtpAudio })
    .fileToRtpAudio;
  assert.ok(candidate);
  return candidate;
}

function adtsParser(): (input: Buffer) => ParsedAdtsFrame[] {
  const candidate = (transcode as unknown as {
    parseAdtsFrames?: (input: Buffer) => ParsedAdtsFrame[];
  }).parseAdtsFrames;
  assert.ok(candidate);
  return candidate;
}

function rfc3640Packetizer(): (accessUnit: Buffer) => Buffer {
  const candidate = (transcode as unknown as {
    aacRfc3640Payload?: (accessUnit: Buffer) => Buffer;
  }).aacRfc3640Payload;
  assert.ok(candidate);
  return candidate;
}

class FakeFfmpeg extends EventEmitter {
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly killSignals: string[] = [];

  kill(signal?: string): boolean {
    this.killSignals.push(signal ?? 'SIGTERM');
    return true;
  }
}

interface TestFfmpegRuntime {
  spawn(command: string, args: string[]): FakeFfmpeg;
  setTimeout(callback: () => void, milliseconds: number): object;
  clearTimeout(timer: object): void;
}

type RunFfmpeg = (
  args: string[],
  label?: string,
  runtime?: TestFfmpegRuntime,
) => Promise<Buffer>;

function ffmpegRunner(): RunFfmpeg {
  const candidate = (transcode as unknown as { runFfmpeg?: RunFfmpeg }).runFfmpeg;
  assert.ok(candidate);
  return candidate;
}

function adtsFrame(
  payload: Buffer,
  options: { crc?: boolean; rawDataBlocks?: number } = {},
): Buffer {
  const protectionAbsent = options.crc ? 0 : 1;
  const headerLength = options.crc ? 9 : 7;
  const frameLength = headerLength + payload.length;
  const header = Buffer.alloc(headerLength);
  header[0] = 0xff;
  header[1] = 0xf0 | protectionAbsent;
  header[2] = (1 << 6) | (11 << 2); // AAC-LC, 8 kHz, mono channel config high bit 0
  header[3] = (1 << 6) | ((frameLength >> 11) & 0x03);
  header[4] = (frameLength >> 3) & 0xff;
  header[5] = ((frameLength & 0x07) << 5) | 0x1f;
  header[6] = 0xfc | (options.rawDataBlocks ?? 0);
  return Buffer.concat([header, payload]);
}

async function withFakeFfmpeg<T>(
  output: Buffer,
  run: () => Promise<T>,
): Promise<{ result: T; args: string[] }> {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'onvif-ffmpeg-codec-'));
  const fakeFfmpeg = path.join(directory, 'ffmpeg');
  const argsFile = path.join(directory, 'args.json');
  await writeFile(
    fakeFfmpeg,
    `#!${process.execPath}\n` +
      "require('node:fs').writeFileSync(process.env.FFMPEG_ARGS_FILE, JSON.stringify(process.argv.slice(2)));\n" +
      `process.stdout.write(Buffer.from('${output.toString('base64')}', 'base64'));\n`,
  );
  await chmod(fakeFfmpeg, 0o755);

  const previousPath = process.env.PATH;
  const previousArgsFile = process.env.FFMPEG_ARGS_FILE;
  process.env.PATH = `${directory}${path.delimiter}${previousPath ?? ''}`;
  process.env.FFMPEG_ARGS_FILE = argsFile;
  try {
    const result = await run();
    const args = JSON.parse(await readFile(argsFile, 'utf8')) as string[];
    return { result, args };
  } finally {
    process.env.PATH = previousPath;
    if (previousArgsFile === undefined) delete process.env.FFMPEG_ARGS_FILE;
    else process.env.FFMPEG_ARGS_FILE = previousArgsFile;
    await rm(directory, { recursive: true, force: true });
  }
}

test('kills FFmpeg at the absolute 120 second deadline and settles once', async () => {
  assert.equal(
    (transcode as unknown as { FFMPEG_TIMEOUT_MS?: number }).FFMPEG_TIMEOUT_MS,
    120_000,
  );
  const child = new FakeFfmpeg();
  const timer = {};
  let timeout: (() => void) | undefined;
  let timeoutMs = 0;
  let clearCalls = 0;
  const runtime: TestFfmpegRuntime = {
    spawn: () => child,
    setTimeout: (callback, milliseconds) => {
      timeout = callback;
      timeoutMs = milliseconds;
      return timer;
    },
    clearTimeout: (value) => {
      assert.equal(value, timer);
      clearCalls++;
    },
  };

  const result = ffmpegRunner()([], 'ffmpeg test', runtime);
  const rejection = assert.rejects(result, /ffmpeg test timed out after 120000 ms/);
  assert.equal(timeoutMs, 120_000);
  assert.ok(timeout);
  timeout();
  child.emit('close', 0);
  await rejection;

  assert.deepEqual(child.killSignals, ['SIGKILL']);
  assert.equal(clearCalls, 1);
});

test('clears the FFmpeg deadline after successful completion', async () => {
  const child = new FakeFfmpeg();
  const timer = {};
  let timeout: (() => void) | undefined;
  let clearCalls = 0;
  const runtime: TestFfmpegRuntime = {
    spawn: () => child,
    setTimeout: (callback) => {
      timeout = callback;
      return timer;
    },
    clearTimeout: () => {
      clearCalls++;
    },
  };

  const result = ffmpegRunner()([], 'ffmpeg test', runtime);
  child.stdout.write(Buffer.from('done'));
  child.emit('close', 0);
  assert.deepEqual(await result, Buffer.from('done'));
  assert.equal(clearCalls, 1);

  assert.ok(timeout);
  timeout();
  assert.deepEqual(child.killSignals, []);
  assert.equal(clearCalls, 1);
});

test('decodes to S16LE before applying the TypeScript PCMA encoder', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'onvif-ffmpeg-'));
  const fakeFfmpeg = path.join(directory, 'ffmpeg');
  const pcm = Buffer.alloc(18);
  [-32768, -30000, -1000, -1, 0, 1, 1000, 30000, 32767].forEach((sample, index) => {
    pcm.writeInt16LE(sample, index * 2);
  });
  await writeFile(
    fakeFfmpeg,
    `#!${process.execPath}\nprocess.stdout.write(Buffer.from('${pcm.toString('hex')}', 'hex'));\n`,
  );
  await chmod(fakeFfmpeg, 0o755);

  const previousPath = process.env.PATH;
  process.env.PATH = `${directory}${path.delimiter}${previousPath ?? ''}`;
  try {
    const encoded = await fileToG711('source.wav', 'PCMA', 0.05);
    assert.deepEqual([...encoded], [108, 98, 86, 85, 213, 213, 214, 226, 236]);
  } finally {
    process.env.PATH = previousPath;
    await rm(directory, { recursive: true, force: true });
  }
});

test('parses ADTS boundaries and CRC headers but rejects multiple raw data blocks', () => {
  const first = Buffer.from([0x11, 0x22, 0x33]);
  const second = Buffer.from([0xaa, 0xbb]);
  const parsed = adtsParser()(Buffer.concat([
    adtsFrame(first),
    adtsFrame(second, { crc: true }),
  ]));

  assert.deepEqual(parsed, [
    { payload: first, samples: 1024, sampleRate: 8000, channels: 1 },
    { payload: second, samples: 1024, sampleRate: 8000, channels: 1 },
  ]);
  assert.throws(
    () => adtsParser()(adtsFrame(first, { rawDataBlocks: 1 })),
    /ADTS raw_data_blocks_in_frame must be 0 at byte 0/,
  );
  assert.throws(
    () => adtsParser()(adtsFrame(first).subarray(0, -1)),
    /truncated ADTS frame at byte 0/,
  );
  assert.throws(
    () => adtsParser()(Buffer.from([0x00, 0x01, 0x02])),
    /invalid ADTS frame at byte 0/,
  );
});

test('builds one RFC 3640 AAC-hbr AU header for each access unit', () => {
  assert.deepEqual(
    rfc3640Packetizer()(Buffer.from([0x11, 0x22, 0x33, 0x44])),
    Buffer.from([0x00, 0x10, 0x00, 0x20, 0x11, 0x22, 0x33, 0x44]),
  );
  assert.throws(
    () => rfc3640Packetizer()(Buffer.alloc(0x2000)),
    /AAC access unit too large: 8192 bytes/,
  );
});

test('wraps G.711 FFmpeg decode output as 40 ms timestamped frames', async () => {
  const pcm = Buffer.alloc(640 * 2);
  const codec: TestCodec = {
    name: 'pcma',
    payloadType: 8,
    encoding: 'PCMA',
    clockRate: 8000,
  };
  const { result } = await withFakeFfmpeg(
    pcm,
    () => fileEncoder()('announcement.wav', codec, 0.05),
  );

  assert.equal(result.codec, 'pcma');
  assert.equal(result.byteLength, 640);
  assert.equal(result.sampleCount, 640);
  assert.deepEqual(result.frames.map((frame) => [frame.payload.length, frame.samples]), [
    [320, 320],
    [320, 320],
  ]);
  assert.ok(result.frames.every((frame) => frame.payload.equals(Buffer.alloc(320, 0xd5))));
});

test('encodes every G.726 bitrate with FFmpeg into exact 40 ms frames', async () => {
  const cases = [
    ['g726-16', 16, 80],
    ['g726-24', 24, 120],
    ['g726-32', 32, 160],
    ['g726-40', 40, 200],
  ] as const;

  for (const [name, bitrate, bytesPerFrame] of cases) {
    const output = Buffer.alloc(bytesPerFrame * 2, 0x5a);
    const codec: TestCodec = {
      name,
      payloadType: 96,
      encoding: name.toUpperCase(),
      clockRate: 8000,
    };
    const { result, args } = await withFakeFfmpeg(
      output,
      () => fileEncoder()('announcement.wav', codec, 0.05),
    );

    assert.equal(result.codec, name);
    assert.equal(result.clockRate, 8000);
    assert.equal(result.byteLength, bytesPerFrame * 2);
    assert.equal(result.sampleCount, 640);
    assert.deepEqual(result.frames.map((frame) => [frame.payload.length, frame.samples]), [
      [bytesPerFrame, 320],
      [bytesPerFrame, 320],
    ]);
    assert.equal(args[args.indexOf('-c:a') + 1], 'g726le');
    assert.equal(args[args.indexOf('-f') + 1], 'g726le');
    assert.ok(args.includes(`${bitrate}k`));
    assert.ok(args.includes('8000'));
    assert.ok(args.includes('volume=0.05'));
  }
});

const ffmpegAvailable = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).status === 0;

test(
  'produces G.726-32 payloads that decode as RFC 3551 little-endian packing',
  { skip: !ffmpegAvailable },
  async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), 'onvif-g726le-roundtrip-'));
    const inputPath = path.join(directory, 'square.wav');
    const sampleCount = 640;
    const pcm = Buffer.alloc(sampleCount * 2);
    for (let index = 0; index < sampleCount; index++) {
      pcm.writeInt16LE(index % 16 < 8 ? 12_000 : -12_000, index * 2);
    }
    const wav = Buffer.alloc(44 + pcm.length);
    wav.write('RIFF', 0, 'ascii');
    wav.writeUInt32LE(36 + pcm.length, 4);
    wav.write('WAVE', 8, 'ascii');
    wav.write('fmt ', 12, 'ascii');
    wav.writeUInt32LE(16, 16);
    wav.writeUInt16LE(1, 20);
    wav.writeUInt16LE(1, 22);
    wav.writeUInt32LE(8000, 24);
    wav.writeUInt32LE(16_000, 28);
    wav.writeUInt16LE(2, 32);
    wav.writeUInt16LE(16, 34);
    wav.write('data', 36, 'ascii');
    wav.writeUInt32LE(pcm.length, 40);
    pcm.copy(wav, 44);
    await writeFile(inputPath, wav);

    try {
      const encoded = await fileEncoder()(inputPath, {
        name: 'g726-32',
        payloadType: 97,
        encoding: 'G726-32',
        clockRate: 8000,
      }, 1);
      const decoded = spawnSync(
        'ffmpeg',
        [
          '-nostdin',
          '-hide_banner',
          '-loglevel', 'error',
          '-f', 'g726le',
          '-code_size', '4',
          '-sample_rate', '8000',
          '-i', 'pipe:0',
          '-f', 's16le',
          '-c:a', 'pcm_s16le',
          'pipe:1',
        ],
        { input: Buffer.concat(encoded.frames.map((frame) => frame.payload)) },
      );
      assert.equal(decoded.status, 0, decoded.stderr.toString());
      assert.equal(decoded.stdout.length, pcm.length);
      let matchingSigns = 0;
      let compared = 0;
      for (let index = 80; index < sampleCount; index++) {
        const expected = pcm.readInt16LE(index * 2);
        const actual = decoded.stdout.readInt16LE(index * 2);
        if (Math.sign(actual) === Math.sign(expected)) matchingSigns++;
        compared++;
      }
      assert.ok(matchingSigns / compared > 0.9);
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  },
);

test('encodes AAC as ADTS, removes ADTS headers, and adds RFC 3640 AU headers', async () => {
  const first = Buffer.from([0xde, 0xad, 0xbe, 0xef]);
  const second = Buffer.from([0xca, 0xfe]);
  const codec: TestCodec = {
    name: 'aac',
    payloadType: 110,
    encoding: 'MPEG4-GENERIC',
    clockRate: 8000,
    channels: 1,
    fmtp: {
      payloadType: 110,
      parameters: {
        mode: 'AAC-hbr',
        config: '1588',
        sizelength: '13',
        indexlength: '3',
        indexdeltalength: '3',
        constantduration: '1024',
      },
    },
  };
  const { result, args } = await withFakeFfmpeg(
    Buffer.concat([adtsFrame(first), adtsFrame(second)]),
    () => fileEncoder()('announcement.wav', codec, 0.25),
  );

  assert.equal(result.codec, 'aac');
  assert.equal(result.clockRate, 8000);
  assert.equal(result.byteLength, 14);
  assert.equal(result.sampleCount, 2048);
  assert.deepEqual(result.frames, [
    { payload: Buffer.from([0x00, 0x10, 0x00, 0x20, ...first]), samples: 1024 },
    { payload: Buffer.from([0x00, 0x10, 0x00, 0x10, ...second]), samples: 1024 },
  ]);
  assert.ok(args.includes('aac'));
  assert.ok(args.includes('adts'));
  assert.ok(args.includes('8000'));
  assert.ok(args.includes('volume=0.25'));
});
