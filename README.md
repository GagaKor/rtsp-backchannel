# RTSP Backchannel for TypeScript

[![latest GitHub Release](https://img.shields.io/github/v/release/GagaKor/rtsp-backchannel?label=release)](https://github.com/GagaKor/rtsp-backchannel/releases/latest) [![rtsp-backchannel on npm](https://img.shields.io/npm/v/rtsp-backchannel?label=npm)](https://www.npmjs.com/package/rtsp-backchannel) [![rtsp-backchannel on PyPI](https://img.shields.io/pypi/v/rtsp-backchannel?label=PyPI)](https://pypi.org/project/rtsp-backchannel/) [![rtsp-backchannel on crates.io](https://img.shields.io/crates/v/rtsp-backchannel?label=crates.io)](https://crates.io/crates/rtsp-backchannel)

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)

ONVIF RTSP backchannel libraries and CLI tools for TypeScript, Python, and Rust.

Supports ONVIF camera discovery, RTSP URI resolution, camera capability inspection,
PTZ control, and two-way audio backchannel streaming with G.711, G.726, and AAC.

Other implementations:

- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md) — [PyPI](https://pypi.org/project/rtsp-backchannel/)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md) — [crates.io](https://crates.io/crates/rtsp-backchannel)

The package starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. Audio codec handling and RTP/RTSP transport
are implemented in TypeScript. FFmpeg is not bundled or installed by this package.

## Requirements

- Node.js 22.12 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` audio backchannel

Discovery and stream URI lookup do not require FFmpeg.

## Installation

```bash
npm install rtsp-backchannel
```

To pin the current release line:

```bash
npm install rtsp-backchannel@^0.3
```

To install the current `master` source instead of a registry release:

```bash
npm install "github:GagaKor/rtsp-backchannel"
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

## Module Formats

The package ships a single ES module build and is reachable from both module
systems.

```typescript
// ES modules
import { playFile } from 'rtsp-backchannel';
```

```javascript
// CommonJS
const { playFile } = require('rtsp-backchannel');
```

`require()` works because Node loads an ES module synchronously from CommonJS
from 22.12.0 onward, which is why that is the minimum version. Both entry
points resolve to the same file, so a process never holds two copies of the
library and its state.

### Silencing MODULE_TYPELESS_PACKAGE_JSON

Running an `import` statement from a `.js` file produces this warning:

```
[MODULE_TYPELESS_PACKAGE_JSON] Warning: Module type of file:///.../use.js is not
specified and it doesn't parse as CommonJS. Reparsing as ES module because module
syntax was detected. This incurs a performance overhead.
```

The warning is about the file being run, not about this package: Node found no
`"type"` field in the nearest `package.json`, guessed CommonJS, failed, and
reparsed. Any one of these resolves it:

- add `"type": "module"` to your own `package.json`
- rename the file to `.mjs`
- use `require()` instead

## Quick Playback

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

`volume` must be between `0.0` and `1.0`. The tested default is `0.05`.

## Complete Workflow

Discovery is optional when the camera address is already known. Stream lookup
is useful for inspecting ONVIF Media Profiles, but `playFile` currently opens
the first profile independently and does not accept a `StreamUri` selected by
the caller.

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

## Public API

| API | Main options | Result |
| --- | --- | --- |
| `discoverDevices(options?)` | `timeoutMs?`, `interfaces?`, `cidrs?`, `ports?`, `concurrency?` | `Promise<DiscoveredDevice[]>` |
| `getStreamUris(options)` | `host`, `user`, `pass`, `deviceUrls?`, `timeoutMs?` | `Promise<StreamUri[]>` |
| `getCameraCapabilities(options)` | `host`, `user?`, `pass?`, `deviceUrls?`, `timeoutMs?` | `Promise<CameraCapabilityReport>` |
| `openPtzSession(options)` | `host`, `user?`, `pass?`, `profileToken?`, `deviceUrls?`, `timeoutMs?`, `defaultMoveTimeoutMs?` | `Promise<PtzSession>` (experimental) |
| `playFile(options)` | `host`, `user`, `pass`, `file`, `volume`, `codec` | RTP packet count as `Promise<number>` |

`DiscoveredDevice` contains `ip`, `xaddrs`, `scopes`, and optional `name`,
`hardware`, and `endpointReference` fields. `StreamUri` contains
`profileToken`, optional `profileName`, and a `uri` without embedded
credentials.

### Device Discovery

Calling `discoverDevices()` without `cidrs` uses WS-Discovery multicast from
the machine's detected local IPv4 interfaces. This is the default for cameras
on the same subnet or VLAN. `interfaces` is an advanced override containing
local addresses of this computer, not camera addresses.

To search routed networks or specific addresses, pass an array whose entries
are either IPv4 CIDRs or individual IPv4 addresses. Every entry is searched and
overlapping hosts are probed once:

```typescript
const devices = await discoverDevices({
  cidrs: ['10.0.0.0/24', '10.128.0.10'],
  timeoutMs: 1000,
  ports: [80, 8000, 443],
  concurrency: 64,
});
```

CIDR mode sends the unauthenticated ONVIF `GetSystemDateAndTime` request to
`/onvif/device_service`. Port `443` uses HTTPS and accepts self-signed camera
certificates; other ports use HTTP. The default ports are `80`, `8000`, and
`443`, and the default concurrency is `64`. A maximum of 4,096 unique usable
IPv4 hosts can be searched per call. `interfaces` and `cidrs` cannot be used
together.

Active CIDR results contain the successful service URLs in `xaddrs`; `scopes`,
`name`, and `hardware` are unavailable unless the device also answers multicast
discovery. The target networks must be routable and their ONVIF ports must be
allowed by host and network firewalls. If the camera IP is already known,
discovery can be skipped and the address passed directly to `getStreamUris`.

`getStreamUris` authenticates with the ONVIF Device and Media services and
returns the RTSP URI for every Media Profile. Network, authentication, and
protocol errors reject the returned promise.

Credentials are optional. Empty `user` and `pass` omit WS-Security for ONVIF and
RTSP authentication; with non-empty ONVIF credentials the library uses
PasswordDigest, while RTSP authentication is sent only after a server
challenge. WS-Security digest authenticates the request but does not encrypt
transport. HTTP and HTTPS cameras, including self-signed TLS endpoints, are
supported for compatibility; use a trusted network or VPN.

### Camera Capability Reports

`getCameraCapabilities` collects the device identity, scopes, advertised
services, Media profiles, PTZ facts, and Media2 encoder evidence in one
report. Supply passwords through `ONVIF_PASSWORD` rather than source code:

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

Here is what a report looks like for a camera that declares Profile S and T
support and advertises a PTZ service. The CLI's `capabilities` command prints
this as a single JSON line; it is pretty-printed below for readability. The
two PTZ nodes make the point: `pan-node` reports `continuousPanTilt: true`
while `zoom-node` reports only `absoluteZoom: true`, so an advertised PTZ
service does not by itself mean pan/tilt support, and the top-level
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

The public report fields have these meanings:

- `device` is the reported manufacturer, model, firmware, and serial identity;
  `scopes` preserves the deduplicated raw ONVIF scope values.
- `declaredProfiles` contains normalized profile names from device scopes. These
  are device self-reports, not independent ONVIF certification results.
- `serviceDiscovery` says whether the inventory came from `GetServices`, the
  legacy `GetCapabilities` fallback, or was unavailable. `services` contains
  namespace, XAddr, and optional version facts.
- `profiles` describes Media1/Media2 profile bindings, including audio presence
  and optional PTZ configuration/node tokens.
- `ptz` separates three different facts: an advertised PTZ service
  (`detected`), profile-to-PTZ bindings (`profileTokens`), and actual movement
  spaces (`panTiltSupported`, `zoomSupported`, and `nodes`). One does not imply
  the others.
- `media2.detected` reports only whether a successful `GetServices` response advertised a Media2 service.
  It is not a reachability result and can remain `true` when Media2 enrichment
  requests fail. It is `null` after the legacy `GetCapabilities` fallback or
  unavailable service discovery. `media2.encodings` and
  `media2.h265Supported` come from encoder-option enrichment when available.
- `warnings` contains failures from optional enrichment requests. Each
  `warning.message` uses generic canonical text and contains no credentials,
  WSSE digest material, URL userinfo, or raw or real camera response payload.
  Initial connection and authentication failures are fatal; they reject the
  promise instead of becoming warnings.

Authenticated service routing is anchored to the selected Device Service URL.
The connected Media endpoint and every advertised Media1, Media2, or PTZ
XAddr must use the same scheme and canonical hostname or IP address. Ports,
paths, and query strings may differ. Camera-reported XAddr values remain in
`services` as evidence, but a mismatched endpoint receives neither WS-Security
material nor a network request; optional enrichment records a generic warning
and leaves the corresponding evidence empty or unknown.

ONVIF response headers are limited to 64 KiB and response bodies/XML input to
1 MiB. Parsed XML is limited to 64 element levels. Exceeding an optional
enrichment budget leaves its result empty/unknown and adds a credential-safe
warning. SOAP faults expose only canonical authentication and protocol codes;
unknown camera codes become `Fault` without payload reflection.

Tri-state booleans are deliberate: `true` means a successful response found the
fact, `false` means a successful response established its absence, and `null`
means the fact could not be established. Optional object members are omitted
when the device did not report them. In particular, a legacy
`GetCapabilities`-only result leaves `media2.detected` and
`media2.h265Supported` as `null`. A Media2 service advertisement and successful
H.265 option enrichment are useful evidence, but are not proof of Profile T
certification.

`timeoutMs` is a per-request timeout. Capability reporting performs multiple
requests, so total elapsed time can exceed one timeout interval. Optional
enrichment failures can add warnings and continue; they do not extend a single
request's timeout.

### PTZ Control

`openPtzSession` opens a control session for one camera: it connects, then
runs `GetServices` and `GetNodes` to find the PTZ service and its node,
resolves a Media Profile token (the first PTZ-capable profile unless
`profileToken` is given explicitly), and caches the node's supported PTZ
spaces so every later call can be checked against what the camera actually
advertised. The returned `PtzSession` reuses the same authenticated
transport `getCameraCapabilities` and `getStreamUris` use; PTZ requests are a
different SOAP body on the existing connection, not a new one.

`PtzSession` exposes `continuousMove`, `absoluteMove`, `relativeMove`,
`stop`, and `getStatus`, plus `close`. Each move method rejects before
sending any request if the camera's PTZ node did not advertise the
corresponding space — for example, `continuousMove({ zoom: ... })` against a
node reporting `continuousZoom: false`. Pan/tilt values and most zoom
quantities are `-1.0`..`1.0`; an absolute zoom *position* is `0.0`..`1.0`.
`close()` makes a best-effort `stop()` call for both pan/tilt and zoom before
marking the session closed, so a caller does not have to remember to stop
movement on the way out.

Every `continuousMove` call carries a device-side timeout, defaulting to
1000 ms, that is sent to the camera as part of the request. The camera is
responsible for halting the movement itself once that timeout elapses, so a
single call moves the camera for only about a second; a caller that wants
continuous motion must keep re-issuing `continuousMove` before the previous
timeout runs out. `defaultMoveTimeoutMs` controls this default (a per-call
`timeoutMs` overrides it for one call). This is a deliberate safety
property: the camera stops on its own, so a crashed or disconnected client
can never leave it moving indefinitely.

Supply passwords through `ONVIF_PASSWORD` rather than source code:

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

**Experimental.** Verified: session open, capability guarding, request
construction, timeout inclusion, and stop-on-close. Unverified: that a
camera physically moves as intended — no PTZ hardware was available.

### Low-Level Backchannel API

Use `openBackchannel` when the session lifecycle or encoded RTP frames must be
controlled directly. Always close the session, including after an error.

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

`withKeepAlive` prevents a short RTSP session from expiring while FFmpeg reads
and encodes the file. `session.send` continues keepalive handling during paced
RTP transmission.

The package also exports `pcm16ToG711`, `linearToALaw`, `linearToMuLaw`,
`generateTonePcm`, and `sendPacedG711` for applications that generate PCM or
control encoding and pacing themselves.

`session.variant` is `G711Variant | undefined`; it is `undefined` when SDP
selects G.726 or AAC. Use `fileToRtpAudio`/`sendPacedFrames` for codec-neutral
playback instead of assuming a G.711 variant.

To bypass ONVIF entirely, pass a direct RTSP target. Embedded credentials are
parsed automatically, and explicit non-empty `user`/`pass` override them:

```typescript
const packetsSent = await playFile({
  host: 'rtsp://admin:p%40ss@camera.local:554/backchannel',
  user: '',
  pass: '',
  file: '/absolute/path/to/event.mp3',
  codec: 'auto',
});
```

Prefer `%40` for a password containing `@`. A raw `@` is interpreted using the
final `@` in the authority. Request URIs and displayed errors strip embedded
credentials.

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
rtsp-backchannel play \
  --host 'rtsp://admin:p%40ss@camera.local/backchannel' \
  --file '/absolute/path/to/event.mp3'
```

The `play` word is optional for backward compatibility. `--pass` is available
for manual use, but `ONVIF_PASSWORD` avoids exposing the password in the
process argument list. `capabilities` accepts repeatable `--device-url` values
in the supplied order and prints exactly one native camelCase JSON report.
Omitting `--timeout-ms` uses the client default; when supplied, it must be a
finite number greater than zero and no greater than 86,400,000 milliseconds
(24 hours), inclusively. Capability argument validation uses fixed diagnostics
that do not echo option values or credentials, rejects a bare `--`, and accepts
hyphen-leading passwords through `--pass <value>` when they are not known
flags or through the unambiguous `--pass=<value>` form. An explicit empty
password remains distinct from an omitted password; prefer `ONVIF_PASSWORD` so
secrets do not appear in the process argument list.

## Playback Behavior

- SDP auto negotiation, in this order: PCMA, PCMU, G726-32, G726-24,
  G726-16, G726-40, AAC
- Supports G711, RFC3551 G726, and RFC 3640 MPEG4-GENERIC AAC-hbr
- MP4A-LATM is explicitly unsupported
- Use `codec`/`--codec` to request one supported codec; explicit selection does
  not fall back to another codec
- TCP interleaved RTP
- 40 ms audio packets with real-time pacing
- RTSP keepalive during long files
- RTSP teardown after success or failure

The first ONVIF Media Profile must expose a `sendonly` audio track offering a
supported codec. Audio output and decoder configuration are camera-specific; a
successful RTSP session does not override disabled or misrouted camera audio
output settings.

## Development

```bash
npm install
npm run build
npm test
npm run typecheck
```

Release preparation and registry publishing are documented in
[RELEASING.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/RELEASING.md).

## License

Licensed under either
[MIT](https://github.com/GagaKor/rtsp-backchannel/blob/master/LICENSE-MIT) or
[Apache-2.0](https://github.com/GagaKor/rtsp-backchannel/blob/master/LICENSE-APACHE),
at your option.

This package does not include or link FFmpeg. If an application bundles or
redistributes FFmpeg, review the license terms of that FFmpeg build separately.
See [FFmpeg Legal](https://ffmpeg.org/legal.html) and
[THIRD_PARTY_NOTICES.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/THIRD_PARTY_NOTICES.md).

ONVIF is a trademark of ONVIF, Inc. This independent project is not affiliated
with or endorsed by ONVIF, Inc. and does not claim ONVIF Profile conformance.
