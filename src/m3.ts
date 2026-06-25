/**
 * M2 + M3 — stream a G.711 test tone out the camera speaker via the
 * ONVIF backchannel.
 *
 *   npm run m3 -- --host 172.168.46.56 --user admin --pass CHANGEME --freq 1000 --ms 5000 --amp 0.9
 */
import { openBackchannel, SAMPLE_RATE } from './backchannel.ts';
import { generateTonePcm, pcm16ToG711 } from './audio/g711.ts';

function arg(name: string, def: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

async function main(): Promise<void> {
  const host = arg('host', '172.168.46.56');
  const user = arg('user', 'admin');
  const pass = arg('pass', 'CHANGEME');
  const freq = Number(arg('freq', '440'));
  const ms = Number(arg('ms', '2000'));
  const amp = Number(arg('amp', '0.5'));

  console.log(`# M2+M3 — backchannel tone @ ${host} (${freq}Hz, ${ms}ms)`);
  const session = await openBackchannel(host, user, pass);
  console.log(`✓ backchannel open: ${session.variant}/${SAMPLE_RATE} pt=${session.payloadType} ch=${session.rtpChannel}`);

  const pcm = generateTonePcm(freq, ms, SAMPLE_RATE, amp);
  const g711 = pcm16ToG711(pcm, session.variant);
  const sent = await session.send(g711);
  await session.close();
  console.log(`✓ sent ${sent} RTP frames — 스피커에서 톤이 들렸다면 M2+M3 PASS.`);
}

main().catch((err) => {
  console.error('M3 error:', err.message ?? err);
  process.exitCode = 1;
});
