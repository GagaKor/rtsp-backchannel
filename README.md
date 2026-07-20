# RTSP Backchannel for TypeScript

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.ko.md)

TypeScript library and CLI for discovering ONVIF cameras, resolving profile
RTSP URIs, and playing one audio file through an ONVIF RTSP backchannel.
GStreamer is not required.

Other implementations:

- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md)
- [Rust](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md)

The package starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. G.711 encoding and RTP/RTSP transport are
implemented in TypeScript. FFmpeg is not bundled or installed by this package.

## Requirements

- Node.js 22 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` audio backchannel

Discovery and stream URI lookup do not require FFmpeg.

## Installation

```bash
npm install rtsp-backchannel
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

const password = process.env.ONVIF_PASSWORD;
if (!password) throw new Error('ONVIF_PASSWORD is required');

const packetsSent = await playFile({
  host: 'camera.local',
  user: 'admin',
  pass: password,
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
| `discoverDevices(options?)` | `timeoutMs?`, `interfaces?: string[]` | `Promise<DiscoveredDevice[]>` |
| `getStreamUris(options)` | `host`, `user`, `pass`, `deviceUrls?`, `timeoutMs?` | `Promise<StreamUri[]>` |
| `playFile(options)` | `host`, `user`, `pass`, `file`, `volume` | RTP packet count as `Promise<number>` |

`DiscoveredDevice` contains `ip`, `xaddrs`, `scopes`, and optional `name`,
`hardware`, and `endpointReference` fields. `StreamUri` contains
`profileToken`, optional `profileName`, and a `uri` without embedded
credentials.

`discoverDevices` uses WS-Discovery multicast. It normally must run from an
IPv4 interface on the same subnet or VLAN as the camera. Pass every local IPv4
address in `interfaces` when a host has multiple NICs or VLANs that must be
searched.

`getStreamUris` authenticates with the ONVIF Device and Media services and
returns the RTSP URI for every Media Profile. Network, authentication, and
protocol errors reject the returned promise.

### Low-Level Backchannel API

Use `openBackchannel` when the session lifecycle or encoded G.711 buffer must
be controlled directly. Always close the session, including after an error.

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

The package also exports `pcm16ToG711`, `linearToALaw`, `linearToMuLaw`,
`generateTonePcm`, and `sendPacedG711` for applications that generate PCM or
control encoding and pacing themselves.

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

# Resolve RTSP URIs for all ONVIF Media Profiles.
rtsp-backchannel streams \
  --host camera.local \
  --user admin

# Play one file and close the RTSP session.
rtsp-backchannel play \
  --host camera.local \
  --user admin \
  --file '/absolute/path/to/event.mp3' \
  --volume 0.05
```

The `play` word is optional for backward compatibility. `--pass` is available
for manual use, but `ONVIF_PASSWORD` avoids exposing the password in the
process argument list.

## Playback Behavior

- G.711 at 8 kHz mono
- PCMA preferred, with PCMU fallback when only PCMU is offered
- TCP interleaved RTP
- 40 ms audio packets with real-time pacing
- RTSP keepalive during long files
- RTSP teardown after success or failure

The first ONVIF Media Profile must expose a `sendonly` audio track offering
PCMA or PCMU. Audio output and decoder configuration are camera-specific; a
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
