# RTSP Backchannel

ONVIF Device/Media 서비스의 일부 기능을 사용해 카메라를 검색하고, 프로필별 RTSP
URI를 조회하고, RTSP 백채널로 음원을 재생하는 Python/TypeScript/Rust 라이브러리 및
CLI입니다. GStreamer는 사용하지 않습니다. FFmpeg는 입력 파일을 mono 8kHz PCM으로
디코딩할 때만 사용하고, G.711 인코딩과 RTP/RTSP 전송은 각 언어의 코드가 처리합니다.

## 준비

- Python 3.11 이상
- Node.js 22 이상과 npm (TypeScript 사용 시)
- Rust 1.86 이상과 Cargo (Rust 사용 시)
- FFmpeg (`ffmpeg` 명령이 `PATH`에 있어야 함; 패키지에 포함되거나 자동 설치되지 않음)

macOS에서는 다음 명령으로 FFmpeg를 설치할 수 있습니다.

```bash
brew install ffmpeg
```

Ubuntu/Debian에서는 다음 명령을 사용합니다.

```bash
sudo apt-get update
sudo apt-get install ffmpeg
```

Windows에서는 [FFmpeg 다운로드 페이지](https://ffmpeg.org/download.html)에서 빌드를
설치한 뒤 `ffmpeg.exe`가 있는 디렉터리를 `PATH`에 추가합니다.

## 설치와 빠른 재생

npm, PyPI, crates.io 패키지 이름은 모두 `rtsp-backchannel`입니다. 릴리스 버전은 각
Registry에서 설치하고, 아직 게시되지 않은 버전이나 최신 소스는 GitHub의 `master`
브랜치에서 설치할 수 있습니다.

각 API는 음원 한 파일을 끝까지 전송하고 RTSP 세션을 종료합니다. 입력 파일 디코딩을
위해 실행 환경의 `PATH`에 `ffmpeg`가 있어야 합니다.

### TypeScript / npm

```bash
npm install rtsp-backchannel

# 최신 master 소스에서 설치
npm install "github:GagaKor/rtsp-backchannel"
```

```typescript
import { playFile } from 'rtsp-backchannel';

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const packetsSent = await playFile({
  host: 'camera.local',
  user: 'admin',
  pass: password,
  file: '/absolute/path/to/audio.mp3',
  volume: 0.05,
});

console.log({ packetsSent });
```

### Python / PyPI

```bash
python3 -m pip install rtsp-backchannel

# 최신 master 소스에서 설치
python3 -m pip install \
  "git+https://github.com/GagaKor/rtsp-backchannel.git#subdirectory=python"
```

```python
import os

from rtsp_backchannel import play_file

result = play_file(
    host="camera.local",
    user="admin",
    password=os.environ["ONVIF_PASSWORD"],
    file="/absolute/path/to/audio.mp3",
    volume=0.05,
)

print(result.packets_sent)
```

### Rust / crates.io

릴리스 버전을 사용할 `Cargo.toml` 설정은 다음과 같습니다.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = "0.1"
```

최신 `master` 소스를 사용하려면 다음처럼 지정합니다.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = { git = "https://github.com/GagaKor/rtsp-backchannel.git", branch = "master" }
```

```rust
use std::path::PathBuf;

use rtsp_backchannel::playback::{PlaybackConfig, play_file};

fn main() -> anyhow::Result<()> {
    let result = play_file(&PlaybackConfig {
        host: "camera.local".to_owned(),
        user: "admin".to_owned(),
        password: std::env::var("ONVIF_PASSWORD")?,
        file: PathBuf::from("/absolute/path/to/audio.mp3"),
        volume: 0.05,
    })?;

    println!("{} RTP packets", result.packets_sent);
    Ok(())
}
```

위 Rust 예제처럼 `anyhow::Result`를 사용하려면 소비자 프로젝트에 `anyhow = "1"`도
추가합니다.

## 라이브러리 기능 사용법

세 구현의 권장 호출 순서는 같습니다.

1. `discover` API로 같은 서브넷의 카메라를 찾습니다. 주소를 이미 알면 생략할 수 있습니다.
2. `streams` API로 Media Profile별 RTSP URI를 조회합니다.
3. `play` API로 음원 한 파일을 전송합니다. 재생이 끝나면 RTSP 세션이 종료됩니다.

`streams` 결과는 프로필 확인이나 별도 RTSP 클라이언트에 사용할 수 있습니다. 현재 고수준
`play` API는 선택한 `StreamUri`를 입력받지 않고, 카메라의 첫 번째 Media Profile URI를
독립적으로 다시 조회합니다. 첫 프로필에 `sendonly` 오디오 백채널이 없으면 재생은
오류로 종료됩니다.

장치 검색과 URI 조회에는 FFmpeg가 필요하지 않습니다. 파일 재생에서만 실행 환경의
`ffmpeg`를 사용합니다. 비밀번호는 환경변수나 secret manager에서 읽어 API에 별도로
전달하고, RTSP URI에 삽입하지 않는 방식을 권장합니다.

현재 고수준 재생 API는 G.711 8kHz mono, TCP interleaved RTP, 40ms 패킷 프로필을
사용합니다. Python은 PCMA를 요구하고, TypeScript와 Rust는 PCMA를 우선하되 카메라가
PCMU만 제공하면 PCMU를 사용합니다. `volume`은 `0.0`부터 `1.0`까지이며 검증된 권장값은
`0.05`입니다.

### TypeScript 전체 워크플로

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

주요 공개 API:

| API | 주요 입력 | 반환값 |
| --- | --- | --- |
| `discoverDevices(options?)` | `timeoutMs?`, `interfaces?: string[]` | `Promise<DiscoveredDevice[]>` |
| `getStreamUris(options)` | `host`, `user`, `pass`, `deviceUrls?`, `timeoutMs?` | `Promise<StreamUri[]>` |
| `playFile(options)` | `host`, `user`, `pass`, `file`, `volume` | 전송한 RTP 패킷 수 `Promise<number>` |

`DiscoveredDevice`에는 `ip`, `xaddrs`, `scopes`, 선택적인 `name`, `hardware`,
`endpointReference`가 있습니다. `StreamUri`에는 `profileToken`, 선택적인 `profileName`,
인증정보가 삽입되지 않은 `uri`가 있습니다. 네트워크 및 프로토콜 오류는 Promise
rejection으로 반환됩니다.

세션을 직접 열어 이미 인코딩한 G.711 버퍼를 보내려면 저수준 API를 사용할 수 있습니다.
세션은 오류가 발생해도 반드시 닫아야 합니다.

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

`pcm16ToG711`, `linearToALaw`, `linearToMuLaw`, `generateTonePcm`,
`sendPacedG711`도 공개되어 있어 PCM 생성이나 인코딩을 직접 제어할 수 있습니다.

### Python 전체 워크플로

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

주요 공개 API:

| API | 주요 입력 | 반환값 |
| --- | --- | --- |
| `discover_devices(...)` | `timeout=3.0`, `interfaces=None` | `list[DiscoveredDevice]` |
| `get_stream_uris(...)` | `host`, `user`, `password`, `device_urls=None`, `timeout=8.0` | `list[StreamUri]` |
| `play_file(...)` | `host`, `user`, `password`, `file`, `volume=0.05` | `PlaybackResult` |

`PlaybackResult`에는 `codec`, `sample_rate`, `payload_type`, `rtp_channel`,
`encoded_bytes`, `packets_sent`, `duration_seconds`가 있습니다. 검색 결과가 없으면 빈
리스트를 반환하고, 잘못된 인자나 네트워크·프로토콜 오류는 예외로 반환합니다. 다중
NIC/VLAN을 빠짐없이 검색해야 하면 `interfaces`에 각 로컬 IPv4 주소를 명시합니다.

### Rust 전체 워크플로

예제에서 오류 전달을 단순화하려면 소비자 `Cargo.toml`에 `anyhow = "1"`을 함께
추가합니다.

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
        result.variant, result.packets_sent, result.duration_seconds
    );
    Ok(())
}
```

주요 공개 API:

| API | 주요 입력 | 반환값 |
| --- | --- | --- |
| `discover_devices(&options)` | `DiscoveryOptions { timeout, interfaces }` | `Vec<DiscoveredDevice>` |
| `get_stream_uris(&options)` | `StreamUriOptions` | `Result<Vec<StreamUri>, String>` |
| `play_file(&config)` | `PlaybackConfig` | `anyhow::Result<PlaybackResult>` |

`PlaybackResult`에는 `variant`, `sample_rate`, `payload_type`, `rtp_channel`,
`encoded_bytes`, `packets_sent`, `duration_seconds`가 있습니다. `DiscoveryOptions::default()`는
3초 동안 자동 선택한 로컬 IPv4 주소에서 검색합니다. 다중 NIC/VLAN에서는
`DiscoveryOptions.interfaces`에 각 주소를 명시합니다.

## CLI 사용법

설치된 세 패키지는 동일한 CLI 명령을 제공합니다. `discover`는 같은 네트워크의 ONVIF
장치를 WS-Discovery로 찾고, `streams`는 인증 후 모든 Media Profile의 RTSP URI를
반환합니다. 출력은 결과 하나당 JSON 한 줄입니다.

```bash
# 같은 서브넷의 ONVIF 장치 검색
rtsp-backchannel discover --timeout-ms 3000

