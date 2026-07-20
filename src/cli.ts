#!/usr/bin/env node

/**
 * M4 — play an audio file out the camera speaker via the ONVIF backchannel.
 *
 *   rtsp-backchannel --host camera.local --user admin --pass '<password>' \
 *     --file announce.wav --volume 0.05
 */
import {
  displayRtspTarget,
  openBackchannel,
  redactRtspCredentials,
  SAMPLE_RATE,
  type BackchannelSession,
} from './backchannel.ts';
import { fileToG711, fileToRtpAudio } from './audio/transcode.ts';
import { pathToFileURL } from 'node:url';
import { discoverDevices } from './onvif/discovery.ts';
import { getStreamUris } from './onvif/streams.ts';
import type { CodecPreference } from './rtsp/sdp.ts';

const HELP = `Usage: rtsp-backchannel --host <camera> --user <user> --pass <password> --file <audio>
  rtsp-backchannel discover [--timeout-ms <ms>] [--interface <IPv4> ...]
                            [--cidr <IPv4/CIDR> ...] [--port <number> ...]
                            [--concurrency <1..256>]
  rtsp-backchannel streams --host <camera> [--user <user>] [--pass <password>]

Options:
  --file <path>       audio file to play once
  --host <address>    camera address
  --user <name>       ONVIF/RTSP user
  --pass <password>   ONVIF/RTSP password (or set ONVIF_PASSWORD)
  --codec <name>      auto|pcma|pcmu|g726-16|g726-24|g726-32|g726-40|aac
  --volume <0..1>     linear volume (default: 0.05)

Discovery options:
  --interface <IPv4>  local PC address for WS-Discovery (repeatable)
  --cidr <IPv4/CIDR>  target IP or CIDR for active discovery (repeatable)
  --port <number>     ONVIF Device Service port (repeatable)
  --concurrency <n>   concurrent CIDR hosts (default: 64)
  --timeout-ms <ms>   discovery timeout (default: 3000)

Playback profile: SDP codec negotiation, TCP interleaved RTP, real-time pacing.
`;

const CODEC_PREFERENCES: readonly CodecPreference[] = [
  'auto',
  'pcma',
  'pcmu',
  'g726-16',
  'g726-24',
  'g726-32',
  'g726-40',
  'aac',
];

function arg(argv: string[], name: string, def?: string): string {
  const i = argv.indexOf(`--${name}`);
  if (i >= 0 && argv[i + 1]) return argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

export interface PlaybackOptions {
  host: string;
  user?: string;
  pass?: string;
  file: string;
  volume?: number;
  codec?: CodecPreference;
}

type PlaybackBackchannelSession = Omit<BackchannelSession, 'withKeepAlive'> & {
  withKeepAlive?: BackchannelSession['withKeepAlive'];
};

export interface PlaybackDependencies {
  openBackchannel(
    ...args: Parameters<typeof openBackchannel>
  ): Promise<PlaybackBackchannelSession>;
  fileToG711: typeof fileToG711;
  fileToRtpAudio?: typeof fileToRtpAudio;
  log(message: string): void;
}

export interface CommandDependencies extends PlaybackDependencies {
  discoverDevices: typeof discoverDevices;
  getStreamUris: typeof getStreamUris;
}

const playbackDependencies: PlaybackDependencies = {
  openBackchannel,
  fileToG711,
  fileToRtpAudio,
  log: (message) => console.log(message),
};

const commandDependencies: CommandDependencies = {
  ...playbackDependencies,
  discoverDevices,
  getStreamUris,
};

function args(argv: string[], name: string): string[] {
  const values: string[] = [];
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === `--${name}` && argv[index + 1]) values.push(argv[++index]);
  }
  return values;
}

export function parseCliArgs(argv: string[]): PlaybackOptions {
  const volume = Number(arg(argv, 'volume', '0.05'));
  if (!Number.isFinite(volume) || volume < 0 || volume > 1) {
    throw new RangeError('volume must be finite and between 0 and 1');
  }
  const codecValue = arg(argv, 'codec', 'auto').toLowerCase();
  if (!CODEC_PREFERENCES.includes(codecValue as CodecPreference)) {
    throw new RangeError(`codec must be one of: ${CODEC_PREFERENCES.join(', ')}`);
  }
  return {
    host: arg(argv, 'host'),
    user: arg(argv, 'user', ''),
    pass: arg(argv, 'pass', process.env.ONVIF_PASSWORD ?? ''),
    file: arg(argv, 'file'),
    volume,
    codec: codecValue as CodecPreference,
  };
}

