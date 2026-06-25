/**
 * Verify a camera's audio support purely via ONVIF commands.
 *
 *   npm run audiocheck -- --host 172.168.46.56 --user admin --pass CHANGEME
 */
import { OnvifDevice } from './onvif/deviceClient.ts';

function arg(name: string, def: string): string {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

const yn = (b: boolean) => (b ? '✅ 지원' : '❌ 없음');

async function main(): Promise<void> {
  const host = arg('host', '172.168.46.56');
  const user = arg('user', 'admin');
  const pass = arg('pass', 'CHANGEME');

  console.log(`# ONVIF 오디오 지원 점검 @ ${host}`);
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

  const mic = sources.length > 0 || anySourceCfg;
  const spk = outputs.length > 0 || anyOutputCfg;
  console.log('\n=== 결론 ===');
  console.log(`  마이크(수신)        : ${yn(mic)}`);
  console.log(`  스피커 출력(송출)   : ${yn(spk)}`);
  console.log(`  양방향 음성 가능     : ${yn(mic && spk)}`);
  if (spk) {
    console.log('  → 스피커 출력 지원: ONVIF/RTSP 백채널로 음원 송출 가능 (npm run play/m3 로 검증).');
    if (outCfg.outputLevels.some((l) => l === 0)) {
      console.log('  ⚠️ OutputLevel 0 감지 — 볼륨이 음소거 상태입니다.');
    }
  }
}

main().catch((err) => {
  console.error('audiocheck error:', err.message ?? err);
  process.exitCode = 1;
});