# 여러 NIC/VLAN에서 검색할 때 IPv4 인터페이스를 반복 지정
rtsp-backchannel discover \
  --interface 192.0.2.20 \
  --interface 198.51.100.20

# Bash/zsh에서 비밀번호를 화면과 셸 히스토리에 남기지 않고 설정
printf 'Camera password: '
read -rs ONVIF_PASSWORD
printf '\n'
export ONVIF_PASSWORD

# 카메라의 모든 profile token/name/RTSP URI 조회
rtsp-backchannel streams \
  --host camera.local \
  --user admin

# 음원 한 파일 재생 후 세션 종료
rtsp-backchannel \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

저장소에서 직접 실행할 때도 앞에서 설정한 `ONVIF_PASSWORD`를 사용해 언어별로 다음
명령을 실행합니다.

```bash
# TypeScript
npm run play -- discover --timeout-ms 3000
npm run play -- streams --host camera.local --user admin

# Python
PYTHONPATH=python python3 -m rtsp_backchannel.cli discover --timeout-ms 3000
PYTHONPATH=python python3 -m rtsp_backchannel.cli streams \
  --host camera.local --user admin

# Rust
cargo run --release --manifest-path rust/Cargo.toml -- discover --timeout-ms 3000
cargo run --release \
  --manifest-path rust/Cargo.toml -- streams --host camera.local --user admin
```

