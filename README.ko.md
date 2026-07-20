# TypeScript용 RTSP Backchannel

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)

ONVIF 카메라 검색, 프로필별 RTSP URI 조회, ONVIF RTSP 백채널을 통한 음원 파일
재생을 지원하는 TypeScript 라이브러리 및 CLI입니다. GStreamer는 필요하지 않습니다.

다른 구현체:

- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md)

패키지는 백채널 세션을 열고 음원 파일 전체를 실시간 속도로 전송한 뒤 세션을
종료합니다. 입력 음원 디코딩에는 별도로 설치된 `ffmpeg` 실행 파일을 사용하며,
G.711 인코딩과 RTP/RTSP 전송은 TypeScript로 구현되어 있습니다. FFmpeg는 이
패키지에 포함되지 않고 자동으로 설치되지도 않습니다.

## 요구 사항

- Node.js 22 이상
- 파일 재생 시 `PATH`에서 실행할 수 있는 `ffmpeg`
- ONVIF `sendonly` 오디오 백채널을 제공하는 카메라

카메라 검색과 스트림 URI 조회에는 FFmpeg가 필요하지 않습니다.

## 설치

```bash
npm install rtsp-backchannel
```

Registry 릴리스 대신 현재 `master` 소스를 설치하려면 다음 명령을 사용합니다.

```bash
npm install "github:GagaKor/rtsp-backchannel"
```

음원 파일을 재생하려면 FFmpeg를 별도로 설치합니다.

```bash
# macOS
brew install ffmpeg

# Ubuntu 또는 Debian
sudo apt-get update
sudo apt-get install ffmpeg
```

