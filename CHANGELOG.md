# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Equivalent TypeScript, Python, and Rust APIs plus a `capabilities` command for
  read-only ONVIF camera capability reports covering device identity, scopes,
  declared profiles, services, media profiles, PTZ, and Media2/H.265 evidence.
- A shared cross-language SOAP fixture that verifies all three implementations
  produce the same deterministic camelCase report.

### Security

- Validate advertised ONVIF service URLs against the selected Device endpoint
  before adding WS-Security credentials or sending a request, and reject HTTP
  redirects during SOAP operations.
- Bound XML parsing, redact untrusted SOAP fault details, and keep optional
  capability failures in sanitized warnings.

## [0.1.0] - Unreleased

### Added

- TypeScript, Python, and Rust library APIs and command-line interfaces.
- ONVIF device discovery with WS-Discovery.
- ONVIF Media profile and RTSP stream URI lookup.
- One-shot PCMA/G.711 A-law RTSP backchannel playback without GStreamer.
- External FFmpeg decoding for common input audio formats.
- MIT OR Apache-2.0 dual licensing.

[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.1.0