export async function playFile(
  options: PlaybackOptions,
  dependencies: PlaybackDependencies = playbackDependencies,
): Promise<number> {
  const {
    host,
    user = '',
    pass = '',
    file,
    volume = 0.05,
    codec = 'auto',
  } = options;
  dependencies.log(`# play "${file}" -> ${displayRtspTarget(host)} speaker (backchannel)`);
  const session = await dependencies.openBackchannel(host, user, pass, { codec });
  let sent = 0;
  let playbackFailed = false;
  let playbackError: unknown;
  try {
    if (session.codec) {
      dependencies.log(
        `backchannel open: ${session.codec.name}/${session.clockRate} ` +
          `pt=${session.payloadType} ch=${session.rtpChannel}`,
      );
      const encodeFile = dependencies.fileToRtpAudio ?? fileToRtpAudio;
      const encode = () => encodeFile(file, session.codec, volume);
      const encoded = session.withKeepAlive
        ? await session.withKeepAlive(encode)
        : await encode();
      const seconds = (encoded.sampleCount / encoded.clockRate).toFixed(1);
      dependencies.log(
        `transcoded: ${encoded.byteLength} bytes ` +
          `(~${seconds}s ${encoded.codec} ${encoded.clockRate}Hz)`,
      );
      sent = await session.send(encoded);
    } else {
      if (!session.variant) throw new Error('backchannel session did not select an audio codec');
      dependencies.log(
        `backchannel open: ${session.variant}/${SAMPLE_RATE} ` +
          `pt=${session.payloadType} ch=${session.rtpChannel}`,
      );
      const encode = () => dependencies.fileToG711(file, session.variant!, volume);
      const g711 = session.withKeepAlive
        ? await session.withKeepAlive(encode)
        : await encode();
      const seconds = (g711.length / SAMPLE_RATE).toFixed(1);
      dependencies.log(
        `transcoded: ${g711.length} bytes (~${seconds}s ${session.variant} 8kHz mono)`,
      );
      sent = await session.send(g711);
    }
    dependencies.log(`sent ${sent} RTP packets`);
  } catch (error) {
    playbackFailed = true;
    playbackError = error;
  }

  let cleanupFailed = false;
  let cleanupError: unknown;
  try {
    await session.close();
  } catch (error) {
    cleanupFailed = true;
    cleanupError = error;
  }

  if (playbackFailed && cleanupFailed) {
    throw new AggregateError(
      [playbackError, cleanupError],
      `${errorMessage(playbackError)}; RTSP cleanup also failed: ${errorMessage(cleanupError)}`,
      { cause: playbackError },
    );
  }
  if (playbackFailed) throw playbackError;
  if (cleanupFailed) throw cleanupError;
  return sent;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function main(
  argv = process.argv.slice(2),
  dependencies: CommandDependencies = commandDependencies,
): Promise<void> {
  if (argv.includes('--help') || argv.includes('-h')) {
    process.stdout.write(HELP);
    return;
  }
  if (argv[0] === 'discover') {
    const commandArgs = argv.slice(1);
    const timeoutMs = Number(arg(commandArgs, 'timeout-ms', '3000'));
    if (!Number.isFinite(timeoutMs) || timeoutMs < 0) {
      throw new RangeError('timeout-ms must be finite and 0 or greater');
    }
    const interfaces = args(commandArgs, 'interface');
    const cidrs = args(commandArgs, 'cidr');
    const ports = args(commandArgs, 'port').map(Number);
    if (ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65_535)) {
      throw new RangeError('port must be an integer between 1 and 65535');
    }
    const concurrencyValues = args(commandArgs, 'concurrency');
    const concurrency = concurrencyValues.length > 0 ? Number(concurrencyValues[0]) : undefined;
    if (
      concurrency !== undefined &&
      (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 256)
    ) {
      throw new RangeError('concurrency must be an integer between 1 and 256');
    }
    const devices = await dependencies.discoverDevices({
      timeoutMs,
      ...(interfaces.length > 0 ? { interfaces } : {}),
      ...(cidrs.length > 0 ? { cidrs } : {}),
      ...(ports.length > 0 ? { ports } : {}),
      ...(concurrency !== undefined ? { concurrency } : {}),
    });
    for (const device of devices) dependencies.log(JSON.stringify(device));
    return;
  }
  if (argv[0] === 'streams') {
    const commandArgs = argv.slice(1);
    const deviceUrls = args(commandArgs, 'device-url');
    const streams = await dependencies.getStreamUris({
      host: arg(commandArgs, 'host'),
      user: arg(commandArgs, 'user', ''),
      pass: arg(commandArgs, 'pass', process.env.ONVIF_PASSWORD ?? ''),
      ...(deviceUrls.length > 0 ? { deviceUrls } : {}),
    });
    for (const stream of streams) {
      dependencies.log(JSON.stringify({ ...stream, uri: displayRtspTarget(stream.uri) }));
    }
    return;
  }
  const playbackArgs = argv[0] === 'play' ? argv.slice(1) : argv;
  await playFile(parseCliArgs(playbackArgs), dependencies);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error('play error:', redactRtspCredentials(errorMessage(err)));
    process.exitCode = 1;
  });
}
