# TypeScript용 RTSP Backchannel

[![latest GitHub Release](https://img.shields.io/github/v/release/GagaKor/rtsp-backchannel?label=release)](https://github.com/GagaKor/rtsp-backchannel/releases/latest) [![rtsp-backchannel on npm](https://img.shields.io/npm/v/rtsp-backchannel?label=npm)](https://www.npmjs.com/package/rtsp-backchannel) [![rtsp-backchannel on PyPI](https://img.shields.io/pypi/v/rtsp-backchannel?label=PyPI)](https://pypi.org/project/rtsp-backchannel/) [![rtsp-backchannel on crates.io](https://img.shields.io/crates/v/rtsp-backchannel?label=crates.io)](https://crates.io/crates/rtsp-backchannel)

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)

ONVIF 카메라 검색, 프로필별 RTSP URI 조회, ONVIF RTSP 백채널을 통한 음원 파일
재생을 지원하는 TypeScript 라이브러리 및 CLI입니다. 파일 재생에만 별도 설치한
FFmpeg가 필요하며 GStreamer는 사용하지 않습니다.

다른 구현체:

- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md) — [PyPI 패키지](https://pypi.org/project/rtsp-backchannel/)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md) — [crates.io 패키지](https://crates.io/crates/rtsp-backchannel)

패키지는 백채널 세션을 열고 음원 파일 전체를 실시간 속도로 전송한 뒤 세션을
종료합니다. 입력 음원 디코딩에는 별도로 설치된 `ffmpeg` 실행 파일을 사용하며,
오디오 코덱 처리와 RTP/RTSP 전송은 TypeScript로 구현되어 있습니다. FFmpeg는 이
패키지에 포함되지 않고 자동으로 설치되지도 않습니다.

## 요구 사항

- 패키지를 `import`하려면 Node.js 22 이상, `require()`하려면 22.12 이상
  ([모듈 형식](#모듈-형식) 참고)
- 파일 재생 시 `PATH`에서 실행할 수 있는 `ffmpeg`
- ONVIF `sendonly` 오디오 백채널을 제공하는 카메라

카메라 검색과 스트림 URI 조회에는 FFmpeg가 필요하지 않습니다.

## 설치

```bash
npm install rtsp-backchannel
```

현재 릴리스 계열에 고정하려면 다음 명령을 사용합니다.

```bash
npm install rtsp-backchannel@^0.3
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

## 모듈 형식

이 패키지는 ES 모듈 빌드 하나만 배포하며, 두 모듈 시스템 모두에서 불러올 수 있습니다.

```typescript
// ES 모듈
import { playFile } from 'rtsp-backchannel';
```

```javascript
// CommonJS
const { playFile } = require('rtsp-backchannel');
```

`require()`가 동작하는 이유는 Node 22.12.0부터 CommonJS에서 ES 모듈을 동기적으로 불러올 수
있기 때문입니다. 두 진입점이 같은 파일을 가리키므로 한 프로세스가 라이브러리와 그 상태를
두 벌 들고 있는 일은 발생하지 않습니다.

`engines`는 Node 22 이상을 허용합니다. `import`는 그 전체 범위에서 동작하기 때문입니다.
22.12 하한은 `require()`에만 적용되며, 그 이전 22.x에서 이 패키지를 `require()`하면
`ERR_REQUIRE_ESM`으로 실패합니다.

### TypeScript와 CommonJS

CommonJS TypeScript 프로젝트는 `moduleResolution`을 `nodenext`(또는 `bundler`)로
설정해야 합니다.

```jsonc
{ "compilerOptions": { "module": "nodenext", "moduleResolution": "nodenext" } }
```

`moduleResolution: node16`에서는 CommonJS 파일에서 이 패키지를 import하면 `TS1479`가
발생합니다("the referenced file is an ECMAScript module and cannot be imported with
'require'"). `node16`에는 Node의 `require(esm)`에 대한 모델이 없고, 이 패키지는 CommonJS
선언 트리를 따로 배포하지 않고 ES 모듈 하나만 배포하므로 그 설정으로는 타입 검사를 통과할
수 없습니다. 런타임 동작에는 영향이 없고 컴파일 단계만 해당합니다.

### `require()`는 frozen 네임스페이스를 반환합니다

ES 모듈을 `require()`하면 Node의 module namespace 객체가 반환되는데, 이 객체는 sealed
상태입니다. export를 읽는 것은 문제없지만 교체는 되지 않습니다.

```javascript
const lib = require('rtsp-backchannel');

lib.playFile;                    // 정상
lib.playFile = fake;             // strict 모드에서 TypeError, sloppy 모드에서는 조용히 무시됨
jest.spyOn(lib, 'playFile');     // TypeError: Cannot redefine property: playFile
```

`sinon.stub(lib, 'playFile')`도 같은 방식으로 실패합니다. 테스트에서 동작을 바꿔야 한다면
라이브러리의 export가 아니라 라이브러리가 호출하는 경계(`ffmpeg`, 소켓)를 stub하거나,
`esmock` 같은 ESM 로더 mock을 사용하세요.

### MODULE_TYPELESS_PACKAGE_JSON 경고 없애기

`.js` 파일에서 `import` 문을 실행하면 다음 경고가 나타납니다.

```
[MODULE_TYPELESS_PACKAGE_JSON] Warning: Module type of file:///.../use.js is not
specified and it doesn't parse as CommonJS. Reparsing as ES module because module
syntax was detected. This incurs a performance overhead.
```

이 경고는 이 패키지가 아니라 실행 중인 파일에 대한 것입니다. Node가 가장 가까운
`package.json`에서 `"type"` 필드를 찾지 못해 CommonJS로 추측했다가 실패하고 다시 파싱한
것입니다. 다음 중 하나로 해결됩니다.

- 자신의 `package.json`에 `"type": "module"` 추가
- 파일 확장자를 `.mjs`로 변경
- `import` 대신 `require()` 사용

## 빠른 재생

```typescript
import { playFile } from 'rtsp-backchannel';

const packetsSent = await playFile({
  host: 'camera.local',
  user: '',
  pass: '',
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
| `discoverDevices(options?)` | `timeoutMs?`, `interfaces?`, `cidrs?`, `ports?`, `concurrency?` | `Promise<DiscoveredDevice[]>` |
| `getStreamUris(options)` | `host`, `user`, `pass`, `deviceUrls?`, `timeoutMs?` | `Promise<StreamUri[]>` |
| `getCameraCapabilities(options)` | `host`, `user?`, `pass?`, `deviceUrls?`, `timeoutMs?` | `Promise<CameraCapabilityReport>` |
| `openPtzSession(options)` | `host`, `user?`, `pass?`, `profileToken?`, `deviceUrls?`, `timeoutMs?`, `defaultMoveTimeoutMs?` | `Promise<PtzSession>` (실험적 기능) |
| `playFile(options)` | `host`, `user`, `pass`, `file`, `volume`, `codec` | RTP 패킷 수 `Promise<number>` |

`DiscoveredDevice`에는 `ip`, `xaddrs`, `scopes`와 선택적인 `name`, `hardware`,
`endpointReference`가 있습니다. `StreamUri`에는 `profileToken`, 선택적인
`profileName`, 인증정보가 삽입되지 않은 `uri`가 있습니다.

### 장치 검색

`cidrs` 없이 `discoverDevices()`를 호출하면 PC에서 감지한 로컬 IPv4 인터페이스를
통해 WS-Discovery multicast를 전송합니다. 같은 서브넷 또는 VLAN의 카메라를 찾는
기본 동작입니다. `interfaces`는 카메라 주소가 아니라 이 PC의 로컬 NIC 주소를
직접 지정하는 고급 옵션입니다.

라우팅 가능한 다른 대역이나 특정 주소를 검색하려면 IPv4 CIDR 또는 단일 IPv4를
배열로 전달합니다. 배열의 모든 항목을 검색하며 겹치는 호스트는 한 번만 확인합니다.

```typescript
const devices = await discoverDevices({
  cidrs: ['10.0.0.0/24', '10.128.0.10'],
  timeoutMs: 1000,
  ports: [80, 8000, 443],
  concurrency: 64,
});
```

CIDR 모드는 `/onvif/device_service`에 인증 전 ONVIF
`GetSystemDateAndTime` 요청을 보냅니다. `443`은 자체 서명 인증서를 허용하는
HTTPS로, 나머지 포트는 HTTP로 확인합니다. 기본 포트는 `80`, `8000`, `443`이고
기본 동시성은 `64`입니다. 한 번에 검색할 수 있는 고유한 사용 가능 IPv4 주소는
최대 4,096개입니다. `interfaces`와 `cidrs`는 함께 사용할 수 없습니다.

CIDR 검색 결과의 `xaddrs`에는 응답한 서비스 URL이 들어갑니다. 장치가 multicast
검색에도 응답하지 않는 한 `scopes`, `name`, `hardware`는 비어 있습니다. 대상
대역으로 라우팅할 수 있어야 하며 PC와 네트워크 방화벽에서 ONVIF 포트가 허용되어야
합니다. 카메라 IP를 이미 안다면 검색을 생략하고 `getStreamUris`에 바로 전달할 수
있습니다.

`getStreamUris`는 ONVIF Device 및 Media 서비스에 인증하고 모든 Media Profile의
RTSP URI를 반환합니다. 네트워크, 인증 및 프로토콜 오류는 Promise rejection으로
전달됩니다.

### 카메라 기능 보고서

`getCameraCapabilities`는 장치 식별 정보, scope, 광고된 서비스, Media profile,
PTZ 사실, Media2 encoder 근거를 하나의 보고서로 수집합니다. 비밀번호는
소스 코드에 넣지 말고 `ONVIF_PASSWORD`로 전달하십시오.

```typescript
import {
  getCameraCapabilities,
  type CameraCapabilityReport,
} from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const report: CameraCapabilityReport = await getCameraCapabilities({
  host: 'camera.local',
  user: 'operator',
  pass: password,
  deviceUrls: ['http://camera.local/onvif/device_service'],
  timeoutMs: 8000,
});

console.log(report.declaredProfiles, report.media2.h265Supported);
```

다음은 Profile S와 T를 선언하고 PTZ 서비스를 갖춘 카메라의 보고서 예시입니다.
CLI는 이 내용을 한 줄의 JSON으로 출력하며, 아래에서는 읽기 쉽도록
줄바꿈했습니다. `pan-node`는 `continuousPanTilt: true`이지만 `zoom-node`는
`absoluteZoom: true`만 가지고 있어, PTZ 서비스 광고만으로 pan/tilt 지원이
보장되지 않는다는 점과 최상위 `ptz.panTiltSupported`/`ptz.zoomSupported`가 두
node를 종합한 값이라는 점을 보여줍니다. 여기의 `declaredProfiles`도 장치
scope에서 얻은 자기 보고일 뿐 독립적인 ONVIF 인증 결과가 아닙니다.

```json
{
  "device": {
    "manufacturer": "Parity Camera",
    "model": "PX-1",
    "firmware": "1.2.3",
    "serial": "parity-001"
  },
  "scopes": [
    "onvif://www.onvif.org/Profile/Streaming",
    "onvif://www.onvif.org/Profile/T"
  ],
  "declaredProfiles": [
    "S",
    "T"
  ],
  "serviceDiscovery": "getServices",
  "services": [
    {
      "namespace": "http://www.onvif.org/ver10/media/wsdl",
      "xaddr": "http://camera.local/onvif/media1",
      "version": {
        "major": 1,
        "minor": 0
      }
    },
    {
      "namespace": "http://www.onvif.org/ver20/media/wsdl",
      "xaddr": "http://camera.local/onvif/media2",
      "version": {
        "major": 2,
        "minor": 0
      }
    },
    {
      "namespace": "http://www.onvif.org/ver20/ptz/wsdl",
      "xaddr": "http://camera.local/onvif/ptz",
      "version": {
        "major": 2,
        "minor": 2
      }
    }
  ],
  "profiles": [
    {
      "token": "shared",
      "source": "media2",
      "name": "Modern Shared",
      "hasAudioEncoder": true,
      "hasAudioOutput": false,
      "hasAudioSource": true,
      "ptzConfigurationToken": "ptz-config-m2",
      "ptzNodeToken": "pan-node"
    }
  ],
  "ptz": {
    "detected": true,
    "panTiltSupported": true,
    "zoomSupported": true,
    "profileTokens": [
      "shared"
    ],
    "serviceCapabilities": {
      "eFlip": true,
      "reverse": false,
      "getCompatibleConfigurations": true,
      "moveStatus": false,
      "statusPosition": true
    },
    "nodes": [
      {
        "token": "pan-node",
        "name": "Pan only",
        "spaces": {
          "absolutePanTilt": false,
          "absoluteZoom": false,
          "relativePanTilt": false,
          "relativeZoom": false,
          "continuousPanTilt": true,
          "continuousZoom": false
        },
        "maximumPresets": 4,
        "homeSupported": true,
        "auxiliaryCommands": [
          "IrisClose",
          "IrisOpen"
        ]
      },
      {
        "token": "zoom-node",
        "name": "Zoom only",
        "spaces": {
          "absolutePanTilt": false,
          "absoluteZoom": true,
          "relativePanTilt": false,
          "relativeZoom": false,
          "continuousPanTilt": false,
          "continuousZoom": false
        },
        "maximumPresets": 2,
        "homeSupported": false,
        "auxiliaryCommands": []
      }
    ]
  },
  "media2": {
    "detected": true,
    "encodings": [
      "H264",
      "H265"
    ],
    "h265Supported": true
  },
  "warnings": []
}
```

공개 보고서 필드의 의미는 다음과 같습니다.

- `device`는 카메라가 보고한 제조사, 모델, 펌웨어, 일련번호이고, `scopes`는
  중복을 제거한 원본 ONVIF scope 값입니다.
- `declaredProfiles`는 장치 scope에서 정규화한 profile 이름입니다. 장치의 자기
  보고일 뿐 독립적인 ONVIF 인증 결과가 아닙니다.
- `serviceDiscovery`는 서비스 목록을 `GetServices`, 기존 `GetCapabilities`
  fallback 중 어디에서 얻었는지 또는 얻지 못했는지를 나타냅니다. `services`에는
  namespace, XAddr, 선택적인 version 정보가 있습니다.
- `profiles`는 Media1/Media2 profile binding, audio 유무, 선택적인 PTZ
  configuration/node token을 설명합니다.
- `ptz`에서는 광고된 PTZ 서비스(`detected`), profile과 PTZ의 연결
  (`profileTokens`), 실제 movement space(`panTiltSupported`, `zoomSupported`,
  `nodes`)를 서로 다른 사실로 취급합니다. 어느 하나가 나머지를 보장하지 않습니다.
- `media2.detected`는 성공한 `GetServices` 응답이 Media2 서비스를 광고했는지만 나타냅니다.
  접근 가능 여부를 뜻하지 않으며 Media2 보강 요청이 실패해도 `true`로 유지될 수 있습니다.
  기존 `GetCapabilities` fallback 또는 서비스 검색 실패에서는 `null`입니다.
  `media2.encodings`와 `media2.h265Supported`는 성공한 encoder option 보강 결과입니다.
- `warnings`에는 선택적인 보강 요청의 실패가 들어갑니다. 각 `warning.message`는
  generic canonical message만 사용하며 credentials, WSSE digest material,
  URL userinfo, raw or real camera response payload를 포함하지 않습니다. 최초 연결 또는
  인증 실패는 치명적이며 Promise를 reject하므로 warning으로 바뀌지 않습니다.
- `audioSend`는 카메라로 오디오를 보낼 수 있는 전송 방식이 있는지를 보고합니다.
  `onvifBackchannel`과 `vigiTalk`는 서로 독립적인 probe가 아니라, 서로 다른 두 개의
  3상 사실입니다. `vigiTalk`는 `onvifBackchannel`이 확정적으로 `false`로 확인된
  경우에만 probe되고 그 외에는 `null`로 남으므로, ONVIF backchannel이 정상 동작하는
  카메라에서는 VIGI probe 자체가 실행되지 않습니다. `transport`는 그중 성공한 쪽의
  이름(`'onvif'`, `'vigi'`, 둘 다 실패하면 `null`)이며, `detected`는 둘 중 하나라도
  성공했을 때만 `true`입니다. 이 probe의 비용과 끄는 방법은 아래를 참고하십시오.

인증된 서비스 요청의 신뢰 기준은 선택된 Device Service URL입니다. 연결된 Media
endpoint와 광고된 모든 Media1, Media2, PTZ XAddr는 같은 scheme과 canonical
hostname 또는 IP 주소를 사용해야 합니다. 포트, path, query string은 달라도 됩니다.
카메라가 보고한 XAddr는 근거로 `services`에 그대로 남지만, 기준과 다른 endpoint에는
WS-Security material과 network request를 모두 보내지 않습니다. 선택적 보강에서는
generic warning을 추가하고 관련 근거를 비어 있거나 알 수 없는 상태로 남깁니다.

ONVIF 응답 header는 64 KiB, 응답 body/XML 입력은 1 MiB로 제한하며 XML element
깊이는 최대 64입니다. 선택적 보강이 이 예산을 넘으면 결과를 비어 있거나 알 수 없는
상태로 두고 자격 증명에 안전한 warning을 추가합니다. SOAP Fault는 canonical 인증
및 protocol code만 노출하며 알 수 없는 카메라 code는 payload를 반영하지 않고
`Fault`가 됩니다.

3상 boolean에는 의도가 있습니다. `true`는 성공한 응답에서 해당 사실을 찾았다는
뜻이고, `false`는 성공한 응답이 부재를 확인했다는 뜻이며, `null`은 사실을 확인할
수 없었다는 뜻입니다. 장치가 보고하지 않은 선택적 object member는 생략됩니다.
특히 기존 `GetCapabilities`만 사용한 결과에서는 `media2.detected`와
`media2.h265Supported`가 `null`로 남습니다. Media2 서비스 광고와 성공한 H.265
option 보강은 유용한 근거이지만 Profile T 인증의 증명은 아닙니다.

`timeoutMs`는 요청 하나마다 적용되는 timeout입니다. 기능 보고서는 여러 요청을
수행하므로 전체 소요 시간은 timeout 한 구간보다 길 수 있습니다. 선택적인 보강
요청은 실패 시 warning을 추가하고 계속할 수 있지만 단일 요청의 timeout을 늘리지는
않습니다.

**기본값에서는 이 호출이 보이는 것보다 비쌉니다.** `getCameraCapabilities`는
`audioSend`를 채우기 위해 카메라에 오디오를 보낼 수 있는 경로가 있는지도 기본적으로
함께 확인합니다. 이 probe는 위에서 이미 연 연결을 다시 읽는 것이 아니라, 실제
backchannel `DESCRIBE`를 보내려고 처음부터 새로 인증하는 ONVIF 세션(자체 서비스
검색과 로그인 포함)을 하나 더 엽니다. 그 결과 송신 가능한 track을 찾지 못했을 때만
VIGI OpenAPI `doAuth` 핸드셰이크를 한 번 더 시도합니다. 구체적으로는 기본 호출마다
여러 번의 추가 SOAP 왕복과 ONVIF 재인증·재검색 한 번이 붙고, ONVIF backchannel이
없는 흔한 경우에는 대부분의 카메라가 아예 응답하지 않는 VIGI 제어 포트로의 HTTPS
요청이 한 번 더 붙습니다. 이 전부를 건너뛰려면 `probeAudioSend: false`를
넘기십시오. 이 경우 `audioSend`는 중립적인 기본값(`detected`, `transport`,
`onvifBackchannel`, `vigiTalk` 모두 `null`)으로 남습니다.

### PTZ 제어

`openPtzSession`은 카메라 한 대에 대한 제어 세션을 엽니다. 먼저 연결한 뒤
`GetServices`와 `GetNodes`를 실행해 PTZ 서비스와 그 node를 찾고, Media Profile
token을 정합니다. `profileToken`을 직접 지정하지 않으면 PTZ를 지원하는 첫
번째 profile을 사용합니다. 이어서 해당 node가 지원하는 PTZ space를 캐시해
두어, 이후의 모든 호출을 카메라가 실제로 광고한 기능과 대조해 검사합니다.
반환되는 `PtzSession`은 `getCameraCapabilities`와 `getStreamUris`가 사용하는
것과 같은 인증된 transport를 재사용합니다. PTZ 요청은 새 연결이 아니라 기존
연결 위의 또 다른 SOAP body일 뿐입니다.

`PtzSession`은 `continuousMove`, `absoluteMove`, `relativeMove`, `stop`,
`getStatus`와 `close`를 제공합니다. 각 move 메서드는 카메라의 PTZ node가 해당
space를 광고하지 않았다면 요청을 보내기 전에 reject합니다. 예를 들어
`continuousZoom: false`를 보고한 node에서 `continuousMove({ zoom: ... })`을
호출하면 거부됩니다. Pan/tilt 값과 대부분의 zoom 값은 `-1.0`~`1.0`이며, 절대
zoom *위치*만 `0.0`~`1.0`입니다. `close()`는 세션을 닫힌 상태로 표시하기 전에
pan/tilt와 zoom 모두에 대해 best-effort로 `stop()`을 호출하므로, 호출자가
종료 시점에 movement 정지를 따로 챙기지 않아도 됩니다.

모든 `continuousMove` 호출에는 카메라로 전달되는 device-side timeout이
포함되며, 기본값은 1000ms입니다. 카메라는 이 timeout이 지나면 스스로
움직임을 멈추므로, 한 번의 호출로는 카메라가 약 1초 동안만 움직입니다.
계속 움직이게 하려면 호출자가 이전 timeout이 끝나기 전에 `continuousMove`를
다시 호출해야 합니다. 이 기본값은 `defaultMoveTimeoutMs`로 제어하며,
호출마다 다른 값을 쓰려면 `timeoutMs`로 재정의할 수 있습니다. 이는 의도된
안전장치입니다. 클라이언트가 멈추라고 지시하지 않아도 카메라가 스스로
정지하므로, 클라이언트가 비정상 종료되거나 연결이 끊겨도 카메라가 계속
움직이는 상태로 남지 않습니다.

비밀번호는 소스 코드에 넣지 말고 `ONVIF_PASSWORD`로 전달하십시오.

```typescript
import {
  openPtzSession,
  type PtzSession,
} from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const session: PtzSession = await openPtzSession({
  host: 'camera.local',
  user: 'operator',
  pass: password,
  deviceUrls: ['http://camera.local/onvif/device_service'],
  timeoutMs: 8000,
});

try {
  await session.continuousMove({ panTilt: { x: 0.5, y: 0 }, timeoutMs: 2000 });
  const status = await session.getStatus();
  console.log(status.panTilt, status.zoom);
} finally {
  await session.close();
}
```

**실험적 기능입니다.** 물리적 움직임은 이제 카메라 한 대에서 검증되었습니다.
TP-Link VIGI C540V(펌웨어 2.2.0 및 2.3.3)에서 `relativeMove`, `continuousMove`,
`absoluteMove`가 pan/tilt와 zoom 양쪽 모두 요청한 방향으로 카메라를 움직였고,
`getStatus`가 매 이동을 추적했으며, 시작 좌표로의 `absoluteMove`가 원위치를
정확히 복원했습니다. 세션 열기, 기능 지원 확인(guard), 요청 구성, timeout 포함,
close 시 stop 호출은 그대로 테스트로 덮여 있습니다. 이 한 모델을 넘어서는
미검증입니다 — 광학 zoom(C540V는 디지털입니다)이나 기계식 preset tour를 갖춘
카메라는 확인하지 못했습니다. 또한 이 카메라는 1초 미만 `Timeout`을 거부하므로
`timeoutMs`가 1000 미만인 `continuousMove`는 실패합니다. 정수 초를
`PT1.000S`가 아니라 `PT1S`로 보내는 것도 이 카메라가 소수점을 거부하기
때문입니다.

### 저수준 백채널 API

세션 수명이나 인코딩된 RTP 프레임을 직접 제어하려면 `openBackchannel`을 사용합니다.
오류가 발생한 경우를 포함해 세션을 항상 닫아야 합니다.

```typescript
import { fileToRtpAudio, openBackchannel } from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const session = await openBackchannel('camera.local', 'admin', password);
try {
  const encoded = await session.withKeepAlive(
    () => fileToRtpAudio(
      '/absolute/path/to/event.mp3',
      session.codec,
      0.05,
    ),
  );
  const packetsSent = await session.send(encoded);
  console.log({ packetsSent });
} finally {
  await session.close();
}
```

`withKeepAlive`는 FFmpeg가 파일을 읽고 인코딩하는 동안 짧은 RTSP 세션이 만료되지
않도록 합니다. `session.send`는 페이싱된 RTP 전송 중에도 keepalive를 계속 처리합니다.

PCM 생성, 인코딩 또는 페이싱을 직접 제어할 수 있도록 `pcm16ToG711`,
`linearToALaw`, `linearToMuLaw`, `generateTonePcm`, `sendPacedG711`도 공개합니다.

### 오디오 송신 전송

`openBackchannel`과 `playFile`은 `transport` 옵션으로 `'auto'`(기본값),
`'onvif'`, `'vigi'`를 받아들이며, CLI도 같은 선택지를 `--transport`로 제공합니다.
`'onvif'`와 `'vigi'`는 각각 하나의 전송 방식을 확정적으로 사용합니다. `'auto'`는
먼저 ONVIF 백채널을 시도하고, 카메라가 응답은 했지만 SDP에 sendonly 오디오 track이
없을 때만 VIGI로 대체합니다. 그 밖의 모든 실패 — 네트워크 오류, 자격 증명 거부,
잘못된 응답 — 는 다른 전송 방식으로 조용히 넘어가지 않고 그대로 전파되므로, 고장난
카메라를 단순히 벤더 API가 없는 카메라로 오인하지 않습니다.

VIGI 전송 방식은 ONVIF 백채널 대신 TP-Link의 VIGI OpenAPI `talk` 프로토콜을
사용합니다. 스피커는 동작하지만 ONVIF 백채널이 없거나 제대로 동작하지 않는 카메라를
위한 것입니다. 카메라 자체의 웹 UI에서 Settings > Network Settings > OpenAPI로
OpenAPI를 켜야 하며, 제어 연결은 기본적으로 20443 포트를 사용합니다. 이 전송
방식은 G.711 a-law만 전달합니다. G.711이 아닌 코덱을 명시적으로 요청하면
resampling이나 조용한 다운그레이드 대신 세션을 여는 시점에 실패합니다. 라이브러리는
장치의 스피커 볼륨을 직접 바꾸지 않습니다 — 카메라에 설정된 값(기본값 80/100이며
실내에서는 큰 편입니다)이 그대로 재생되며, 볼륨을 바꾸려면 이 라이브러리가 아니라
카메라 자체 UI를 사용해야 합니다.

VIGI 배지가 붙어 있다고 해서 모든 카메라가 이 API를 지원하는 것은 아닙니다.
TP-Link는 지원하는 IPC 및 NVR 모델 목록을
https://www.tp-link.com/en/vigi-open-api/product-list/ 에 공개해 두었으므로,
브랜드만 보고 지원 여부를 가정하지 말고 실제 모델을 그 목록과 대조하십시오. VIGI
NVR도 이 목록에 함께 실려 있지만 전혀 다른 프로토콜을 사용하므로 이 전송 방식의
지원 대상이 아닙니다.

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

# CIDR 전체와 단일 IP를 함께 검색합니다.
rtsp-backchannel discover \
  --cidr 10.0.0.0/24 \
  --cidr 10.128.0.10 \
  --timeout-ms 1000 \
  --port 80 \
  --port 8000 \
  --concurrency 64

# 모든 ONVIF Media Profile의 RTSP URI를 조회합니다.
rtsp-backchannel streams \
  --host camera.local \
  --user admin

# camelCase 카메라 기능 보고서 하나를 JSON 한 줄로 출력합니다.
rtsp-backchannel capabilities \
  --host camera.local \
  --user operator \
  --device-url http://camera.local/onvif/device_service \
  --timeout-ms 8000

# 음원 한 파일을 재생하고 RTSP 세션을 종료합니다.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

하위 호환성을 위해 `play` 단어는 생략할 수 있습니다. 수동 실행에서는 `--pass`도
사용할 수 있지만, `ONVIF_PASSWORD`를 사용하면 비밀번호가 프로세스 인자 목록에
노출되지 않습니다. `capabilities`는 반복된 `--device-url`을 입력 순서대로 받고
native camelCase JSON 보고서를 정확히 한 줄 출력합니다. `--timeout-ms`를 생략하면
client 기본값을 사용하며, 지정할 때는 0보다 큰 유한한 숫자여야 합니다.
최댓값 86,400,000ms(24시간)는 포함됩니다. 기능 인자 검증은 option 값이나 자격
증명을 되풀이하지 않는 고정 진단을 사용하고 bare `--`를 거부합니다. 알려진 flag가
아닌 하이픈 시작 비밀번호는 `--pass <값>`으로, 모호하지 않은 형태는
`--pass=<값>`으로 전달할 수 있습니다. 명시적인 빈 비밀번호와 생략된 비밀번호의
구분은 유지됩니다. 비밀이 프로세스 인자 목록에 남지 않도록 `ONVIF_PASSWORD` 사용을
권장합니다.

## 재생 동작

- SDP 자동 협상: PCMA, PCMU, G726-32, G726-24, G726-16, G726-40, AAC
- G711, RFC3551 G726, RFC 3640 MPEG4-GENERIC AAC-hbr 지원
- MP4A-LATM은 명시적으로 지원하지 않음
- TCP interleaved RTP
- 40ms 오디오 패킷과 실시간 페이싱
- 긴 음원 재생 중 RTSP keepalive 전송
- 성공 또는 실패 후 RTSP 세션 종료

첫 번째 ONVIF Media Profile이 지원 코덱을 제공하는 `sendonly` 오디오 트랙을
포함해야 합니다. 오디오 출력과 디코더 설정은 카메라마다 다르므로 RTSP 세션이
정상적으로 열려도 카메라의 출력이 비활성화되었거나 잘못 연결되어 있으면 소리가 나지
않을 수 있습니다.

## 인증, RTSP 및 코덱

빈 `user`와 `pass`는 ONVIF WS-Security와 RTSP 인증을 생략합니다. 비어 있지 않은
ONVIF 자격 증명은 PasswordDigest를 사용하고 RTSP 인증은 서버 challenge 뒤에
전송합니다. WS-Security digest는 인증일 뿐 전송 암호화가 아닙니다. 자체 서명 TLS를
포함한 HTTP/HTTPS 호환성을 지원하므로 신뢰할 수 있는 네트워크 또는 VPN을 사용하십시오.

기본 SDP 자동 협상 순서는 PCMA, PCMU, G726-32, G726-24, G726-16, G726-40, AAC입니다.
G711, RFC3551 G726, RFC 3640 MPEG4-GENERIC AAC-hbr을 지원하며 MP4A-LATM은 명시적으로
지원하지 않습니다. `codec`/`--codec`로 코덱을 지정하면 다른 코덱으로 대체하지 않습니다.
`session.variant`는 G.711에서만 값이 있는 선택적 값이며 G.726/AAC에서는 `undefined`입니다.

직접 RTSP는 ONVIF를 우회합니다. 내장 자격 증명은 자동 파싱되고 비어 있지 않은 명시적
인자가 우선합니다. 비밀번호의 `@`는 `%40`으로 쓰는 것을 권장하며 raw `@`는 authority의
마지막 구분자를 사용합니다. 요청 URI와 로그에서는 자격 증명이 제거됩니다.

```typescript
const packetsSent = await playFile({
  host: 'rtsp://admin:p%40ss@camera.local/backchannel',
  user: '', pass: '', file: '/absolute/path/to/event.mp3', codec: 'auto',
});
```

```bash
# 자격 증명 없음
rtsp-backchannel play --host camera.local --file '/absolute/path/to/event.mp3'
# 직접 RTSP
rtsp-backchannel play --host 'rtsp://admin:p%40ss@camera.local/backchannel' \
  --file '/absolute/path/to/event.mp3'
```

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
