/**
 * List the camera's RTSP stream URIs (one per ONVIF media profile).
 *
 *   ONVIF_PASSWORD='<password>' npm run streams -- --host camera.local
 */
import { getStreamUris } from './onvif/streams.ts';

function arg(name: string, def?: string): string {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

async function main(): Promise<void> {
  const host = arg('host');
  const user = arg('user', 'admin');
  const pass = arg('pass', process.env.ONVIF_PASSWORD);

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
