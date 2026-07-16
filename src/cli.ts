/**
 * M4 — play an audio file out the camera speaker via the ONVIF backchannel.
 *
 *   npm run play -- --host 10.128.10.141 --user admin --pass CHANGEME \
 *     --file announce.wav --volume 0.05
 */
import { openBackchannel, SAMPLE_RATE } from './backchannel.ts';
import { fileToG711 } from './audio/transcode.ts';
import { pathToFileURL } from 'node:url';

const HELP = `Usage: npm run play -- --host <camera> --user <user> --pass <password> --file <audio>

Options:
  --file <path>       audio file to play once
  --host <address>    camera address
  --user <name>       ONVIF/RTSP user
  --pass <password>   ONVIF/RTSP password
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

const playbackDependencies: PlaybackDependencies = {
  openBackchannel,
  fileToG711,
  log: (message) => console.log(message),
};

export function parseCliArgs(argv: string[]): PlaybackOptions {
  const volume = Number(arg(argv, 'volume', '0.05'));
  if (!Number.isFinite(volume) || volume < 0 || volume > 1) {
    throw new RangeError('volume must be finite and between 0 and 1');
  }
  return {
    host: arg(argv, 'host', '172.168.46.56'),
    user: arg(argv, 'user', 'admin'),
    pass: arg(argv, 'pass', 'CHANGEME'),
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
    const sent = await session.send(g711);
    dependencies.log(`sent ${sent} RTP packets`);
    return sent;
  } finally {
    await session.close();
  }
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  if (argv.includes('--help') || argv.includes('-h')) {
    process.stdout.write(HELP);
    return;
  }
  await playFile(parseCliArgs(argv));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error('play error:', err.message ?? err);
    process.exitCode = 1;
  });
}
