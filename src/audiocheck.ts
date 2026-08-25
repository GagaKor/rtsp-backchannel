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
import { vigiHardwareLikelihood } from './onvif/capabilities.ts';
import { openVigiControl } from './vigi/control.ts';

function arg(name: string, def?: string): string {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  if (def !== undefined) return def;
  throw new Error(`missing --${name}`);
}

const VIGI_MODES = ['auto', 'always', 'never'] as const;
type VigiMode = typeof VIGI_MODES[number];

const yn = (b: boolean) => (b ? '✅ 지원' : '❌ 없음');

/**
 * Three-state rendering. `null` means "not established" and must never print
 * as ❌: a probe that failed has not shown that the hardware lacks the
 * feature, and this script's whole purpose is to stop reporting an untested
 * transport as an absent one.
 */
const ynu = (b: boolean | null) => (b === null ? '❔ 확인 불가' : yn(b));

/** true if either holds; false only when both are established false. */
function either(a: boolean | null, b: boolean | null): boolean | null {
  if (a === true || b === true) return true;
  if (a === false && b === false) return false;
  return null;
}

/** true only if both hold; false as soon as either is established false. */
function both(a: boolean | null, b: boolean | null): boolean | null {
  if (a === false || b === false) return false;
  if (a === true && b === true) return true;
  return null;
}

async function main(): Promise<void> {
  const host = arg('host');
  const user = arg('user', 'admin');
  const pass = arg('pass', process.env.ONVIF_PASSWORD);
  const vigiMode = arg('vigi', 'auto') as VigiMode;
  if (!VIGI_MODES.includes(vigiMode)) {
    throw new RangeError(`--vigi must be one of: ${VIGI_MODES.join(', ')}`);
  }

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
  // null until the OpenAPI actually answers. Asking is not free: it sends a
  // credential-bearing doAuth to a port that counts failed attempts toward a
  // device lockout, with a password the ONVIF exchange never validated (the
  // OpenAPI admin account is configured separately). So the same vendor gate
  // the capability report uses applies here — a camera that names another
  // manufacturer cannot have this API, and spending a lockout attempt on it
  // is pure downside.
  let vigiSpeaker: boolean | null = null;
  const isVigi = vigiMode === 'always' ? true
    : vigiMode === 'never' ? null
      : vigiHardwareLikelihood(info);
  if (isVigi === true) {
    try {
      const control = await openVigiControl({ host, user, pass });
      const vigiAudio = await control.getAudioCapability();
      vigiSpeaker = vigiAudio.speaker;
      console.log(
        `  getAudioCapability                   : speaker=${yn(vigiAudio.speaker)}` +
          ` microphone=${yn(vigiAudio.microphone)}`,
      );
    } catch (err) {
      // A diagnostic must never throw just because a camera has no VIGI
      // OpenAPI (it is off by default and most cameras do not implement it
      // at all) — note the failure and leave vigiSpeaker unestablished.
      const message = err instanceof Error ? err.message : String(err);
      console.log(`  확인 불가 (OpenAPI 미지원이거나 비활성화됨) : ${redactRtspCredentials(message)}`);
    }
  } else if (isVigi === false) {
    console.log('  건너뜀 — 장치가 TP-Link/VIGI 하드웨어로 보고되지 않음 (--vigi always 로 강제).');
  } else {
    const why = vigiMode === 'never' ? '--vigi never' : '장치가 제조사를 보고하지 않음';
    console.log(`  건너뜀 — ${why} (--vigi always 로 강제).`);
  }

  const speakerSupported = either(onvifOutput, vigiSpeaker);
  let transportLabel = speakerSupported === null ? '확인 불가' : '없음';
  if (onvifOutput) transportLabel = 'ONVIF 백채널';
  else if (vigiSpeaker === true) transportLabel = 'VIGI OpenAPI talk';

  console.log('\n=== 결론 ===');
  console.log(`  마이크(수신)        : ${yn(micSupported)}`);
  console.log(`  스피커 출력(송출)   : ${ynu(speakerSupported)}`);
  console.log(`  양방향 음성 가능     : ${ynu(both(micSupported, speakerSupported))}`);
  console.log(`  사용 가능한 전송    : ${transportLabel}`);
  if (onvifOutput) {
    console.log('  → ONVIF 백채널로 음원 송출 가능 (npm run play/m3 로 검증, 기본 --transport auto).');
    if (outCfg.outputLevels.some((l) => l === 0)) {
      console.log('  ⚠️ OutputLevel 0 감지 — 볼륨이 음소거 상태입니다.');
    }
  } else if (vigiSpeaker === true) {
    console.log('  → VIGI OpenAPI talk로 음원 송출 가능 (npm run play -- --transport vigi 로 검증).');
  } else if (speakerSupported === false) {
    console.log('  → ONVIF와 VIGI OpenAPI 모두 스피커 출력을 보고하지 않았습니다.');
  } else {
    console.log('  → ONVIF는 스피커 출력을 보고하지 않았고, VIGI OpenAPI는 확인되지 않았습니다.');
  }
}

main().catch((err) => {
  const message = err instanceof Error ? err.message : String(err);
  console.error('audiocheck error:', redactRtspCredentials(message));
  process.exitCode = 1;
});
