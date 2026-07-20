# RTSP Backchannel for Rust

[English](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.md) |
[한국어](https://github.com/GagaKor/rtsp-backchannel/blob/master/rust/README.ko.md)

Rust library and CLI for discovering ONVIF cameras, resolving profile RTSP
URIs, and playing one audio file through an ONVIF RTSP backchannel. GStreamer
is not required.

Other implementations:

- [TypeScript](https://github.com/GagaKor/rtsp-backchannel/blob/master/README.md)
- [Python](https://github.com/GagaKor/rtsp-backchannel/blob/master/python/README.md)

The crate starts a backchannel session, sends the complete file at real-time
speed, and closes the session. It calls a separately installed `ffmpeg`
executable to decode input audio. G.711 encoding and RTP/RTSP transport are
implemented in Rust. FFmpeg is not bundled or installed by this crate.

## Requirements

- Rust 1.86 or later
- `ffmpeg` on `PATH` for file playback
- A camera that exposes an ONVIF `sendonly` G.711 audio backchannel

Discovery and stream URI lookup do not require FFmpeg.

## Installation

Add the released crate to `Cargo.toml`. The example application also uses
`anyhow` to simplify error propagation.

```toml
[dependencies]
anyhow = "1"
rtsp-backchannel = "0.1"
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
        user: "admin".to_owned(),
        password: std::env::var("ONVIF_PASSWORD")?,
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
        result.variant, result.packets_sent, result.duration_seconds
    );
    Ok(())
}
```

## Public API

| API | Main options | Result |
| --- | --- | --- |
| `discover_devices(&options)` | `DiscoveryOptions { timeout, interfaces }` | `Vec<DiscoveredDevice>` |
| `get_stream_uris(&options)` | `StreamUriOptions` | `Result<Vec<StreamUri>, String>` |
| `play_file(&config)` | `PlaybackConfig` | `anyhow::Result<PlaybackResult>` |

`DiscoveredDevice` contains `ip`, `xaddrs`, `scopes`, and optional `name`,
`hardware`, and `endpoint_reference` fields. `DiscoveryOptions::default()`
searches for three seconds using an automatically selected local IPv4 address.
Set `interfaces` explicitly when multiple NICs or VLANs must be covered.

WS-Discovery multicast is normally not routed, so discovery must run from the
same subnet or VLAN as the camera.

`StreamUriOptions::new(host, user, password)` uses an eight-second timeout and
the standard ONVIF Device service URL candidates. Set `device_urls` from a
discovery result when the camera advertises a specific endpoint. Each
`StreamUri` contains `profile_token`, optional `profile_name`, and a `uri`
without embedded credentials.

`PlaybackResult` contains `variant`, `sample_rate`, `payload_type`,
`rtp_channel`, `encoded_bytes`, `packets_sent`, and `duration_seconds`.

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
