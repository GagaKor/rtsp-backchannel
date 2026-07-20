/**
 * List the camera's RTSP stream URIs (one per ONVIF media profile).
 *
 *   npm run streams -- --host 172.168.46.56 --user admin --pass CHANGEME
 */
import { getStreamUris } from './onvif/streams.ts';

function arg(name: string, def: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

async function main(): Promise<void> {
  const host = arg('host', '172.168.46.56');
  const user = arg('user', 'admin');
  const pass = arg('pass', process.env.ONVIF_PASSWORD ?? 'CHANGEME');

  const streams = await getStreamUris({ host, user, pass });
  for (const stream of streams) {
    console.log(`[${stream.profileToken}] ${stream.profileName ?? ''}`.trim());
    console.log(`  ${stream.uri}\n`);
  }
}

main().catch((err) => {
  console.error('streams error:', err.message ?? err);
  process.exitCode = 1;
});
