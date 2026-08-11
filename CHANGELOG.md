# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Automated version tags and GitHub Releases with native Rust CLI binaries for
  Linux (x64 and ARM64), macOS (Apple Silicon and Intel), and Windows (x64),
  plus live release and package-version badges in every README.

## [0.3.0] - 2026-08-11

### Added

- Experimental ONVIF PTZ movement control in all three packages:
  `openPtzSession` / `open_ptz_session` for continuous, absolute, and relative
  moves plus stop and status. Every continuous move carries a device-side
  timeout so a stopped client cannot leave a camera moving, and operations the
  camera does not advertise are rejected before a request is sent. Physical
  movement is unverified against real PTZ hardware.
- Equivalent TypeScript, Python, and Rust APIs plus a `capabilities` command for
  read-only ONVIF camera capability reports covering device identity, scopes,
  declared profiles, services, media profiles, PTZ, and Media2/H.265 evidence.
- A shared cross-language SOAP fixture that verifies all three implementations
  produce the same deterministic camelCase report.
- `saxes` as the first npm runtime dependency, backing namespace-aware ONVIF XML
  parsing in the TypeScript package. The Python and Rust packages gained no new
  dependencies.

### Fixed

- Narrow `net.Socket` data chunks to `Buffer` before concatenating, so the
  packages build against `@types/node` 26 as well as 22.

### Security

- Validate advertised ONVIF service URLs against the selected Device endpoint
  before adding WS-Security credentials or sending a request, and reject HTTP
  redirects during SOAP operations.
- Bound XML parsing, redact untrusted SOAP fault details, and keep optional
  capability failures in sanitized warnings.
- Fail `connect()` when the Device service rejects `GetCapabilities`, instead of
  ignoring the error and continuing against a guessed Media service URL.

## [0.2.0] - 2026-07-21

### Added

- Negotiated ONVIF backchannel codecs, selecting from the codecs a camera
  offers rather than always sending G.711 A-law.

## [0.1.0] - 2026-07-20

### Added

- TypeScript, Python, and Rust library APIs and command-line interfaces.
- ONVIF device discovery with WS-Discovery.
- ONVIF Media profile and RTSP stream URI lookup.
- One-shot PCMA/G.711 A-law RTSP backchannel playback without GStreamer.
- External FFmpeg decoding for common input audio formats.
- MIT OR Apache-2.0 dual licensing.

[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.0
