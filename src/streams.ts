/**
 * List the camera's RTSP stream URIs (one per ONVIF media profile).
 *
 *   ONVIF_PASSWORD='<password>' npm run streams -- --host camera.local
 */
import { getStreamUris } from './onvif/streams.ts';
import { displayRtspTarget, redactRtspCredentials } from './backchannel.ts';

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
    console.log(`  ${displayRtspTarget(stream.uri)}\n`);
  }
}

main().catch((err) => {
  const message = err instanceof Error ? err.message : String(err);
  console.error('streams error:', redactRtspCredentials(message));
  process.exitCode = 1;
});
