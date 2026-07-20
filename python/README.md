# RTSP Backchannel for Python

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md)

Python library and CLI for discovering ONVIF cameras, resolving profile RTSP
URIs, and playing one audio file through an ONVIF RTSP backchannel. FFmpeg is
required only for file playback; GStreamer is not used.

Other implementations:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md)

The package starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. Audio codec handling and RTP/RTSP transport
are implemented in Python. FFmpeg is not bundled or installed by this package.

## Requirements

- Python 3.11 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` audio backchannel

Discovery and stream URI lookup do not require FFmpeg.

## Installation

Install a released version from PyPI:

```bash
python3 -m pip install 'rtsp-backchannel>=0.2,<0.3'
```

To install the current `master` source instead of a registry release:

```bash
python3 -m pip install \
  "git+https://github.com/GagaKor/rtsp-backchannel.git#subdirectory=python"
```

Install FFmpeg separately when playback is required:

```bash
# macOS
brew install ffmpeg

# Ubuntu or Debian
sudo apt-get update
sudo apt-get install ffmpeg
```

On Windows, install a build from the
[FFmpeg download page](https://ffmpeg.org/download.html) and add the directory
containing `ffmpeg.exe` to `PATH`.

## Quick Playback

```python
import os

from rtsp_backchannel import play_file

result = play_file(
    host="camera.local",
    user="",
    password="",
    file="/absolute/path/to/event.mp3",
    volume=0.05,
)

print(result.packets_sent, result.duration_seconds)
```

`volume` must be between `0.0` and `1.0`. The tested default is `0.05`.

## Complete Workflow

Discovery is optional when the camera address is already known. Stream lookup
is useful for inspecting ONVIF Media Profiles, but `play_file` currently opens
the first profile independently and does not accept a `StreamUri` selected by
the caller.

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

## Public API

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

Without `cidrs`, this searches local IPv4 interfaces with WS-Discovery.
Omitting `interfaces` uses addresses detected from hostname resolution and the
default route. `interfaces` contains local addresses of this computer, not
camera addresses.

Pass IPv4 CIDRs and individual IPv4 addresses in one array to actively search
every selected target. Overlapping hosts are probed once:

```python
devices = discover_devices(
    cidrs=["10.0.0.0/24", "10.128.0.10"],
    timeout=1.0,
    ports=[80, 8000, 443],
    concurrency=64,
)
```

CIDR mode sends the unauthenticated ONVIF `GetSystemDateAndTime` request to
`/onvif/device_service`. Port `443` uses HTTPS with self-signed certificates
accepted; other ports use HTTP. The default ports are `80`, `8000`, and `443`.
A maximum of 4,096 unique usable IPv4 hosts can be searched per call.
`interfaces` and `cidrs` cannot be combined.

Each result contains `ip`, `xaddrs`, `scopes`, and optional `name`, `hardware`,
and `endpoint_reference` fields. Active CIDR results have successful service
URLs in `xaddrs`, but discovery metadata is normally empty. The networks must
be routable and firewalls must allow the selected ONVIF ports.

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

Authenticates with the ONVIF Device and Media services and returns every Media
Profile's `profile_token`, optional `profile_name`, and `uri`. Credentials are
not inserted into returned RTSP URIs.

### `play_file`

```python
play_file(
    *,
    host: str,
    user: str,
    password: str,
    file: str,
    volume: float = 0.05,
    codec: str = "auto",
) -> PlaybackResult
```

`PlaybackResult` contains `codec`, `sample_rate`, `payload_type`, `rtp_channel`,
`encoded_bytes`, `packets_sent`, and `duration_seconds`. Invalid arguments,
authentication failures, network failures, and unsupported camera SDP are
reported as exceptions.

Empty credentials omit ONVIF WS-Security and RTSP authentication. Non-empty
ONVIF credentials use PasswordDigest; RTSP credentials are sent after a server
challenge. WS-Security digest is authentication, not transport encryption.
HTTP and HTTPS, including self-signed TLS compatibility, are supported; use a
trusted network or VPN.

The default `codec="auto"` negotiates SDP in this order: PCMA, PCMU, G726-32,
G726-24, G726-16, G726-40, AAC. The implementation supports G711, RFC3551
G726, and RFC 3640 MPEG4-GENERIC AAC-hbr. MP4A-LATM is explicitly unsupported.
An explicit codec request does not fall back to another codec.

ONVIF can be bypassed with a direct RTSP target:

```python
result = play_file(
    host="rtsp://admin:p%40ss@camera.local/backchannel",
    user="",
    password="",
    file="/absolute/path/to/event.mp3",
    codec="auto",
)
```

Embedded credentials are parsed automatically; explicit non-empty arguments
override them. Prefer `%40` for `@` in a password. Raw `@` uses the final
authority separator. Request URIs and logs strip credentials.

## CLI

Read the password without echoing it or placing it in shell history:

```bash
printf 'Camera password: '
read -rs ONVIF_PASSWORD
printf '\n'
export ONVIF_PASSWORD
```

Then use the installed command:

```bash
# Discover cameras. Output is one JSON object per line.
rtsp-backchannel discover --timeout-ms 3000