Windows에서는 [FFmpeg 다운로드 페이지](https://ffmpeg.org/download.html)에서 빌드를
설치한 뒤 `ffmpeg.exe`가 있는 디렉터리를 `PATH`에 추가합니다.

## 빠른 재생

```typescript
import { playFile } from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const packetsSent = await playFile({
  host: 'camera.local',
  user: 'admin',
  pass: password,
  file: '/absolute/path/to/event.mp3',
  volume: 0.05,
});

console.log({ packetsSent });
```

`volume`은 `0.0`부터 `1.0`까지 지정할 수 있으며 검증된 기본값은 `0.05`입니다.

## 전체 워크플로

카메라 주소를 알고 있다면 검색을 생략할 수 있습니다. 스트림 조회는 ONVIF Media
Profile을 확인할 때 유용하지만, 현재 `playFile`은 호출자가 선택한 `StreamUri`를
입력받지 않고 첫 번째 프로필을 독립적으로 다시 엽니다.

```typescript
import {
  discoverDevices,
  getStreamUris,
  playFile,
} from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const devices = await discoverDevices({ timeoutMs: 3000 });
const camera = devices[0];
if (!camera) throw new Error('no ONVIF device found');

const streams = await getStreamUris({
  host: camera.ip,
  user: 'admin',
  pass: password,
  deviceUrls: camera.xaddrs,
  timeoutMs: 8000,
});

for (const stream of streams) {
  console.log(stream.profileToken, stream.profileName, stream.uri);
}

const packetsSent = await playFile({
  host: camera.ip,
  user: 'admin',
  pass: password,
  file: '/absolute/path/to/event.mp3',
  volume: 0.05,
});

console.log({ packetsSent });
```

## 공개 API

| API | 주요 옵션 | 반환값 |
| --- | --- | --- |
| `discoverDevices(options?)` | `timeoutMs?`, `interfaces?: string[]` | `Promise<DiscoveredDevice[]>` |
| `getStreamUris(options)` | `host`, `user`, `pass`, `deviceUrls?`, `timeoutMs?` | `Promise<StreamUri[]>` |
| `playFile(options)` | `host`, `user`, `pass`, `file`, `volume` | RTP 패킷 수 `Promise<number>` |

`DiscoveredDevice`에는 `ip`, `xaddrs`, `scopes`와 선택적인 `name`, `hardware`,
`endpointReference`가 있습니다. `StreamUri`에는 `profileToken`, 선택적인
`profileName`, 인증정보가 삽입되지 않은 `uri`가 있습니다.

`discoverDevices`는 WS-Discovery multicast를 사용하므로 일반적으로 카메라와 같은
서브넷 또는 VLAN에 연결된 IPv4 인터페이스에서 실행해야 합니다. 여러 NIC나 VLAN을
빠짐없이 검색해야 한다면 `interfaces`에 각 로컬 IPv4 주소를 명시합니다.

`getStreamUris`는 ONVIF Device 및 Media 서비스에 인증하고 모든 Media Profile의
RTSP URI를 반환합니다. 네트워크, 인증 및 프로토콜 오류는 Promise rejection으로
전달됩니다.

### 저수준 백채널 API

세션 수명이나 인코딩된 G.711 버퍼를 직접 제어하려면 `openBackchannel`을 사용합니다.
오류가 발생한 경우를 포함해 세션을 항상 닫아야 합니다.

```typescript
import { fileToG711, openBackchannel } from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const session = await openBackchannel('camera.local', 'admin', password);
try {
  const g711 = await fileToG711(
    '/absolute/path/to/event.mp3',
    session.variant,
    0.05,
  );
  const packetsSent = await session.send(g711);
  console.log({ packetsSent });
} finally {
  await session.close();
}
```

PCM 생성, 인코딩 또는 페이싱을 직접 제어할 수 있도록 `pcm16ToG711`,
`linearToALaw`, `linearToMuLaw`, `generateTonePcm`, `sendPacedG711`도 공개합니다.

## CLI

비밀번호를 화면이나 셸 히스토리에 남기지 않고 환경변수로 설정합니다.

```bash
printf 'Camera password: '
read -rs ONVIF_PASSWORD
printf '\n'
export ONVIF_PASSWORD
```

설치된 명령은 다음과 같이 사용합니다.

```bash
# 카메라 검색. 결과 하나당 JSON 한 줄을 출력합니다.
rtsp-backchannel discover --timeout-ms 3000

# 여러 NIC 또는 VLAN에서 검색할 인터페이스를 직접 지정합니다.
rtsp-backchannel discover \
  --interface 192.0.2.20 \
  --interface 198.51.100.20

# 모든 ONVIF Media Profile의 RTSP URI를 조회합니다.
rtsp-backchannel streams \
  --host camera.local \
  --user admin

# 음원 한 파일을 재생하고 RTSP 세션을 종료합니다.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

하위 호환성을 위해 `play` 단어는 생략할 수 있습니다. 수동 실행에서는 `--pass`도
사용할 수 있지만, `ONVIF_PASSWORD`를 사용하면 비밀번호가 프로세스 인자 목록에
노출되지 않습니다.

## 재생 동작

- G.711 8kHz mono
- PCMA 우선, 카메라가 PCMU만 제공하면 PCMU 사용
- TCP interleaved RTP
- 40ms 오디오 패킷과 실시간 페이싱
- 긴 음원 재생 중 RTSP keepalive 전송
- 성공 또는 실패 후 RTSP 세션 종료

첫 번째 ONVIF Media Profile이 PCMA 또는 PCMU를 제공하는 `sendonly` 오디오 트랙을
포함해야 합니다. 오디오 출력과 디코더 설정은 카메라마다 다르므로 RTSP 세션이
정상적으로 열려도 카메라의 출력이 비활성화되었거나 잘못 연결되어 있으면 소리가 나지
않을 수 있습니다.

## 개발

```bash
npm install
npm run build
npm test
npm run typecheck
```

버전 변경과 Registry 배포 절차는
[RELEASING.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/RELEASING.md)에
정리되어 있습니다.

## 라이선스

사용자가 선택할 수 있는
[MIT](https://github.com/GagaKor/rtsp-backchannel/blob/master/LICENSE-MIT) 또는
[Apache-2.0](https://github.com/GagaKor/rtsp-backchannel/blob/master/LICENSE-APACHE)
라이선스로 배포합니다.

이 패키지는 FFmpeg를 포함하거나 링크하지 않습니다. 애플리케이션에서 FFmpeg를 함께
번들하거나 재배포한다면 해당 FFmpeg 빌드의 라이선스 조건을 별도로 확인해야 합니다.
[FFmpeg Legal](https://ffmpeg.org/legal.html)과
[THIRD_PARTY_NOTICES.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/THIRD_PARTY_NOTICES.md)를
참고하십시오.

ONVIF는 ONVIF, Inc.의 상표입니다. 이 프로젝트는 ONVIF, Inc.와 독립적으로
개발되었고 제휴 또는 보증을 받지 않았으며 ONVIF Profile 적합성을 주장하지 않습니다.
