# Python용 RTSP Backchannel

[![latest GitHub Release](https://img.shields.io/github/v/release/GagaKor/rtsp-backchannel?label=release)](https://github.com/GagaKor/rtsp-backchannel/releases/latest) [![rtsp-backchannel on npm](https://img.shields.io/npm/v/rtsp-backchannel?label=npm)](https://www.npmjs.com/package/rtsp-backchannel) [![rtsp-backchannel on PyPI](https://img.shields.io/pypi/v/rtsp-backchannel?label=PyPI)](https://pypi.org/project/rtsp-backchannel/) [![rtsp-backchannel on crates.io](https://img.shields.io/crates/v/rtsp-backchannel?label=crates.io)](https://crates.io/crates/rtsp-backchannel)

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md)

ONVIF 카메라 검색과 기능 확인, 프로필별 RTSP URI 조회, ONVIF RTSP 백채널을 통한
음원 파일 재생을 지원하는 Python 라이브러리 및 CLI입니다. 파일 재생에만 별도 설치한
FFmpeg가 필요하며 GStreamer는 사용하지 않습니다.

다른 구현체:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md) — [npm 패키지](https://www.npmjs.com/package/rtsp-backchannel)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md) — [crates.io 패키지](https://crates.io/crates/rtsp-backchannel)

패키지는 백채널 세션을 열고 음원 파일 전체를 실시간 속도로 전송한 뒤 세션을
종료합니다. 입력 음원 디코딩에는 별도로 설치된 `ffmpeg` 실행 파일을 사용하며,
오디오 코덱 처리와 RTP/RTSP 전송은 Python으로 구현되어 있습니다. FFmpeg는 이 패키지에
포함되지 않고 자동으로 설치되지도 않습니다.

## 요구 사항

- Python 3.11 이상
- 파일 재생 시 `PATH`에서 실행할 수 있는 `ffmpeg`
- ONVIF `sendonly` 오디오 백채널을 제공하는 카메라

카메라 검색, 기능 보고서, 스트림 URI 조회에는 FFmpeg가 필요하지 않습니다.

## 설치

PyPI에 게시된 버전을 설치합니다.

```bash
python3 -m pip install 'rtsp-backchannel>=0.3,<0.4'
```

Registry 릴리스 대신 현재 `master` 소스를 설치하려면 다음 명령을 사용합니다.

```bash
python3 -m pip install \
  "git+https://github.com/GagaKor/rtsp-backchannel.git#subdirectory=python"
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

```python
import os

from rtsp_backchannel import play_file

result = play_file(
    host="camera.local",
    user="admin",
    password=os.environ["ONVIF_PASSWORD"],
    file="/absolute/path/to/event.mp3",
    volume=0.05,
)

print(result.packets_sent, result.duration_seconds)
```

`volume`은 `0.0`부터 `1.0`까지 지정할 수 있으며 검증된 기본값은 `0.05`입니다.

## 전체 워크플로

카메라 주소를 알고 있다면 검색을 생략할 수 있습니다. 스트림 조회는 ONVIF Media
Profile을 확인할 때 유용하지만, 현재 `play_file`은 호출자가 선택한 `StreamUri`를
입력받지 않고 첫 번째 프로필을 독립적으로 다시 엽니다.

```python
import os

from rtsp_backchannel import (
    discover_devices,
    get_stream_uris,
    play_file,
)

password = os.environ["ONVIF_PASSWORD"]

devices = discover_devices(timeout=3.0)
if not devices:
    raise RuntimeError("no ONVIF device found")
camera = devices[0]

streams = get_stream_uris(
    host=camera.ip,
    user="admin",
    password=password,
    device_urls=camera.xaddrs,
    timeout=8.0,
)

for stream in streams:
    print(stream.profile_token, stream.profile_name, stream.uri)

result = play_file(
    host=camera.ip,
    user="admin",
    password=password,
    file="/absolute/path/to/event.mp3",
    volume=0.05,
)

print(result.codec, result.packets_sent, result.duration_seconds)
```

## 공개 API

### `discover_devices`

```python
discover_devices(
    *,
    timeout: float = 3.0,
    interfaces: list[str] | None = None,
    cidrs: list[str] | None = None,
    ports: list[int] | None = None,
    concurrency: int = 64,
) -> list[DiscoveredDevice]
```

`cidrs` 없이 호출하면 로컬 IPv4 인터페이스에서 WS-Discovery 검색을 실행합니다.
`interfaces`를 생략하면 호스트 이름 해석과 기본 라우트에서 감지한 주소를 사용합니다.
`interfaces`에는 카메라 주소가 아니라 이 PC의 로컬 주소를 전달합니다.

IPv4 CIDR과 단일 IPv4를 한 배열에 넣으면 지정한 모든 대상을 능동 검색합니다. 겹치는
호스트는 한 번만 확인합니다.

```python
devices = discover_devices(
    cidrs=["10.0.0.0/24", "10.128.0.10"],
    timeout=1.0,
    ports=[80, 8000, 443],
    concurrency=64,
)
```

CIDR 모드는 `/onvif/device_service`에 인증 전 ONVIF
`GetSystemDateAndTime` 요청을 보냅니다. `443`은 자체 서명 인증서를 허용하는
HTTPS로, 나머지는 HTTP로 확인합니다. 기본 포트는 `80`, `8000`, `443`입니다.
한 번에 검색할 수 있는 고유한 사용 가능 IPv4 주소는 최대 4,096개이며
`interfaces`와 `cidrs`는 함께 사용할 수 없습니다.

검색 결과에는 `ip`, `xaddrs`, `scopes`와 선택적인 `name`, `hardware`,
`endpoint_reference`가 있습니다. CIDR 결과의 `xaddrs`에는 응답한 서비스 URL이
들어가지만 검색 메타데이터는 일반적으로 비어 있습니다. 대상 대역으로 라우팅할 수
있어야 하며 방화벽에서 선택한 ONVIF 포트를 허용해야 합니다.

### `get_stream_uris`

```python
get_stream_uris(
    *,
    host: str,
    user: str,
    password: str,
    device_urls: list[str] | None = None,
    timeout: float = 8.0,
) -> list[StreamUri]
```

ONVIF Device 및 Media 서비스에 인증하고 각 Media Profile의 `profile_token`,
선택적인 `profile_name`, `uri`를 반환합니다. 반환되는 RTSP URI에는 인증정보를
삽입하지 않습니다.

### `get_camera_capabilities`

```python
get_camera_capabilities(
    *,
    host: str,
    user: str = "",
    password: str = "",
    device_urls: list[str] | None = None,
    timeout: float = 8.0,
) -> CameraCapabilityReport
```

이 읽기 전용 API는 장치 식별 정보, scope, 광고된 서비스, Media profile, PTZ 사실,
Media2 encoder 근거를 하나의 보고서로 수집합니다. 패키지 root에서
`CameraCapabilityReport`, `CameraCapabilityVersion` 및 중첩 보고서 dataclass를
공개하므로 타입이 있는 결과로 확인할 수 있습니다.

```python
import os

from rtsp_backchannel import get_camera_capabilities

report = get_camera_capabilities(
    host="camera.local",
    user="operator",
    password=os.environ["ONVIF_PASSWORD"],
    device_urls=["http://camera.local/onvif/device_service"],
    timeout=8.0,
)

print(report.declared_profiles, report.media2.h265_supported)
```

다음은 Profile S와 T를 선언하고 PTZ 서비스를 광고하는 카메라에 대해
`capabilities` CLI 명령이 출력하는 JSON입니다. CLI는 한 줄로 출력하며
아래에서는 읽기 쉽도록 줄바꿈했습니다. `pan-node`는
`continuousPanTilt: true`이지만 `zoom-node`는 `absoluteZoom: true`만 가지고
있어, PTZ 서비스
광고만으로 pan/tilt 지원이 보장되지 않는다는 점과 최상위
`ptz.panTiltSupported`/`ptz.zoomSupported`가 두 node를 종합한 값이라는 점을
보여줍니다. 여기의 `declaredProfiles`도 장치 scope에서 얻은 자기 보고일 뿐
독립적인 ONVIF 인증 결과가 아닙니다.

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

보고서 필드의 의미는 다음과 같습니다.

- `device`는 카메라가 보고한 제조사, 모델, 펌웨어, 일련번호이고 `scopes`는 중복을
  제거한 원본 ONVIF scope 값입니다.
- `declared_profiles`는 장치 scope에서 정규화한 profile 이름입니다. 장치의 자기
  보고일 뿐 독립적인 ONVIF 인증 결과가 아닙니다. CLI에서는 `declaredProfiles`로
  표기합니다.
- `service_discovery`는 서비스 목록을 `GetServices`, 기존 `GetCapabilities`
  fallback 중 어디에서 얻었는지 또는 얻지 못했는지를 나타냅니다. `services`에는
  namespace, XAddr, 선택적인 `CameraCapabilityVersion` 정보가 있습니다.
- `profiles`는 Media1/Media2 binding 및 선택적인 PTZ configuration/node token을
  설명합니다. 광고된 PTZ 서비스, `profile_tokens`의 profile binding,
  `pan_tilt_supported`, `zoom_supported`, PTZ node의 실제 movement space는 서로 다른
  사실이며 어느 하나가 나머지를 보장하지 않습니다.
- `media2.detected`는 성공한 `GetServices` 응답이 Media2를 광고했는지만 나타냅니다.
  접근 가능 여부가 아니며 Media2 보강에 실패해도 `true`로 남을 수 있습니다. 기존
  fallback 또는 서비스 검색 실패에서는 `null`입니다. CLI의 `media2.encodings`와
  `media2.h265Supported`는 성공한 encoder option 보강의 근거입니다.
- `warnings`에는 선택적인 보강 요청 실패가 들어갑니다. 각 `warning.message`는
  generic canonical text만 사용하며 credentials, WSSE digest material,
  URL userinfo, raw or real camera response payload를 포함하지 않습니다. 최초 연결 또는
  인증 실패는 치명적이며 예외로 전달되므로 warning으로 바뀌지 않습니다.

3상 boolean에는 의도가 있습니다. `true`는 성공한 응답에서 해당 사실을 찾았다는
뜻이고, `false`는 성공한 응답이 부재를 확인했다는 뜻이며, `null`은 사실을 확인할
수 없었다는 뜻입니다. Python dataclass에서는 각각 `True`, `False`, `None`이고 CLI는
JSON 표기를 출력합니다. 장치가 보고하지 않은 선택적인 JSON object member는
생략합니다. Media2 광고와 성공한 H.265 보강은 유용한 Profile T 근거이지만 Profile T
인증의 증명은 아닙니다.

서비스 검색에 성공하면 선택적인 Media, PTZ 보강 요청은 각각 일치하는 광고된
서비스 XAddr로 routing됩니다. 반환된 서비스 URL은 WSSE 생성이나 네트워크 I/O 전에
동일 출처 규칙으로 검증합니다. 스킴과 정규화한 호스트 이름은 선택된 Device 서비스와
같아야 하며 port, path, query는 달라도 됩니다. 다른 출처의 XAddr도 `services`에는
그대로 남지만 보강 요청은 보내지 않고 `invalid ONVIF service URL` warning을 기록합니다.
연결 과정에서 반환된 Media XAddr에도 같은 규칙을 적용합니다.

XML은 encoding-aware DTD/entity 차단을 유지하며 element 깊이는 최대 64단계입니다.
SOAP fault는 `ActionNotSupported`를 포함한 고정 인증/프로토콜 allowlist만 출력하며
알 수 없는 code는 항상 `SOAP Fault: Fault`로 정규화합니다.

`timeout`은 요청마다 적용되므로 여러 요청을 수행하는 보고서의 전체 시간은 한 timeout
구간보다 길 수 있습니다.

### `open_ptz_session`

```python
open_ptz_session(options: PtzSessionOptions) -> PtzSession

PtzSessionOptions(
    host: str,
    user: str = "",
    password: str = "",
    profile_token: str | None = None,
    device_urls: list[str] | None = None,
    timeout: float = 8.0,
    default_move_timeout_ms: float = 1000.0,
)
```

`open_ptz_session`은 카메라 한 대에 대한 PTZ 제어 세션을 엽니다. 먼저 연결한
뒤 `GetServices`와 `GetNodes`를 실행해 PTZ 서비스와 그 node를 찾고, Media
Profile token을 정합니다. `profile_token`을 직접 지정하지 않으면 PTZ를
지원하는 첫 번째 profile을 사용합니다. 이어서 해당 node가 지원하는 PTZ
space를 캐시해 두어, 이후의 모든 호출을 카메라가 실제로 광고한 기능과
대조해 검사합니다. 반환되는 `PtzSession`은 `get_camera_capabilities`와
`get_stream_uris`가 사용하는 것과 같은 인증된 transport를 재사용합니다.
PTZ 요청은 새 연결이 아니라 기존 연결 위의 또 다른 SOAP body일 뿐입니다.

`PtzSession`은 `continuous_move`, `absolute_move`, `relative_move`,
`stop`, `get_status`와 `close`를 제공합니다. 각 move 메서드는 카메라의 PTZ
node가 해당 space를 광고하지 않았다면 요청을 보내기 전에 예외를
발생시킵니다. 예를 들어 `continuous_zoom=False`를 보고한 node에서
`continuous_move(zoom=...)`을 호출하면 예외가 발생합니다. Pan/tilt 값과
대부분의 zoom 값은 `-1.0`~`1.0`이며, 절대 zoom *위치*만 `0.0`~`1.0`입니다.
`close()`는 세션을 닫힌 상태로 표시하기 전에 pan/tilt와 zoom 모두에 대해
best-effort로 `stop()`을 호출하므로, 호출자가 종료 시점에 movement 정지를
따로 챙기지 않아도 됩니다.

모든 `continuous_move` 호출에는 카메라로 전달되는 device-side timeout이
포함되며, 기본값은 1000ms입니다. 카메라는 이 timeout이 지나면 스스로
움직임을 멈추므로, 한 번의 호출로는 카메라가 약 1초 동안만 움직입니다.
계속 움직이게 하려면 호출자가 이전 timeout이 끝나기 전에
`continuous_move`를 다시 호출해야 합니다. 이 기본값은
`default_move_timeout_ms`로 제어하며, 호출마다 다른 값을 쓰려면
`timeout_ms`로 재정의할 수 있습니다. 이는 의도된 안전장치입니다.
클라이언트가 멈추라고 지시하지 않아도 카메라가 스스로 정지하므로,
클라이언트가 비정상 종료되거나 연결이 끊겨도 카메라가 계속 움직이는
상태로 남지 않습니다.

비밀번호는 소스 코드에 넣지 말고 `ONVIF_PASSWORD`로 전달하십시오.

```python
import os

from rtsp_backchannel import PtzSessionOptions, PtzVector, open_ptz_session

password = os.environ["ONVIF_PASSWORD"]

session = open_ptz_session(
    PtzSessionOptions(
        host="camera.local",
        user="operator",
        password=password,
        device_urls=["http://camera.local/onvif/device_service"],
        timeout=8.0,
    )
)

try:
    session.continuous_move(pan_tilt=PtzVector(0.5, 0.0), timeout_ms=2000.0)
    status = session.get_status()
    print(status.pan_tilt, status.zoom)
finally:
    session.close()
```

**실험적 기능입니다.** 검증됨: 세션 열기, 기능 지원 확인(guard), 요청 구성,
timeout 포함, close 시 stop 호출. 미검증: 카메라가 의도한 대로 실제로
움직이는지 여부 — 실제 PTZ 하드웨어가 없어 확인하지 못했습니다.

### `play_file`

```python
play_file(
    *,
    host: str,
    user: str,
    password: str,
    file: str,
    volume: float = 0.05,
) -> PlaybackResult
```

`PlaybackResult`에는 `codec`, `sample_rate`, `payload_type`, `rtp_channel`,
`encoded_bytes`, `packets_sent`, `duration_seconds`가 있습니다. 잘못된 인자, 인증 실패,
네트워크 실패, 지원되지 않는 카메라 SDP는 예외로 전달됩니다.

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

하위 호환성을 위해 `play` 단어는 생략할 수 있습니다. `streams`와 재생 명령은 기존의
빈 문자열 인증정보 기본값을 유지합니다. `capabilities`에서 `--pass`를 생략하면
`ONVIF_PASSWORD`를 읽고, 환경변수가 없으면 빈 비밀번호를 사용합니다. 명시적인
`--pass ""`는 환경변수보다 우선하여 빈 비밀번호를 사용합니다.

`capabilities`에는 비어 있지 않은 `--host`가 필요하고 비어 있지 않은 `--user`를
지정할 수 있습니다. 반복된 `--device-url`은 입력 순서를 보존합니다. API를 한 번
호출하고 native camelCase JSON object를 정확히 한 줄 출력합니다. `--timeout-ms`를
생략하면 API 기본값을 사용하고, 지정한 값은 소수도 가능하지만 0보다 큰 유한한
요청별 millisecond 값이어야 하며 24시간 상한(86,400,000ms)을 포함하여 그 이하여야
합니다. 파싱한 millisecond 숫자를 second로 변환하기 전에 검증합니다. 잘못되었거나
상한을 넘는 값은 API 또는 네트워크 호출 전에 값이 포함되지 않은 고정 진단과 종료
상태 2로 거부합니다.

capability CLI는 bare `--` argument terminator를 값이 포함되지 않은 고정 진단으로
거부합니다. `--pass=--value` 또는 `--pass` 다음의 별도 값으로 전달한 hyphen-prefixed
password는 opaque 값으로 유지하고, 알려진 capability flag는 비밀번호 누락으로
처리합니다. 명시적인 `--pass ""`의 환경변수 override 동작은 그대로입니다.

## 재생 동작

- PCMA(G.711 A-law) 8kHz mono
- TCP interleaved RTP
- 40ms 오디오 패킷과 실시간 페이싱
- 긴 음원 재생 중 RTSP keepalive 전송
- 성공 또는 실패 후 RTSP 세션 종료

첫 번째 ONVIF Media Profile이 지원 코덱을 제공하는 `sendonly` 오디오 트랙을 포함해야
합니다. 오디오 출력과 디코더 설정은 카메라마다 다르므로 RTSP 세션이 정상적으로
열려도 카메라의 출력이 비활성화되었거나 잘못 연결되어 있으면 소리가 나지 않을 수
있습니다.

## 인증, RTSP 및 코덱

빈 자격 증명은 ONVIF WS-Security와 RTSP 인증을 생략합니다. 비어 있지 않은 ONVIF
자격 증명은 PasswordDigest를 사용하고 RTSP 인증은 서버 challenge 뒤에 전송합니다.
WS-Security digest는 인증일 뿐 전송 암호화가 아닙니다. 자체 서명 TLS를 포함한
HTTP/HTTPS 호환성을 지원하므로 신뢰할 수 있는 네트워크 또는 VPN을 사용하십시오.

기본 `codec="auto"` SDP 협상 순서는 PCMA, PCMU, G726-32, G726-24, G726-16, G726-40,
AAC입니다. G711, RFC3551 G726, RFC 3640 MPEG4-GENERIC AAC-hbr을 지원하며 MP4A-LATM은
명시적으로 지원하지 않습니다. 코덱을 지정하면 다른 코덱으로 대체하지 않습니다.

직접 RTSP는 ONVIF를 우회합니다.

```python
result = play_file(
    host="rtsp://admin:p%40ss@camera.local/backchannel",
    user="", password="", file="/absolute/path/to/event.mp3", codec="auto",
)
```

내장 자격 증명은 자동 파싱되고 비어 있지 않은 명시적 인자가 우선합니다. 비밀번호의
`@`는 `%40`으로 쓰는 것을 권장하며 raw `@`는 authority의 마지막 구분자를 사용합니다.
요청 URI와 로그에서는 자격 증명이 제거됩니다.

```bash
# 자격 증명 없음
rtsp-backchannel play --host camera.local --file '/absolute/path/to/event.mp3'
# 직접 RTSP
rtsp-backchannel play --host 'rtsp://admin:p%40ss@camera.local/backchannel' \
  --file '/absolute/path/to/event.mp3'
```

## 개발

저장소 루트에서 실행합니다.

```bash
PYTHONPATH=python:. python3 -m unittest discover -s python -p 'test_*.py'
python3 -m build python
python3 -m twine check python/dist/*
```

버전 변경과 Registry 배포 절차는
[RELEASING.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/RELEASING.md)에
정리되어 있습니다.

## 라이선스

사용자가 선택할 수 있는
[MIT](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/LICENSE-MIT) 또는
[Apache-2.0](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/LICENSE-APACHE)
라이선스로 배포합니다.

이 패키지는 FFmpeg를 포함하거나 링크하지 않습니다. 애플리케이션에서 FFmpeg를 함께
번들하거나 재배포한다면 해당 FFmpeg 빌드의 라이선스 조건을 별도로 확인해야 합니다.
[FFmpeg Legal](https://ffmpeg.org/legal.html)과
[THIRD_PARTY_NOTICES.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/THIRD_PARTY_NOTICES.md)를
참고하십시오.

ONVIF는 ONVIF, Inc.의 상표입니다. 이 프로젝트는 ONVIF, Inc.와 독립적으로
개발되었고 제휴 또는 보증을 받지 않았으며 ONVIF Profile 적합성을 주장하지 않습니다.
