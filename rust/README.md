# RTSP Backchannel for Rust

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md)

Rust library and CLI for discovering ONVIF cameras, resolving profile RTSP
URIs, and playing one audio file through an ONVIF RTSP backchannel. FFmpeg is
required only for file playback; GStreamer is not used.

Other implementations:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md)
- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md)

The crate starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. Audio codec handling and RTP/RTSP transport
are implemented in Rust. FFmpeg is not bundled or installed by this crate.

## Requirements

- Rust 1.86 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` audio backchannel

Discovery and stream URI lookup do not require FFmpeg.

## Installation

Add the released crate to `Cargo.toml`. The example application also uses
`anyhow` to simplify error propagation.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = "0.2"
```

To use the current `master` source instead of a registry release:

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = { git = "https://github.com/GagaKor/rtsp-backchannel.git", branch = "master" }
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

```rust
use std::path::PathBuf;

use rtsp_backchannel::playback::{PlaybackConfig, play_file};

fn main() -> anyhow::Result<()> {
    let result = play_file(&PlaybackConfig {
        host: "camera.local".to_owned(),
        user: "".to_owned(),
        password: "".to_owned(),
        file: PathBuf::from("/absolute/path/to/event.mp3"),
        volume: 0.05,
    })?;

    println!("{} RTP packets", result.packets_sent);
    Ok(())
}
```

`volume` must be between `0.0` and `1.0`. The tested default is `0.05`.

## Complete Workflow

Discovery is optional when the camera address is already known. Stream lookup
is useful for inspecting ONVIF Media Profiles, but `play_file` currently opens
the first profile independently and does not accept a `StreamUri` selected by
the caller.

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

## Public API

| API | Main options | Result |
| --- | --- | --- |
| `discover_devices(&options)` | `DiscoveryOptions { timeout, interfaces }` | `Vec<DiscoveredDevice>` |
| `discover_devices_in_cidrs(&options)` | `CidrDiscoveryOptions` | `Result<Vec<DiscoveredDevice>, String>` |
| `get_stream_uris(&options)` | `StreamUriOptions` | `Result<Vec<StreamUri>, String>` |
| `play_file(&config)` | `PlaybackConfig` | `anyhow::Result<PlaybackResult>` |

### Device Discovery

`discover_devices(&DiscoveryOptions::default())` uses WS-Discovery multicast
from an automatically selected local IPv4 address. Set `interfaces` to local
addresses of this computer when specific NICs or VLANs must be searched.

Use the additive CIDR API to search multiple routed networks and individual
addresses without changing the existing `DiscoveryOptions` contract:

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

Every entry in `cidrs` is searched, individual IPs are treated as `/32`, and
overlapping hosts are probed once. CIDR mode sends the unauthenticated ONVIF
`GetSystemDateAndTime` request to `/onvif/device_service`. Port `443` uses
HTTPS with self-signed certificates accepted; other ports use HTTP. The
defaults are ports `80`, `8000`, and `443`, a one-second timeout, and
concurrency `64`. A maximum of 4,096 unique usable IPv4 hosts can be searched.

`DiscoveredDevice` contains `ip`, `xaddrs`, `scopes`, and optional `name`,
`hardware`, and `endpoint_reference` fields. Active CIDR results contain the
successful service URLs in `xaddrs`, but discovery metadata is normally empty.
The networks must be routable and firewalls must allow the ONVIF ports.

`StreamUriOptions::new(host, user, password)` uses an eight-second timeout and
the standard ONVIF Device service URL candidates. Set `device_urls` from a
discovery result when the camera advertises a specific endpoint. Each
`StreamUri` contains `profile_token`, optional `profile_name`, and a `uri`
without embedded credentials.

`PlaybackResult` contains `codec`, an optional G.711-only `variant`, `sample_rate`,
`channels`, `payload_type`, `rtp_channel`, `encoded_bytes`, `packets_sent`, and
`duration_seconds`.

Empty credentials omit ONVIF WS-Security and RTSP authentication. Non-empty
ONVIF credentials use PasswordDigest; RTSP credentials are sent after a server
challenge. WS-Security digest authenticates but does not encrypt transport.
HTTP and HTTPS, including self-signed TLS compatibility, are supported; use a
trusted network or VPN.

The default `CodecPreference::Auto` negotiates SDP in this order: PCMA, PCMU,
G726-32, G726-24, G726-16, G726-40, AAC. G711, RFC3551 G726, and RFC 3640
MPEG4-GENERIC AAC-hbr are supported; MP4A-LATM is explicitly unsupported.
Use `play_file_with_codec(&config, CodecPreference::Aac)` to request one
codec. Explicit selection does not fall back.

Direct RTSP bypasses ONVIF:

```rust
use std::path::PathBuf;
use rtsp_backchannel::audio::CodecPreference;
use rtsp_backchannel::playback::{PlaybackConfig, play_file_with_codec};

let result = play_file_with_codec(&PlaybackConfig {
    host: "rtsp://admin:p%40ss@camera.local/backchannel".to_owned(),
    user: "".to_owned(),
    password: "".to_owned(),
    file: PathBuf::from("/absolute/path/to/event.mp3"),
    volume: 0.05,
}, CodecPreference::Auto)?;
```

Embedded credentials are parsed automatically; explicit non-empty fields
override them. Prefer `%40` for `@` in a password. Raw `@` uses the final
authority separator. Request URIs and logs strip credentials.

## CLI

Install the binary from crates.io:

```bash
cargo install rtsp-backchannel
```

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

The `play` word is optional for backward compatibility. `--pass` is available
for manual use, but `ONVIF_PASSWORD` avoids exposing the password in the
process argument list.

## Playback Behavior

- SDP auto negotiation: PCMA, PCMU, G726-32, G726-24, G726-16, G726-40, AAC
- Supports G711, RFC3551 G726, and RFC 3640 MPEG4-GENERIC AAC-hbr
- MP4A-LATM is explicitly unsupported
- TCP interleaved RTP
- 40 ms audio packets with real-time pacing
- RTSP keepalive during long files
- RTSP teardown after success or failure

The first ONVIF Media Profile must expose a `sendonly` audio track offering a
supported codec. Audio output and decoder configuration are camera-specific; a
successful RTSP session does not override disabled or misrouted camera audio
output settings.

## Development

From the repository root:

```bash
cargo test --manifest-path rust/Cargo.toml
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo package --manifest-path rust/Cargo.toml
```

Release preparation and registry publishing are documented in
[RELEASING.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/RELEASING.md).

## License

Licensed under either
[MIT](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/LICENSE-MIT)
or
[Apache-2.0](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/LICENSE-APACHE),
at your option.

This crate does not include or link FFmpeg. If an application bundles or
redistributes FFmpeg, review the license terms of that FFmpeg build separately.
See [FFmpeg Legal](https://ffmpeg.org/legal.html) and
[THIRD_PARTY_NOTICES.md](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/THIRD_PARTY_NOTICES.md).

ONVIF is a trademark of ONVIF, Inc. This independent project is not affiliated
with or endorsed by ONVIF, Inc. and does not claim ONVIF Profile conformance.
