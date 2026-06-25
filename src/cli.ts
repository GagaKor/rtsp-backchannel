/**
 * M4 — play an audio file out the camera speaker via the ONVIF backchannel.
 *
 *   npm run play -- --host 172.168.46.56 --user admin --pass CHANGEME --file announce.wav
 */
import { openBackchannel, SAMPLE_RATE } from './backchannel.ts';
import { fileToG711 } from './audio/transcode.ts';

function arg(name: string, def?: string): string {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

async function main(): Promise<void> {
  const host = arg('host', '172.168.46.56');
  const user = arg('user', 'admin');
  const pass = arg('pass', 'CHANGEME');
  const file = arg('file');

  console.log(`# play "${file}" → ${host} speaker (backchannel)`);
  const session = await openBackchannel(host, user, pass);
  console.log(`✓ backchannel open: ${session.variant}/${SAMPLE_RATE} pt=${session.payloadType} ch=${session.rtpChannel}`);

  const g711 = await fileToG711(file, session.variant);
  const seconds = (g711.length / SAMPLE_RATE).toFixed(1);
  console.log(`  transcoded → ${g711.length} bytes (~${seconds}s ${session.variant} 8kHz mono)`);

  const sent = await session.send(g711);
  await session.close();
  console.log(`✓ sent ${sent} RTP frames — 카메라 스피커에서 재생됐다면 M4 PASS.`);
}

main().catch((err) => {
  console.error('play error:', err.message ?? err);
  process.exitCode = 1;
});
