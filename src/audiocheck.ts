/**
 * Verify a camera's audio support via both ONVIF commands and the VIGI
 * OpenAPI control channel. Two transports can carry outbound audio —
 * ONVIF/RTSP backchannel and TP-Link's VIGI OpenAPI `talk` protocol — and a
 * camera can support one without the other, so this script checks both
 * before concluding that two-way audio is unavailable.
 *
 *   ONVIF_PASSWORD='<password>' npm run audiocheck -- --host camera.local
 */
import { OnvifDevice } from './onvif/deviceClient.ts';
import { displayRtspTarget, redactRtspCredentials } from './backchannel.ts';
import { openVigiControl } from './vigi/control.ts';

function arg(name: string, def?: string): string {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

const yn = (b: boolean) => (b ? '✅ 지원' : '❌ 없음');

async function main(): Promise<void> {
  const host = arg('host');
  const user = arg('user', 'admin');
  const pass = arg('pass', process.env.ONVIF_PASSWORD);

  console.log(`# ONVIF 오디오 지원 점검 @ ${displayRtspTarget(host)}`);
  const dev = new OnvifDevice(host, user, pass);
  const info = await dev.connect();
  console.log(`  장치: ${info.manufacturer ?? '?'} ${info.model ?? '?'} (fw ${info.firmware ?? '?'})\n`);

  const [profiles, sources, outputs, outCfg] = await Promise.all([
    dev.getProfiles(),
    dev.getAudioSources().catch(() => [] as string[]),
    dev.getAudioOutputs().catch(() => [] as string[]),
    dev.getAudioOutputConfigurations().catch(() => ({
      configTokens: [],
      outputTokens: [],
      outputLevels: [] as number[],
      sendPrimaryAudio: [] as string[],
    })),
  ]);

  console.log('— ONVIF Media 명령 결과 —');
  console.log(`  GetAudioSources (마이크/입력)       : ${yn(sources.length > 0)}  tokens=[${sources.join(', ')}]`);
  console.log(`  GetAudioOutputs (스피커/출력)        : ${yn(outputs.length > 0)}  tokens=[${outputs.join(', ')}]`);
  console.log(
    `  GetAudioOutputConfigurations         : configs=[${outCfg.configTokens.join(', ')}]` +
      ` outputLevel=[${outCfg.outputLevels.join(', ')}] (0-100)`,
  );

  const anyEncoder = profiles.some((p) => p.hasAudioEncoder);
  const anyOutputCfg = profiles.some((p) => p.hasAudioOutput);
  const anySourceCfg = profiles.some((p) => p.hasAudioSource);
  console.log('\n— 미디어 프로파일의 오디오 구성 —');
  console.log(`  AudioEncoderConfiguration (스트림 오디오) : ${yn(anyEncoder)}`);
  console.log(`  AudioOutputConfiguration  (스피커 출력)   : ${yn(anyOutputCfg)}`);
  console.log(`  AudioSourceConfiguration  (마이크 입력)   : ${yn(anySourceCfg)}`);

  const micSupported = sources.length > 0 || anySourceCfg;
  const onvifOutput = outputs.length > 0 || anyOutputCfg;

  console.log('\n— VIGI OpenAPI 결과 —');
  let vigiSpeaker = false;
  let vigiChecked = false;
  try {
    const control = await openVigiControl({ host, user, pass });
    const vigiAudio = await control.getAudioCapability();
    vigiSpeaker = vigiAudio.speaker;
    vigiChecked = true;
    console.log(
      `  getAudioCapability                   : speaker=${yn(vigiAudio.speaker)}` +
        ` microphone=${yn(vigiAudio.microphone)}`,
    );
  } catch (err) {
    // A diagnostic must never throw just because a camera has no VIGI
    // OpenAPI (it is off by default and most cameras do not implement it
    // at all) — note the failure and leave the ONVIF-derived verdict as is.
    const message = err instanceof Error ? err.message : String(err);
    console.log(`  확인 불가 (OpenAPI 미지원이거나 비활성화됨) : ${redactRtspCredentials(message)}`);
  }

  let transportLabel = '없음';
  if (onvifOutput) transportLabel = 'ONVIF 백채널';
  else if (vigiSpeaker) transportLabel = 'VIGI OpenAPI talk';

  console.log('\n=== 결론 ===');
  console.log(`  마이크(수신)        : ${yn(micSupported)}`);
  console.log(`  스피커 출력(송출)   : ${yn(onvifOutput || vigiSpeaker)}`);
  console.log(`  사용 가능한 전송    : ${transportLabel}`);
  if (onvifOutput) {
    console.log('  → ONVIF 백채널로 음원 송출 가능 (npm run play/m3 로 검증, 기본 --transport auto).');
    if (outCfg.outputLevels.some((l) => l === 0)) {
      console.log('  ⚠️ OutputLevel 0 감지 — 볼륨이 음소거 상태입니다.');
    }
  } else if (vigiSpeaker) {
    console.log('  → VIGI OpenAPI talk로 음원 송출 가능 (npm run play -- --transport vigi 로 검증).');
  } else if (vigiChecked) {
    console.log('  → ONVIF와 VIGI OpenAPI 모두 스피커 출력을 보고하지 않았습니다.');
  }
}

main().catch((err) => {
  const message = err instanceof Error ? err.message : String(err);
  console.error('audiocheck error:', redactRtspCredentials(message));
  process.exitCode = 1;
});
