# Python용 RTSP Backchannel

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md)

ONVIF 카메라 검색, 프로필별 RTSP URI 조회, ONVIF RTSP 백채널을 통한 음원 파일
재생을 지원하는 Python 라이브러리 및 CLI입니다. 파일 재생에만 별도 설치한 FFmpeg가
필요하며 GStreamer는 사용하지 않습니다.

다른 구현체:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md)

패키지는 백채널 세션을 열고 음원 파일 전체를 실시간 속도로 전송한 뒤 세션을
종료합니다. 입력 음원 디코딩에는 별도로 설치된 `ffmpeg` 실행 파일을 사용하며,
오디오 코덱 처리와 RTP/RTSP 전송은 Python으로 구현되어 있습니다. FFmpeg는 이 패키지에
포함되지 않고 자동으로 설치되지도 않습니다.

## 요구 사항

- Python 3.11 이상
- 파일 재생 시 `PATH`에서 실행할 수 있는 `ffmpeg`
- ONVIF `sendonly` 오디오 백채널을 제공하는 카메라

카메라 검색과 스트림 URI 조회에는 FFmpeg가 필요하지 않습니다.

## 설치

PyPI에 게시된 버전을 설치합니다.

```bash
python3 -m pip install 'rtsp-backchannel>=0.2,<0.3'
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

# 음원 한 파일을 재생하고 RTSP 세션을 종료합니다.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

하위 호환성을 위해 `play` 단어는 생략할 수 있습니다. Python CLI의 `--user`와
`--pass` 기본값은 빈 문자열이며 카메라 인증이 필요할 때 `--pass`를 명시하십시오.

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
