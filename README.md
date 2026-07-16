# ONVIF RTSP Audio Backchannel

카메라의 ONVIF RTSP 백채널로 음원 파일을 한 번 전송하고 종료하는 Python/TypeScript/Rust 라이브러리 및 CLI입니다.
GStreamer는 사용하지 않습니다. FFmpeg는 입력 파일을 mono 8kHz PCM으로 디코딩할 때만 사용하고,
PCMA(A-law) 인코딩과 RTP/RTSP 전송은 각 언어의 코드가 처리합니다.

## 준비

- Python 3
- Node.js 22 이상과 npm (TypeScript 사용 시)
- Rust 1.85 이상과 Cargo (Rust 사용 시)
- FFmpeg (`ffmpeg` 명령이 `PATH`에 있어야 함)

macOS에서는 다음 명령으로 FFmpeg를 설치할 수 있습니다.

```bash
brew install ffmpeg
```

## 다른 프로젝트에서 라이브러리로 사용

세 패키지 이름은 모두 `onvif-backchannel`입니다. 아직 npm, PyPI, crates.io에는
게시하지 않았으므로 현재는 아래 Git 설치법을 사용합니다. Registry 게시 후에는 각
예제의 첫 번째 설치 명령을 사용할 수 있습니다.

각 API는 음원 한 파일을 끝까지 전송하고 RTSP 세션을 종료합니다. 입력 파일 디코딩을
위해 실행 환경의 `PATH`에 `ffmpeg`가 있어야 합니다.

### TypeScript / npm

```bash
# Registry 게시 후
npm install onvif-backchannel

# 현재 Git 브랜치에서 설치
npm install "github:GagaKor/onvif-test#onvif-rtsp-two-way-audio"
```

```typescript
import { playFile } from 'onvif-backchannel';

const packetsSent = await playFile({
  host: '10.128.10.141',
  user: 'admin',
  pass: process.env.ONVIF_PASSWORD ?? '',
  file: '/absolute/path/to/audio.mp3',
  volume: 0.05,
});

console.log({ packetsSent });
```

### Python / PyPI

```bash
# Registry 게시 후
python3 -m pip install onvif-backchannel

# 현재 Git 브랜치의 Python 패키지 설치
python3 -m pip install \
  "git+https://github.com/GagaKor/onvif-test.git@onvif-rtsp-two-way-audio#subdirectory=python"
```

```python
import os

from onvif_backchannel import play_file

result = play_file(
    host="10.128.10.141",
    user="admin",
    password=os.environ["ONVIF_PASSWORD"],
    file="/absolute/path/to/audio.mp3",
    volume=0.05,
)

print(result.packets_sent)
```

### Rust / crates.io

Registry 게시 후 사용할 `Cargo.toml` 설정은 다음과 같습니다.

```toml
[dependencies]
onvif-backchannel = "0.1"
```

현재 Git 브랜치를 사용하려면 다음처럼 지정합니다.

```toml
[dependencies]
onvif-backchannel = { git = "https://github.com/GagaKor/onvif-test.git", branch = "onvif-rtsp-two-way-audio" }
```

Private GitHub 저장소에서 Cargo의 기본 Git 클라이언트가 인증 정보를 찾지 못하면 소비자
프로젝트의 `.cargo/config.toml`에 시스템 Git credential helper 사용을 설정합니다.

```toml
[net]
git-fetch-with-cli = true
```

```rust
use std::path::PathBuf;

use onvif_backchannel::playback::{PlaybackConfig, play_file};

fn main() -> anyhow::Result<()> {
    let result = play_file(&PlaybackConfig {
        host: "10.128.10.141".to_owned(),
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

## Python으로 한 번 재생

저장소 루트에서 실행합니다. MP3와 WAV 등 FFmpeg가 디코딩할 수 있는 파일을 사용할 수 있습니다.

```bash
python3 python/onvif_play.py \
  --host 10.128.10.141 \
  --user admin \
  --pass '<CAMERA_PASSWORD>' \
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
  --host 10.128.10.141 \
  --user admin \
  --pass '<CAMERA_PASSWORD>' \
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

다음 명령은 Python과 동일한 0.05 볼륨, PCMA 8kHz, TCP, 40ms 패킷,
rebase 페이싱 프로필을 사용합니다.

```bash
npm run play -- \
  --host 10.128.10.141 \
  --user admin \
  --pass '<CAMERA_PASSWORD>' \
  --file '/absolute/path/to/audio.mp3' \
  --volume 0.05
```

긴 음원은 협상된 RTSP 세션 timeout의 절반 간격으로 keepalive를 전송합니다.
음원 전송이 끝나거나 변환 오류가 발생하면 RTSP 세션을 종료합니다.

## Rust로 한 번 재생

다음 명령은 다른 구현과 동일한 순수 Rust Q11/A-law 인코더, PCMA 8kHz,
TCP, 40ms 패킷, rebase 페이싱 프로필을 사용합니다. GStreamer는 필요하지 않습니다.

```bash
# 실행 전 서비스 secret/env에 ONVIF_PASSWORD를 설정합니다.
cargo run --release --manifest-path rust/Cargo.toml -- \
  --host 10.128.10.141 \
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

## Registry 게시

Registry 게시 작업은 각 서비스의 배포 권한과 로그인이 필요하므로 자동으로 실행하지
않습니다. 배포자는 전체 테스트를 통과시킨 뒤 저장소 루트에서 다음 명령을 실행합니다.
crates.io 게시 전에는 프로젝트 소유자가 라이선스를 결정하고 `rust/Cargo.toml`에
`license` 또는 `license-file`을 반드시 추가해야 합니다. 현재는 임의의 라이선스를
부여하지 않았으므로 Cargo 패키징은 가능하지만 crates.io 업로드는 거부됩니다.

```bash
# npm
npm publish

# PyPI (사전에 build와 twine 설치 필요)
(cd python && python3 -m build)
python3 -m twine upload python/dist/*

# crates.io
cargo publish --manifest-path rust/Cargo.toml
```
