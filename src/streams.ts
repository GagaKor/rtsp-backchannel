/**
 * List the camera's RTSP stream URIs (one per ONVIF media profile).
 *
 *   npm run streams -- --host 172.168.46.56 --user admin --pass CHANGEME
 */
import { OnvifDevice } from './onvif/deviceClient.ts';

function arg(name: string, def: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

async function main(): Promise<void> {
  const host = arg('host', '172.168.46.56');
  const user = arg('user', 'admin');
  const pass = arg('pass', 'CHANGEME');

  const dev = new OnvifDevice(host, user, pass);
  const info = await dev.connect();
  console.log(`# ${info.manufacturer ?? '?'} ${info.model ?? '?'} @ ${host}\n`);

  const profiles = await dev.getProfiles();
  for (const p of profiles) {
    const uri = await dev.getStreamUri(p.token);
    const withCreds = uri.replace('rtsp://', `rtsp://${user}:${pass}@`);
    console.log(`[${p.token}]`);
    console.log(`  ${uri}`);
    console.log(`  (인증포함) ${withCreds}\n`);
  }
}

main().catch((err) => {
  console.error('streams error:', err.message ?? err);
  process.exitCode = 1;
});
