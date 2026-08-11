# Rust용 RTSP Backchannel

[![rtsp-backchannel on npm](https://img.shields.io/npm/v/rtsp-backchannel?label=npm)](https://www.npmjs.com/package/rtsp-backchannel) [![rtsp-backchannel on PyPI](https://img.shields.io/pypi/v/rtsp-backchannel?label=PyPI)](https://pypi.org/project/rtsp-backchannel/) [![rtsp-backchannel on crates.io](https://img.shields.io/crates/v/rtsp-backchannel?label=crates.io)](https://crates.io/crates/rtsp-backchannel)

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md)

ONVIF 카메라 검색, 읽기 전용 카메라 기능 근거 보고, 프로필별 RTSP URI 조회,
ONVIF RTSP 백채널을 통한 음원 파일 재생을 지원하는 Rust 라이브러리 및 CLI입니다.
파일 재생에만 별도 설치한 FFmpeg가 필요하며 GStreamer는 사용하지 않습니다.

다른 구현체:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md) — [npm 패키지](https://www.npmjs.com/package/rtsp-backchannel)
- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md) — [PyPI 패키지](https://pypi.org/project/rtsp-backchannel/)

crate는 백채널 세션을 열고 음원 파일 전체를 실시간 속도로 전송한 뒤 세션을
종료합니다. 입력 음원 디코딩에는 별도로 설치된 `ffmpeg` 실행 파일을 사용하며,
오디오 코덱 처리와 RTP/RTSP 전송은 Rust로 구현되어 있습니다. FFmpeg는 이 crate에
포함되지 않고 자동으로 설치되지도 않습니다.

## 요구 사항

- Rust 1.86 이상
- 파일 재생 시 `PATH`에서 실행할 수 있는 `ffmpeg`
- ONVIF `sendonly` 오디오 백채널을 제공하는 카메라

카메라 검색, 기능 보고, 스트림 URI 조회에는 FFmpeg가 필요하지 않습니다.

## 설치

릴리스된 crate를 `Cargo.toml`에 추가합니다. 아래 예제에서는 오류 전달을 단순화하기
위해 `anyhow`도 사용합니다.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = "0.3"
```

Registry 릴리스 대신 현재 `master` 소스를 사용하려면 다음과 같이 지정합니다.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = { git = "https://github.com/GagaKor/rtsp-backchannel.git", branch = "master" }
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

```rust
use std::path::PathBuf;

use rtsp_backchannel::playback::{PlaybackConfig, play_file};

fn main() -> anyhow::Result<()> {
    let result = play_file(&PlaybackConfig {
        host: "camera.local".to_owned(),
        user: "admin".to_owned(),
        password: std::env::var("ONVIF_PASSWORD")?,
        file: PathBuf::from("/absolute/path/to/event.mp3"),
        volume: 0.05,
    })?;

    println!("{} RTP packets", result.packets_sent);
    Ok(())
}
```

`volume`은 `0.0`부터 `1.0`까지 지정할 수 있으며 검증된 기본값은 `0.05`입니다.

## 전체 워크플로

카메라 주소를 알고 있다면 검색을 생략할 수 있습니다. 스트림 조회는 ONVIF Media
Profile을 확인할 때 유용하지만, 현재 `play_file`은 호출자가 선택한 `StreamUri`를
입력받지 않고 첫 번째 프로필을 독립적으로 다시 엽니다.

```rust
use std::path::PathBuf;

use anyhow::{Error, Result, anyhow};
use rtsp_backchannel::discovery::{DiscoveryOptions, discover_devices};
use rtsp_backchannel::onvif::{StreamUriOptions, get_stream_uris};
use rtsp_backchannel::playback::{PlaybackConfig, play_file};

fn main() -> Result<()> {
    let password = std::env::var("ONVIF_PASSWORD")?;

    let devices = discover_devices(&DiscoveryOptions::default());
    let camera = devices
        .first()
        .ok_or_else(|| anyhow!("no ONVIF device found"))?;

    let mut stream_options = StreamUriOptions::new(
        camera.ip.to_string(),
        "admin",
        password.clone(),
    );
    stream_options.device_urls.clone_from(&camera.xaddrs);
    let streams = get_stream_uris(&stream_options).map_err(Error::msg)?;

    for stream in streams {
        println!(
            "{} {:?} {}",
            stream.profile_token, stream.profile_name, stream.uri
        );
    }

    let result = play_file(&PlaybackConfig {
        host: camera.ip.to_string(),
        user: "admin".to_owned(),
        password,
        file: PathBuf::from("/absolute/path/to/event.mp3"),
        volume: 0.05,
    })?;

    println!(
        "{:?} {} packets {:.2}s",
        result.codec, result.packets_sent, result.duration_seconds
    );
    Ok(())
}
```

## 공개 API

| API | 주요 옵션 | 반환값 |
| --- | --- | --- |
| `discover_devices(&options)` | `DiscoveryOptions { timeout, interfaces }` | `Vec<DiscoveredDevice>` |
| `discover_devices_in_cidrs(&options)` | `CidrDiscoveryOptions` | `Result<Vec<DiscoveredDevice>, String>` |
| `get_camera_capabilities(&options)` | `CameraCapabilityOptions` | `Result<CameraCapabilityReport, String>` |
| `get_stream_uris(&options)` | `StreamUriOptions` | `Result<Vec<StreamUri>, String>` |
| `open_ptz_session(&options)` | `PtzSessionOptions` | `Result<PtzSession, String>` (실험적 기능) |
| `play_file(&config)` | `PlaybackConfig` | `anyhow::Result<PlaybackResult>` |

### 카메라 기능 근거

`get_camera_capabilities`, `CameraCapabilityOptions`,
`CameraCapabilityReport`와 보고서의 공개 하위 타입은
`rtsp_backchannel::onvif`에서 export됩니다. 이 작업은 읽기 전용이며 한 번 연결한 뒤
scope와 서비스 인벤토리를 읽고, 사용할 수 있는 Media, PTZ, Media2 근거를
조회합니다.

```rust
use std::time::Duration;

use rtsp_backchannel::onvif::{
    CameraCapabilityOptions, CameraCapabilityReport, get_camera_capabilities,
};

let password = std::env::var("ONVIF_PASSWORD").unwrap_or_default();
let mut options = CameraCapabilityOptions::new("camera.local", "admin", password);
options.device_urls = vec![
    "http://camera.local/onvif/device_service".to_owned(),
];
options.timeout = Duration::from_secs(8);
let report: CameraCapabilityReport = get_camera_capabilities(&options)?;

println!("{:?}", report.declared_profiles);
# Ok::<(), String>(())
```

다음은 Profile S와 T를 선언하고 PTZ 서비스를 광고하는 카메라에 대해
`capabilities` 명령이 출력하는 JSON입니다. CLI는 한 줄로 출력하며 아래에서는
읽기 쉽도록 줄바꿈했습니다. `pan-node`는 `continuousPanTilt: true`이지만
`zoom-node`는 `absoluteZoom: true`만 가지고 있으며, 이는 PTZ 서비스 광고만으로
pan/tilt 지원이 보장되지 않는 이유이자 최상위
`ptz.panTiltSupported`/`ptz.zoomSupported`가 두 node를 종합한 값인 이유입니다.
여기의 `declaredProfiles`도 장치 scope에서 얻은 자기 보고일 뿐 독립적인
ONVIF 인증 결과가 아닙니다.

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

보고서는 인증 판정이 아니라 관측 근거로 해석해야 합니다.

- `scopes`는 카메라의 원본 scope를 보존합니다. `declared_profiles`(JSON
  `declaredProfiles`)는 scope에서 얻은 선언 프로필 자체 보고이며 ONVIF 인증 또는
  Profile T 적합성 주장이 아닙니다.
- `service_discovery`(JSON `serviceDiscovery`)는 인벤토리가 `GetServices`, 기존
  `GetCapabilities` fallback, 또는 어느 쪽에서도 오지 못했는지를 나타냅니다.
  `services`는 광고된 서비스 인벤토리입니다. PTZ 서비스 광고, media profile의
  `profile_tokens` PTZ binding, PTZ service capability,
  `pan_tilt_supported`/`zoom_supported` movement 공간은 서로 다른 사실입니다.
  zoom-only node만으로 pan/tilt 지원이 확인되지는 않습니다.
- tri-state 필드에서 `true`는 성공한 응답으로 지원을 확인한 값, `false`는 성공한
  응답으로 부재를 확인한 값, `null`은 확인할 수 없었던 값입니다. 카메라가 보고하지
  않은 선택적 객체 멤버는 native Rust 직렬화와 camelCase JSON에서 생략됩니다.
- `media2.detected`는 `GetServices` 이후에만 null이 아닌 값입니다. `true`/`false`는
  Media2 광고 여부이고, 기존 `GetCapabilities` fallback 또는 서비스 검색 실패에서는
  `null`입니다. 광고, endpoint reachability, `media2.h265Supported` codec 근거는
  별개의 사실이며 어느 하나도 Profile T를 인증하지 않습니다.
- 선택적 enrichment 실패는 sanitized `warnings`를 추가합니다. 그
  `warning.message`는 generic 값이며 credentials, WSSE digest material, URL userinfo,
  raw response payload를 포함하지 않습니다. 초기 연결 및 인증 실패는 치명적 오류로
  처리하며 warning으로 낮추지 않습니다.
- 공식 ONVIF returned-XAddr 규칙에 따라 광고된 endpoint는 WSSE 생성이나 네트워크
  전송 전에 선택한 Device 요청과 same-origin 검사를 통과해야 합니다. scheme과
  canonical host/IP가 같아야 하지만 서로 다른 포트와 경로는 허용됩니다. 광고된
  `XAddr` 자체는 `services` 보고서에 유지됩니다.
  [ONVIF Core 명세](https://www.onvif.org/specs/)를 참고하십시오.
- XML depth 한도는 64입니다. 선택적 enrichment 요청에서 depth 한도를 넘으면
  sanitized warning을 남기고 해당 근거를 unknown 또는 빈 값으로 두며 나머지
  보고서는 계속할 수 있습니다.
- `timeout`은 per-request 한도입니다. 한 보고서는 여러 요청을 사용하므로 총 소요
  시간은 per-request timeout 한 번보다 길 수 있습니다.

### PTZ 제어

`open_ptz_session`, `PtzSession`, `PtzSessionOptions`, `PtzStatus`,
`PtzVector`는 `rtsp_backchannel::onvif`에서 export됩니다. `open_ptz_session`은
카메라 한 대에 대한 제어 세션을 엽니다. 먼저 연결한 뒤 `GetServices`와
`GetNodes`를 실행해 PTZ 서비스와 그 node를 찾고, Media Profile token을
정합니다. `profile_token`을 직접 지정하지 않으면 PTZ를 지원하는 첫 번째
profile을 사용합니다. 이어서 해당 node가 지원하는 PTZ space를 캐시해 두어,
이후의 모든 호출을 카메라가 실제로 광고한 기능과 대조해 검사합니다. 반환되는
`PtzSession`은 `get_camera_capabilities`와 `get_stream_uris`가 사용하는 것과
같은 인증된 transport를 재사용합니다. PTZ 요청은 새 연결이 아니라 기존 연결
위의 또 다른 SOAP body일 뿐입니다.

`PtzSession`은 `continuous_move`, `absolute_move`, `relative_move`, `stop`,
`get_status`와 `close`를 제공합니다. 각 move 메서드는 카메라의 PTZ node가
해당 space를 광고하지 않았다면 요청을 보내기 전에 오류를 반환합니다. 예를
들어 `continuous_zoom: false`를 보고한 node에서
`continuous_move(None, Some(zoom), None)`을 호출하면 오류가 발생합니다.
Pan/tilt 값과 대부분의 zoom 값은 `-1.0`~`1.0`이며, 절대 zoom *위치*만
`0.0`~`1.0`입니다. `close()`는 세션을 닫힌 상태로 표시하기 전에 pan/tilt와
zoom 모두에 대해 best-effort로 `stop()`을 호출하므로, 호출자가 종료 시점에
movement 정지를 따로 챙기지 않아도 됩니다.

모든 `continuous_move` 호출에는 카메라로 전달되는 device-side timeout이
포함되며, 기본값은 1000ms입니다. 카메라는 이 timeout이 지나면 스스로
움직임을 멈추므로, 한 번의 호출로는 카메라가 약 1초 동안만 움직입니다.
계속 움직이게 하려면 호출자가 이전 timeout이 끝나기 전에
`continuous_move`를 다시 호출해야 합니다. 이 기본값은
`PtzSessionOptions::default_move_timeout_ms`로 제어하며,
`continuous_move`의 `timeout_ms` 인자로 호출마다 재정의할 수 있습니다. 이는
의도된 안전장치입니다. 클라이언트가 멈추라고 지시하지 않아도 카메라가
스스로 정지하므로, 클라이언트가 비정상 종료되거나 연결이 끊겨도 카메라가
계속 움직이는 상태로 남지 않습니다.

```rust
use rtsp_backchannel::onvif::{PtzSessionOptions, PtzVector, open_ptz_session};

let password = std::env::var("ONVIF_PASSWORD").unwrap_or_default();
let mut options = PtzSessionOptions::new("camera.local", "operator", password);
options.device_urls = vec![
    "http://camera.local/onvif/device_service".to_owned(),
];
let mut session = open_ptz_session(&options)?;

// close()는 오류가 발생한 경로에서도 실행됩니다. close()로 카메라 정지를
// 보장하려는 호출자가 중간 호출 실패 때문에 이를 건너뛰면 안 되기 때문입니다.
let result = (|| -> Result<(), String> {
    session.continuous_move(Some(PtzVector { x: 0.5, y: 0.0 }), None, Some(2000.0))?;
    let status = session.get_status()?;
    println!("{:?} {:?}", status.pan_tilt, status.zoom);
    Ok(())
})();
session.close();
result?;
# Ok::<(), String>(())
```

**실험적 기능입니다.** 검증됨: 세션 열기, 기능 지원 확인(guard), 요청 구성,
timeout 포함, close 시 stop 호출. 미검증: 카메라가 의도한 대로 실제로
움직이는지 여부 — 실제 PTZ 하드웨어가 없어 확인하지 못했습니다.

### 장치 검색

`discover_devices(&DiscoveryOptions::default())`는 자동으로 선택한 로컬 IPv4 주소에서
WS-Discovery multicast를 사용합니다. 특정 NIC 또는 VLAN을 검색하려면 카메라 주소가
아니라 이 PC의 로컬 주소를 `interfaces`에 지정합니다.

기존 `DiscoveryOptions` 호환성을 유지하면서 여러 라우팅 대역과 단일 주소를 검색하려면
추가된 CIDR API를 사용합니다.

```rust
use std::time::Duration;

use rtsp_backchannel::discovery::{
    CidrDiscoveryOptions, discover_devices_in_cidrs,
};

let mut options = CidrDiscoveryOptions::new([
    "10.0.0.0/24",
    "10.128.0.10",
]);
options.timeout = Duration::from_secs(1);
options.ports = vec![80, 8000, 443];
options.concurrency = 64;
let devices = discover_devices_in_cidrs(&options)?;
# Ok::<(), String>(())
```

`cidrs` 배열의 모든 항목을 검색하고, 단일 IP는 `/32`로 처리하며 겹치는 호스트는 한
번만 확인합니다. CIDR 모드는 `/onvif/device_service`에 인증 전 ONVIF
`GetSystemDateAndTime` 요청을 보냅니다. `443`은 자체 서명 인증서를 허용하는
HTTPS로, 나머지는 HTTP로 확인합니다. 기본값은 포트 `80`, `8000`, `443`, timeout
1초, 동시성 `64`입니다. 한 번에 고유한 사용 가능 IPv4 주소 최대 4,096개를 검색할
수 있습니다.

`DiscoveredDevice`에는 `ip`, `xaddrs`, `scopes`와 선택적인 `name`, `hardware`,
`endpoint_reference`가 있습니다. CIDR 결과의 `xaddrs`에는 응답한 서비스 URL이
들어가지만 검색 메타데이터는 일반적으로 비어 있습니다. 대상 대역으로 라우팅할 수
있어야 하며 방화벽에서 ONVIF 포트를 허용해야 합니다.

`StreamUriOptions::new(host, user, password)`는 8초 timeout과 표준 ONVIF Device
서비스 URL 후보를 사용합니다. 카메라가 특정 endpoint를 광고한다면 검색 결과의
`xaddrs`를 `device_urls`에 지정합니다. 각 `StreamUri`에는 `profile_token`, 선택적인
`profile_name`, 인증정보가 삽입되지 않은 `uri`가 있습니다.

`PlaybackResult`에는 `codec`, G.711에서만 값이 있는 선택적 `variant`, `sample_rate`,
`channels`, `payload_type`, `rtp_channel`, `encoded_bytes`, `packets_sent`,
`duration_seconds`가 있습니다.

## CLI

crates.io에서 바이너리를 설치합니다.

```bash
cargo install rtsp-backchannel
```

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

# native camelCase 카메라 기능 보고서 하나를 출력합니다.
rtsp-backchannel capabilities \
  --host camera.local \
  --user admin \
  --device-url http://camera.local/onvif/device_service \
  --device-url http://camera.local:8000/vendor/device \
  --timeout-ms 8000

# 음원 한 파일을 재생하고 RTSP 세션을 종료합니다.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

`capabilities` 명령은 상태 줄 없이 camelCase JSON 객체를 정확히 한 줄 출력하고 newline
하나를 덧붙입니다. `--pass`를 생략하면 `ONVIF_PASSWORD`를 사용하며, 명시적인
`--pass ""`는 환경변수를 빈 비밀번호로 덮어씁니다. 비어 있지 않은 `--device-url`을
반복하면 입력 순서대로 시도합니다. `--timeout-ms`는 24시간 상한(86,400,000ms)을
포함하여 그 이하인 유한한 양의 decimal 밀리초를 받아 0이 아닌 per-request
`Duration`으로 변환하며, 생략하면 API 기본값 8초를 유지합니다. 도움말과 입력 검증
실패는 네트워크를 열거나 credentials/environment 값을 출력하지 않습니다.

하위 호환성을 위해 `play` 단어는 생략할 수 있습니다. 수동 실행에서는 `--pass`도
사용할 수 있지만, `ONVIF_PASSWORD`를 사용하면 비밀번호가 프로세스 인자 목록에
노출되지 않습니다.

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

빈 자격 증명은 ONVIF WS-Security와 RTSP 인증을 생략합니다. 비어 있지 않은 ONVIF
자격 증명은 PasswordDigest를 사용하고 RTSP 인증은 서버 challenge 뒤에 전송합니다.
WS-Security digest는 인증일 뿐 전송 암호화가 아닙니다. 자체 서명 TLS를 포함한
HTTP/HTTPS 호환성을 지원하므로 신뢰할 수 있는 네트워크 또는 VPN을 사용하십시오.

기본 `CodecPreference::Auto` SDP 협상 순서는 PCMA, PCMU, G726-32, G726-24, G726-16,
G726-40, AAC입니다. G711, RFC3551 G726, RFC 3640 MPEG4-GENERIC AAC-hbr을 지원하며
MP4A-LATM은 명시적으로 지원하지 않습니다. `play_file_with_codec(&config,
CodecPreference::Aac)`처럼 지정하면 다른 코덱으로 대체하지 않습니다. `variant`는
G.711에서만 값이 있는 선택적 값입니다.

직접 RTSP는 ONVIF를 우회합니다. 내장 자격 증명은 자동 파싱되고 비어 있지 않은 명시적
필드가 우선합니다. 비밀번호의 `@`는 `%40`으로 쓰는 것을 권장하며 raw `@`는 authority의
마지막 구분자를 사용합니다. 요청 URI와 로그에서는 자격 증명이 제거됩니다.

```rust
use std::path::PathBuf;
use rtsp_backchannel::audio::CodecPreference;
use rtsp_backchannel::playback::{PlaybackConfig, play_file_with_codec};

let result = play_file_with_codec(&PlaybackConfig {
    host: "rtsp://admin:p%40ss@camera.local/backchannel".to_owned(),
    user: "".to_owned(), password: "".to_owned(),
    file: PathBuf::from("/absolute/path/to/event.mp3"), volume: 0.05,
}, CodecPreference::Auto)?;
```

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
cargo test --manifest-path rust/Cargo.toml
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo package --manifest-path rust/Cargo.toml
```

버전 변경과 Registry 배포 절차는
[RELEASING.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/RELEASING.md)에
정리되어 있습니다.

## 라이선스

사용자가 선택할 수 있는
[MIT](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/LICENSE-MIT) 또는
[Apache-2.0](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/LICENSE-APACHE)
라이선스로 배포합니다.

이 crate는 FFmpeg를 포함하거나 링크하지 않습니다. 애플리케이션에서 FFmpeg를 함께
번들하거나 재배포한다면 해당 FFmpeg 빌드의 라이선스 조건을 별도로 확인해야 합니다.
[FFmpeg Legal](https://ffmpeg.org/legal.html)과
[THIRD_PARTY_NOTICES.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/THIRD_PARTY_NOTICES.md)를
참고하십시오.

ONVIF는 ONVIF, Inc.의 상표입니다. 이 프로젝트는 ONVIF, Inc.와 독립적으로
개발되었고 제휴 또는 보증을 받지 않았으며 ONVIF Profile 적합성을 주장하지 않습니다.
