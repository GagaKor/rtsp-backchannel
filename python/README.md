# RTSP Backchannel for Python

Discover ONVIF cameras, resolve profile RTSP URIs, and play event audio through
an ONVIF RTSP backchannel without GStreamer.

The package uses a separately installed `ffmpeg` executable to decode input
audio. PCMA encoding and RTP/RTSP transport are implemented by the package.

## Requirements

- Python 3.11 or later
- `ffmpeg` on `PATH` for audio playback

Discovery and stream URI lookup do not require FFmpeg. The package does not
bundle or install the FFmpeg executable.

## Installation

Install a released version from PyPI:

```bash
python3 -m pip install rtsp-backchannel
```

Install the latest `master` source before the first registry release:

```bash
python3 -m pip install \
  "git+https://github.com/GagaKor/rtsp-backchannel.git#subdirectory=python"
```

## Complete workflow

The usual flow is discovery, stream lookup, then one-shot playback. If the
camera address is already known, discovery can be skipped.

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

print(result.packets_sent, result.duration_seconds)
```

`play_file` opens the first ONVIF Media Profile, requires that profile to expose
a `sendonly` audio backchannel, sends the file once, and closes the RTSP
session. It resolves that profile independently and does not accept a
`StreamUri` selected from `get_stream_uris`. The current playback profile is
PCMA 8 kHz mono over TCP interleaved RTP with 40 ms packets. `volume` accepts
values from `0.0` to `1.0`; the tested default is `0.05`.

## Public API

### `discover_devices`

```python
discover_devices(
    *,
    timeout: float = 3.0,
    interfaces: list[str] | None = None,
) -> list[DiscoveredDevice]
```

Searches selected local IPv4 interfaces with WS-Discovery. Omitting
`interfaces` uses addresses detected from hostname resolution and the default
route. Pass every local IPv4 address explicitly when multiple NICs or VLANs
must be covered. Each result contains `ip`, `xaddrs`, `scopes`, and optional
`name`, `hardware`, and `endpoint_reference` fields. Discovery normally needs
to run on the same subnet or VLAN as the camera because WS-Discovery multicast
is not routed.

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

Authenticates to the ONVIF Device and Media services and returns every Media
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
) -> PlaybackResult
```

`PlaybackResult` contains `codec`, `sample_rate`, `payload_type`, `rtp_channel`,
`encoded_bytes`, `packets_sent`, and `duration_seconds`. Invalid arguments,
authentication failures, network failures, and unsupported camera SDP are
reported as exceptions.

## Command line

On Bash or zsh, read `ONVIF_PASSWORD` without echoing it or putting its value in
shell history:

```bash
# Set the password once for the following commands.
printf 'Camera password: '
read -rs ONVIF_PASSWORD
printf '\n'
export ONVIF_PASSWORD

# Discover cameras. Output is one JSON object per line.
rtsp-backchannel discover --timeout-ms 3000

# Select interfaces explicitly when the host has multiple NICs or VLANs.
rtsp-backchannel discover \
  --interface 192.0.2.20 \
  --interface 198.51.100.20

# Resolve RTSP URIs for all ONVIF Media Profiles.
rtsp-backchannel streams \
  --host camera.local \
  --user admin

# Play one file and close the session.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

See the [project README](https://github.com/GagaKor/rtsp-backchannel) for the
TypeScript and Rust APIs, protocol details, troubleshooting, and source-based
development commands.

ONVIF is a trademark of ONVIF, Inc. This independent project is not affiliated
with or endorsed by ONVIF, Inc. and does not claim ONVIF Profile conformance.
