/**
 * M1 — verify (in TypeScript) that the camera exposes an ONVIF RTSP audio
 * backchannel we can send to. Reimplements the earlier Python probe.
 *
 *   ONVIF_PASSWORD='<password>' node src/m1.ts --host camera.local
 */
import { OnvifDevice } from './onvif/deviceClient.ts';
import { RtspClient } from './rtsp/backchannelClient.ts';
import { parseSdp, findBackchannelAudio, pickSendCodec } from './rtsp/sdp.ts';

function arg(name: string, def?: string): string {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

function rtspParts(uri: string): { host: string; port: number; path: string } {
  const m = /^rtsp:\/\/(?:[^@/]+@)?([^:/]+)(?::(\d+))?(\/.*)?$/.exec(uri);
  if (!m) throw new Error(`bad RTSP uri: ${uri}`);
  return { host: m[1], port: Number(m[2] ?? 554), path: m[3] ?? '/' };
}

async function main(): Promise<void> {
  const host = arg('host');
  const user = arg('user', 'admin');
  const pass = arg('pass', process.env.ONVIF_PASSWORD);

  console.log(`# M1 — ONVIF backchannel probe @ ${host} (${user})`);

  const dev = new OnvifDevice(host, user, pass);
  const info = await dev.connect();
  console.log(`✓ ONVIF connected: ${info.manufacturer ?? '?'} ${info.model ?? '?'} (fw ${info.firmware ?? '?'})`);

  const profiles = await dev.getProfiles();
  if (profiles.length === 0) throw new Error('no media profiles');
  for (const p of profiles) {
    console.log(
      `  profile ${p.token}: audioEncoder=${p.hasAudioEncoder} ` +
        `audioOutput=${p.hasAudioOutput} audioSource=${p.hasAudioSource}`,
    );
  }

  const streamUri = await dev.getStreamUri(profiles[0].token);
  console.log(`  stream URI: ${streamUri}`);

  const { host: rHost, port, path } = rtspParts(streamUri);
  const baseUri = `rtsp://${rHost}:${port}${path}`;

  // Each DESCRIBE uses a fresh connection: this camera rejects a 2nd DESCRIBE
  // on a reused socket, and the real backchannel flow (M2) opens its own
  // connection with the backchannel DESCRIBE as the first request anyway.
  const describeOnce = async (backchannel: boolean) => {
    const rtsp = new RtspClient(rHost, port, user, pass);
    await rtsp.connect();
    try {
      if (!backchannel) {
        const opt = await rtsp.options(baseUri);
        console.log(`  OPTIONS -> ${opt.status} (Public: ${opt.headers['public'] ?? '-'})`);
      }
      return await rtsp.describe(baseUri, { backchannel });
    } finally {
      rtsp.close();
    }
  };

  const plain = await describeOnce(false);
  const plainSdp = parseSdp(plain.body);
  console.log(
    `  DESCRIBE (plain) -> ${plain.status}; tracks=` +
      plainSdp.media.map((m) => `${m.media}/${m.direction ?? '?'}`).join(', '),
  );

  const bc = await describeOnce(true);
  const bcSdp = parseSdp(bc.body);
  console.log(
    `  DESCRIBE (backchannel) -> ${bc.status}; tracks=` +
      bcSdp.media.map((m) => `${m.media}/${m.direction ?? '?'}`).join(', '),
  );

  const track = findBackchannelAudio(bcSdp);
  if (bc.status === 200 && track) {
    const send = pickSendCodec(track);
    console.log('\n✅ M1 PASS — backchannel audio track present:');
    console.log(`   track control=${track.control ?? '(none)'}  offered=${track.formats.join(',')}`);
    console.log(
      `   ▶ 송출 코덱 선택: PT=${send?.payloadType} ${send?.encoding}/${send?.clockRate}` +
        ` ${send?.encoding === 'PCMU' || send?.encoding === 'PCMA' ? '(G.711 — 단순/권장)' : ''}`,
    );
    console.log('   → 다음(M2): 이 트랙으로 SETUP + PLAY 세션 수립');
  } else {
    console.log('\n❌ M1 FAIL — no sendonly audio backchannel track found.');
    process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error('M1 error:', err.message ?? err);
  process.exitCode = 1;
});