RTSP URI는 SOAP 응답값 그대로 반환하며 사용자명이나 비밀번호를 URI에 삽입하지 않습니다.
RTSP 클라이언트에는 URI와 인증정보를 각각 전달합니다. WS-Discovery multicast는 라우터를
통과하지 않으므로 카메라와 같은 서브넷/VLAN에 연결된 IPv4 인터페이스에서 실행해야 합니다.

## Python으로 한 번 재생

저장소 루트에서 실행합니다. MP3와 WAV 등 FFmpeg가 디코딩할 수 있는 파일을 사용할 수 있습니다.

```bash
python3 python/onvif_play.py \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/audio.mp3'
```

음원 전송이 끝나면 RTSP 세션을 종료합니다. 이벤트마다 위 명령을 한 번 실행하면 됩니다.

현재 기본값은 SM-DM-4M2W에서 정상 재생을 확인한 다음 프로필입니다.

- PCMA(G.711 A-law), mono 8kHz
- TCP interleaved RTP
- 패킷 간격 40ms
- Python A-law 인코더 (`python-alaw`)
- rebase pacer, sender RTP identity, 첫 패킷 marker
- 볼륨 0.05, preroll 없음, RTCP sender report 없음

설정을 모두 명시하려면 다음 명령을 사용합니다.

