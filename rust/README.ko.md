# Rust용 RTSP Backchannel

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md)

ONVIF 카메라 검색, 프로필별 RTSP URI 조회, ONVIF RTSP 백채널을 통한 음원 파일
재생을 지원하는 Rust 라이브러리 및 CLI입니다. 파일 재생에만 별도 설치한 FFmpeg가
필요하며 GStreamer는 사용하지 않습니다.

다른 구현체:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)
- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md)

crate는 백채널 세션을 열고 음원 파일 전체를 실시간 속도로 전송한 뒤 세션을
종료합니다. 입력 음원 디코딩에는 별도로 설치된 `ffmpeg` 실행 파일을 사용하며,
오디오 코덱 처리와 RTP/RTSP 전송은 Rust로 구현되어 있습니다. FFmpeg는 이 crate에
포함되지 않고 자동으로 설치되지도 않습니다.

## 요구 사항

- Rust 1.86 이상
- 파일 재생 시 `PATH`에서 실행할 수 있는 `ffmpeg`
- ONVIF `sendonly` 오디오 백채널을 제공하는 카메라

카메라 검색과 스트림 URI 조회에는 FFmpeg가 필요하지 않습니다.

## 설치

릴리스된 crate를 `Cargo.toml`에 추가합니다. 아래 예제에서는 오류 전달을 단순화하기
위해 `anyhow`도 사용합니다.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = "0.2"
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
| `get_stream_uris(&options)` | `StreamUriOptions` | `Result<Vec<StreamUri>, String>` |
| `play_file(&config)` | `PlaybackConfig` | `anyhow::Result<PlaybackResult>` |

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
