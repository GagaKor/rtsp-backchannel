/**
 * M2 + M3 — stream a G.711 test tone out the camera speaker via the
 * ONVIF backchannel.
 *
 *   ONVIF_PASSWORD='<password>' npm run m3 -- --host camera.local --freq 1000 --ms 5000 --amp 0.9
 */
import {
  displayRtspTarget,
  openBackchannel,
  redactRtspCredentials,
  SAMPLE_RATE,
} from './backchannel.ts';
import { generateTonePcm, pcm16ToG711 } from './audio/g711.ts';
import { pathToFileURL } from 'node:url';

function arg(name: string, def?: string): string {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

export interface ToneOptions {
  host: string;
  user?: string;
  pass?: string;
  freq?: number;
  ms?: number;
  amp?: number;
}

export interface ToneDependencies {
  openBackchannel: typeof openBackchannel;
  generateTonePcm: typeof generateTonePcm;
  pcm16ToG711: typeof pcm16ToG711;
  log(message: string): void;
}

const toneDependencies: ToneDependencies = {
  openBackchannel,
  generateTonePcm,
  pcm16ToG711,
  log: (message) => console.log(message),
};

export async function runTone(
  options: ToneOptions,
  dependencies: ToneDependencies = toneDependencies,
): Promise<number> {
  const {
    host,
    user = 'admin',
    pass = '',
    freq = 440,
    ms = 2000,
    amp = 0.5,
  } = options;
  dependencies.log(
    `# M2+M3 — backchannel tone @ ${displayRtspTarget(host)} (${freq}Hz, ${ms}ms)`,
  );
  const session = await dependencies.openBackchannel(host, user, pass);
  try {
    const variant = session.variant;
    if (!variant) {
      throw new Error(`M3 tone requires G.711; negotiated ${session.codec.name}`);
    }
    dependencies.log(
      `✓ backchannel open: ${variant}/${SAMPLE_RATE} ` +
        `pt=${session.payloadType} ch=${session.rtpChannel}`,
    );
    const pcm = dependencies.generateTonePcm(freq, ms, SAMPLE_RATE, amp);
    const g711 = dependencies.pcm16ToG711(pcm, variant);
    const sent = await session.send(g711);
    dependencies.log(`✓ sent ${sent} RTP frames — 스피커에서 톤이 들렸다면 M2+M3 PASS.`);
    return sent;
  } finally {
    await session.close();
  }
}

async function main(): Promise<void> {
  const host = arg('host');
  const user = arg('user', 'admin');
  const pass = arg('pass', process.env.ONVIF_PASSWORD);
  const freq = Number(arg('freq', '440'));
  const ms = Number(arg('ms', '2000'));
  const amp = Number(arg('amp', '0.5'));

  await runTone({ host, user, pass, freq, ms, amp });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    const message = err instanceof Error ? err.message : String(err);
    console.error('M3 error:', redactRtspCredentials(message));
    process.exitCode = 1;
  });
}