```bash
python3 python/onvif_play.py \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/audio.mp3' \
  --volume 0.05 \
  --encoder python-alaw \
  --codec pcma \
  --sample-rate 8000 \
  --transport tcp \
  --packet-ms 40 \
  --pacer rebase \
  --rtp-identity sender \
  --marker-mode first \
  --preroll-ms 0 \
  --rtcp-interval 0
```

## TypeScript로 한 번 재생

최초 한 번 의존성을 설치합니다.

```bash
npm install
```

다음 명령은 Python과 동일한 0.05 볼륨, G.711 8kHz, TCP, 40ms 패킷,
rebase 페이싱 프로필을 사용합니다. PCMA를 우선하고 카메라가 PCMU만 제공하면
PCMU를 사용합니다.

```bash
npm run play -- \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/audio.mp3' \
  --volume 0.05
```

긴 음원은 협상된 RTSP 세션 timeout의 절반 간격으로 keepalive를 전송합니다.
음원 전송이 끝나거나 변환 오류가 발생하면 RTSP 세션을 종료합니다.

## Rust로 한 번 재생

다음 명령은 순수 Rust Q11/G.711 인코더, 8kHz, TCP, 40ms 패킷, rebase 페이싱
프로필을 사용합니다. PCMA를 우선하고 카메라가 PCMU만 제공하면 PCMU를 사용합니다.
GStreamer는 필요하지 않습니다.

```bash
# 실행 전 서비스 secret/env에 ONVIF_PASSWORD를 설정합니다.
cargo run --release --manifest-path rust/Cargo.toml -- \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/audio.mp3' \
  --volume 0.05
```

Rust 구현도 긴 음원 재생 중 RTSP keepalive를 전송하며, 재생 성공 또는 오류 후
TEARDOWN으로 세션을 정리합니다.
수동 호환을 위한 `--pass` 옵션도 지원하지만, 자동 실행에서는 비밀번호가 프로세스
인자에 남지 않도록 `ONVIF_PASSWORD` 환경변수를 사용합니다.

## 테스트

```bash
PYTHONPATH=python:. python3 -m unittest discover -s python -p 'test_*.py'
npm test
npm run typecheck
cargo test --manifest-path rust/Cargo.toml
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

## 배포

버전 변경, 검증, npm/PyPI/crates.io 게시, Git 태그와 GitHub Release 생성 순서는
[RELEASING.md](RELEASING.md)에 정리되어 있습니다. Registry 자격 증명과 토큰은 저장소에
커밋하지 않습니다.

## 라이선스

이 프로젝트는 사용자가 선택할 수 있는 `MIT OR Apache-2.0` 이중 라이선스로
배포합니다. 자세한 조건은 [LICENSE-MIT](LICENSE-MIT)와
[LICENSE-APACHE](LICENSE-APACHE)를 확인하십시오. 외부 기여도 동일한 이중
라이선스로 제공되며 자세한 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md)에 있습니다.

이 프로젝트는 FFmpeg 소스나 바이너리를 포함하거나 링크하지 않고, 실행 환경에 별도로
설치된 `ffmpeg` 프로세스를 호출합니다. FFmpeg를 이 프로젝트와 함께 번들하거나
재배포하는 경우에는 사용한 FFmpeg 빌드의 LGPL/GPL 및 기타 설정에 따른 조건을 별도로
확인하고 준수해야 합니다. 자세한 내용은 [FFmpeg Legal](https://ffmpeg.org/legal.html)과
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하십시오.

ONVIF는 ONVIF, Inc.의 상표입니다. 이 프로젝트는 ONVIF, Inc.와 독립적으로 개발되었고,
ONVIF, Inc.의 제휴 또는 보증을 받지 않았으며, ONVIF Profile 적합성을 주장하지 않습니다.
