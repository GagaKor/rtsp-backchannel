#!/usr/bin/env node

/**
 * M4 — play an audio file out the camera speaker via the ONVIF backchannel.
 *
 *   onvif-backchannel --host 10.128.10.141 --user admin --pass CHANGEME \
 *     --file announce.wav --volume 0.05
 */
import { openBackchannel, SAMPLE_RATE } from './backchannel.ts';
import { fileToG711 } from './audio/transcode.ts';
import { pathToFileURL } from 'node:url';
import { discoverDevices } from './onvif/discovery.ts';
import { getStreamUris } from './onvif/streams.ts';

const HELP = `Usage: onvif-backchannel --host <camera> --user <user> --pass <password> --file <audio>
  onvif-backchannel discover [--timeout-ms <ms>] [--interface <IPv4> ...]
  onvif-backchannel streams --host <camera> [--user <user>] [--pass <password>]

Options:
  --file <path>       audio file to play once
  --host <address>    camera address
  --user <name>       ONVIF/RTSP user
  --pass <password>   ONVIF/RTSP password (default: ONVIF_PASSWORD or CHANGEME)
  --volume <0..1>     linear volume (default: 0.05)

Playback profile: PCMA 8 kHz mono, TCP interleaved RTP, 40 ms packets.
`;

function arg(argv: string[], name: string, def?: string): string {
  const i = argv.indexOf(`--${name}`);
  if (i >= 0 && argv[i + 1]) return argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

export interface PlaybackOptions {
  host: string;
  user: string;
  pass: string;
  file: string;
  volume: number;
}

export interface PlaybackDependencies {
  openBackchannel: typeof openBackchannel;
  fileToG711: typeof fileToG711;
  log(message: string): void;
}

export interface CommandDependencies extends PlaybackDependencies {
  discoverDevices: typeof discoverDevices;
  getStreamUris: typeof getStreamUris;
}

const playbackDependencies: PlaybackDependencies = {
  openBackchannel,
  fileToG711,
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
  return {
    host: arg(argv, 'host', '172.168.46.56'),
    user: arg(argv, 'user', 'admin'),
    pass: arg(argv, 'pass', process.env.ONVIF_PASSWORD ?? 'CHANGEME'),
    file: arg(argv, 'file'),
    volume,
  };
}

export async function playFile(
  options: PlaybackOptions,
  dependencies: PlaybackDependencies = playbackDependencies,
): Promise<number> {
  const { host, user, pass, file, volume } = options;
  dependencies.log(`# play "${file}" -> ${host} speaker (backchannel)`);
  const session = await dependencies.openBackchannel(host, user, pass);
  let sent = 0;
  let playbackFailed = false;
  let playbackError: unknown;
  try {
    dependencies.log(
      `backchannel open: ${session.variant}/${SAMPLE_RATE} ` +
        `pt=${session.payloadType} ch=${session.rtpChannel}`,
    );
    const g711 = await dependencies.fileToG711(file, session.variant, volume);
    const seconds = (g711.length / SAMPLE_RATE).toFixed(1);
    dependencies.log(
      `transcoded: ${g711.length} bytes (~${seconds}s ${session.variant} 8kHz mono)`,
    );
    sent = await session.send(g711);
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
    const devices = await dependencies.discoverDevices({
      timeoutMs,
      ...(interfaces.length > 0 ? { interfaces } : {}),
    });
    for (const device of devices) dependencies.log(JSON.stringify(device));
    return;
  }
  if (argv[0] === 'streams') {
    const commandArgs = argv.slice(1);
    const deviceUrls = args(commandArgs, 'device-url');
    const streams = await dependencies.getStreamUris({
      host: arg(commandArgs, 'host', '172.168.46.56'),
      user: arg(commandArgs, 'user', 'admin'),
      pass: arg(commandArgs, 'pass', process.env.ONVIF_PASSWORD ?? 'CHANGEME'),
      ...(deviceUrls.length > 0 ? { deviceUrls } : {}),
    });
    for (const stream of streams) dependencies.log(JSON.stringify(stream));
    return;
  }
  const playbackArgs = argv[0] === 'play' ? argv.slice(1) : argv;
  await playFile(parseCliArgs(playbackArgs), dependencies);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error('play error:', err.message ?? err);
    process.exitCode = 1;
  });
}