# Search explicit interfaces on a multi-NIC or multi-VLAN host.
rtsp-backchannel discover \
  --interface 192.0.2.20 \
  --interface 198.51.100.20

# Search every host in a CIDR plus one specific IP.
rtsp-backchannel discover \
  --cidr 10.0.0.0/24 \
  --cidr 10.128.0.10 \
  --timeout-ms 1000 \
  --port 80 \
  --port 8000 \
  --concurrency 64

# Resolve RTSP URIs for all ONVIF Media Profiles.
rtsp-backchannel streams \
  --host camera.local \
  --user admin

# Play one file and close the RTSP session.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --pass "$ONVIF_PASSWORD" \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05 \
  --codec auto

# No ONVIF or RTSP credentials.
rtsp-backchannel play --host camera.local --file '/absolute/path/to/event.mp3'

# Direct RTSP bypasses ONVIF.
rtsp-backchannel play --host 'rtsp://admin:p%40ss@camera.local/backchannel' \
  --file '/absolute/path/to/event.mp3'
```

The `play` word is optional for backward compatibility. Python's CLI defaults
`--user` and `--pass` to empty strings; pass `--pass` explicitly when the
camera requires credentials.

## Playback Behavior

- SDP auto negotiation: PCMA, PCMU, G726-32, G726-24, G726-16, G726-40, AAC
- Supports G711, RFC3551 G726, and RFC 3640 MPEG4-GENERIC AAC-hbr
- MP4A-LATM is explicitly unsupported
- TCP interleaved RTP
- 40 ms audio packets with real-time pacing
- RTSP keepalive during long files
- RTSP teardown after success or failure

The first ONVIF Media Profile must expose a `sendonly` supported audio track. Audio
output and decoder configuration are camera-specific; a successful RTSP
session does not override disabled or misrouted camera audio output settings.

## Development

From the repository root:

```bash
PYTHONPATH=python:. python3 -m unittest discover -s python -p 'test_*.py'
python3 -m build python
python3 -m twine check python/dist/*
```

Release preparation and registry publishing are documented in
[RELEASING.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/RELEASING.md).

## License

Licensed under either
[MIT](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/LICENSE-MIT)
or
[Apache-2.0](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/LICENSE-APACHE),
at your option.

This package does not include or link FFmpeg. If an application bundles or
redistributes FFmpeg, review the license terms of that FFmpeg build separately.
See [FFmpeg Legal](https://ffmpeg.org/legal.html) and
[THIRD_PARTY_NOTICES.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/THIRD_PARTY_NOTICES.md).

ONVIF is a trademark of ONVIF, Inc. This independent project is not affiliated
with or endorsed by ONVIF, Inc. and does not claim ONVIF Profile conformance.
