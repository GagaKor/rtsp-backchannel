# RTSP Backchannel for TypeScript

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)

TypeScript library and CLI for discovering ONVIF cameras, resolving profile
RTSP URIs, and playing one audio file through an ONVIF RTSP backchannel.
FFmpeg is required only for file playback; GStreamer is not used.

Other implementations:

- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md)

The package starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. Audio codec handling and RTP/RTSP transport
are implemented in TypeScript. FFmpeg is not bundled or installed by this package.

## Requirements

- Node.js 22 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` audio backchannel

Discovery and stream URI lookup do not require FFmpeg.

## Installation

```bash
npm install rtsp-backchannel
```

The current release line is `0.2`:

```bash
npm install rtsp-backchannel@^0.2
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
process argument list.

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
