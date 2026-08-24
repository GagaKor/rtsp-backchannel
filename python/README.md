# RTSP Backchannel for Python

[![latest GitHub Release](https://img.shields.io/github/v/release/GagaKor/rtsp-backchannel?label=release)](https://github.com/GagaKor/rtsp-backchannel/releases/latest) [![rtsp-backchannel on npm](https://img.shields.io/npm/v/rtsp-backchannel?label=npm)](https://www.npmjs.com/package/rtsp-backchannel) [![rtsp-backchannel on PyPI](https://img.shields.io/pypi/v/rtsp-backchannel?label=PyPI)](https://pypi.org/project/rtsp-backchannel/) [![rtsp-backchannel on crates.io](https://img.shields.io/crates/v/rtsp-backchannel?label=crates.io)](https://crates.io/crates/rtsp-backchannel)

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.ko.md)

Python library and CLI for discovering and inspecting ONVIF cameras, resolving
profile RTSP URIs, and playing one audio file through an ONVIF RTSP
backchannel. FFmpeg is required only for file playback; GStreamer is not used.

Other implementations:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md) — [npm](https://www.npmjs.com/package/rtsp-backchannel)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md) — [crates.io](https://crates.io/crates/rtsp-backchannel)

The package starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. Audio codec handling and RTP/RTSP transport
are implemented in Python. FFmpeg is not bundled or installed by this package.

## Requirements

- Python 3.11 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` audio backchannel

Discovery, capability reporting, and stream URI lookup do not require FFmpeg.

## Installation

Install a released version from PyPI:

```bash
python3 -m pip install 'rtsp-backchannel>=0.3,<0.4'
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

This read-only API collects device identity, scopes, advertised services,
Media profiles, PTZ facts, and Media2 encoder evidence. The package root
exports `CameraCapabilityReport`, `CameraCapabilityVersion`, and the other
nested report dataclasses for typed inspection.

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

The following is the JSON the `capabilities` CLI command prints for a camera
that declares Profile S and T support and advertises a PTZ service; it is
pretty-printed here, though the CLI writes one line. The two PTZ nodes make
the point: `pan-node` reports `continuousPanTilt: true` while `zoom-node`
reports only `absoluteZoom: true`, so an advertised PTZ service does not by
itself mean pan/tilt support, and the top-level
`ptz.panTiltSupported`/`ptz.zoomSupported` summarize across both nodes.
`declaredProfiles` here is a self-report drawn from the device's own scopes,
not an ONVIF certification.

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

The report fields have these meanings:

- `device` contains the reported manufacturer, model, firmware, and serial;
  `scopes` preserves the deduplicated raw ONVIF scope values.
- `declared_profiles` contains normalized profile names from device scopes.
  These are device self-reports, not independent ONVIF certification results.
  The CLI spells this field `declaredProfiles`.
- `service_discovery` records whether inventory came from `GetServices`, the
  legacy `GetCapabilities` fallback, or was unavailable. `services` contains
  namespace, XAddr, and optional `CameraCapabilityVersion` facts.
- `profiles` describes Media1/Media2 bindings and optional PTZ configuration
  and node tokens. A reported PTZ service, profile bindings in
  `profile_tokens`, and movement spaces represented by `pan_tilt_supported`,
  `zoom_supported`, and PTZ nodes are separate facts; one does not imply the
  others.
- `media2.detected` says only whether a successful `GetServices` response
  advertised Media2. It is not a reachability result and can remain `true`
  when Media2 enrichment fails. It is `null` after a legacy fallback or
  unavailable discovery. The CLI fields
  `media2.encodings` and `media2.h265Supported` contain encoder-option evidence
  when available.
- `warnings` contains failures from optional enrichment operations. Each
  `warning.message` uses generic canonical text and contains no credentials,
  WSSE digest material, URL userinfo, or raw or real camera response payload.
  Initial connection and authentication failures are fatal; they raise an
  exception instead of becoming warnings.

Tri-state booleans are intentional: `true` means a successful response found
the fact, `false` means a successful response established its absence, and
`null` means the fact could not be established. The Python dataclasses use
`True`, `False`, and `None`; the CLI emits their JSON forms. Optional JSON
object members are omitted when the device did not report them. A Media2
advertisement and successful H.265 enrichment are useful Profile T evidence,
not proof of Profile T certification.

Successful service discovery routes each optional Media and PTZ enrichment
request to the matching advertised service XAddr. Returned service
URLs are subject to a same-origin rule before WSSE generation or network I/O:
their scheme and canonical hostname must match the selected Device service;
ports, paths, and queries may differ. A cross-origin XAddr remains in
`services`, but its enrichment is skipped with an `invalid ONVIF service URL`
warning. The connected Media XAddr is validated by the same rule.

XML keeps the encoding-aware DTD/entity rejection and permits at most 64
element levels. SOAP fault output uses a fixed authentication/protocol
allowlist, including `ActionNotSupported`; every unknown code is reported
only as `SOAP Fault: Fault`.

`timeout` applies per request; because one report performs multiple requests,
its total elapsed time can exceed one timeout interval.

This package's `get_camera_capabilities` does not include the TypeScript
package's default audio-send probe described in the root
[README.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md#camera-capability-reports):
there is no `probe_audio_send` option, no `audio_send` field on
`CameraCapabilityReport`, and no extra ONVIF or VIGI OpenAPI round trip
performed here. This call's cost and behavior are unchanged.

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

`open_ptz_session` opens a control session for one camera: it connects, then
runs `GetServices` and `GetNodes` to find the PTZ service and its node,
resolves a Media Profile token (the first PTZ-capable profile unless
`profile_token` is given explicitly), and caches the node's supported PTZ
spaces so every later call can be checked against what the camera actually
advertised. The returned `PtzSession` reuses the same authenticated
transport `get_camera_capabilities` and `get_stream_uris` use; PTZ requests
are a different SOAP body on the existing connection, not a new one.

`PtzSession` exposes `continuous_move`, `absolute_move`, `relative_move`,
`stop`, and `get_status`, plus `close`. Each move method raises before
sending any request if the camera's PTZ node did not advertise the
corresponding space — for example, `continuous_move(zoom=...)` against a
node reporting `continuous_zoom=False`. Pan/tilt values and most zoom
quantities are `-1.0`..`1.0`; an absolute zoom *position* is `0.0`..`1.0`.
`close()` makes a best-effort `stop()` call for both pan/tilt and zoom before
marking the session closed, so a caller does not have to remember to stop
movement on the way out.

Every `continuous_move` call carries a device-side timeout, defaulting to
1000 ms, that is sent to the camera as part of the request. The camera is
responsible for halting the movement itself once that timeout elapses, so a
single call moves the camera for only about a second; a caller that wants
continuous motion must keep re-issuing `continuous_move` before the previous
timeout runs out. `default_move_timeout_ms` controls this default (a
per-call `timeout_ms` overrides it for one call). This is a deliberate
safety property: the camera stops on its own, so a crashed or disconnected
client can never leave it moving indefinitely.

Supply passwords through `ONVIF_PASSWORD` rather than source code:

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

**Experimental.** Physical movement is now verified, against one camera: a
TP-Link VIGI C540V (firmware 2.2.0 and 2.3.3), where `relativeMove`,
`continuousMove`,
and `absoluteMove` each moved the camera in the requested direction on both
pan/tilt and zoom, `getStatus` tracked every move, and an `absoluteMove` back
to the starting coordinates restored them exactly. Session open, capability
guarding, request construction, timeout inclusion, and stop-on-close remain
covered by tests. Unverified beyond that one model — no camera with optical
zoom (the C540V's is digital) or with mechanical preset tours has been
exercised. That camera also rejects any sub-second `Timeout`, so
`continuousMove` with `timeoutMs` under 1000 fails on it; whole seconds are
sent as `PT1S` rather than `PT1.000S` precisely because it rejects the
decimal point.

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

### Audio Send Transports

The TypeScript package in this repository added a second audio-send
transport alongside the ONVIF backchannel: TP-Link's VIGI OpenAPI `talk`
protocol, selected with `transport: 'auto' | 'onvif' | 'vigi'` (`--transport`
on its CLI). It targets cameras that have a working speaker but no working
ONVIF backchannel — confirmed, for example, on a TP-Link VIGI C540V whose
ONVIF answers a backchannel `DESCRIBE` with a receive-only track and reports
no audio output configuration, yet whose VIGI OpenAPI speaker plays audio
normally. `'auto'` tries ONVIF first and only falls back to VIGI when the
camera's SDP has no sendonly track; every other failure still propagates.
See the root
[README.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md#audio-send-transports)
for the full details, including the OpenAPI setup step and its G.711-only,
model-dependent nature.

This Python package does not implement that transport. `play_file` and the
lower-level RTSP backchannel support here speak ONVIF backchannel only —
there is no `transport` argument and no VIGI fallback. A camera whose only
working audio-send path is VIGI OpenAPI cannot be reached from this package
today.

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

# Print one camelCase camera capability report as one JSON line.
rtsp-backchannel capabilities \
  --host camera.local \
  --user operator \
  --device-url http://camera.local/onvif/device_service \
  --timeout-ms 8000

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

The `play` word is optional for backward compatibility. `streams` and playback
retain their empty-string credential defaults. For `capabilities`, omitting
`--pass` reads `ONVIF_PASSWORD` and uses an empty password if that variable is
unset. An explicit `--pass ""` overrides the environment with an empty
password.

`capabilities` requires a non-empty `--host`, accepts a non-empty `--user`, and
preserves repeatable `--device-url` values in supplied order. It calls the API
once and prints exactly one native camelCase JSON object. Omitting
`--timeout-ms` uses the API default; a supplied value may be decimal but must
be finite and greater than zero and no greater than the inclusive 24-hour
maximum (86,400,000 ms). The parsed millisecond number is validated before it
is converted to seconds. Invalid or excessive values exit with status 2 and a
fixed value-free diagnostic before API or network dispatch.

The capability CLI rejects a bare `--` argument terminator with a fixed
value-free diagnostic. Hyphen-prefixed passwords remain opaque when supplied
as `--pass=--value` or as the separate value to `--pass`; known capability
flags are still treated as a missing password. The explicit `--pass ""`
environment override is unchanged.

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
