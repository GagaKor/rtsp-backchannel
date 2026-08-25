# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CommonJS support for the npm package. `exports` now declares a `require`
  condition alongside `import`, so `require('rtsp-backchannel')` works instead
  of failing with `ERR_PACKAGE_PATH_NOT_EXPORTED`. Both conditions resolve to
  the same ES module build, which Node loads synchronously from CommonJS, so
  there is no second copy of the library or its state in a process.
- A Module Formats section in both TypeScript READMEs covering the ESM and
  CommonJS entry points, and explaining that the `MODULE_TYPELESS_PACKAGE_JSON`
  warning comes from the consumer's own `package.json` missing a `"type"`
  field rather than from this package.
- `exports` gains a `default` condition and a `./package.json` subpath. A
  resolver that requests neither `import` nor `require` — a restricted
  `--conditions` set, or a bundler with its own `conditionNames` — and tooling
  that resolves the manifest by subpath both hit the same
  `ERR_PACKAGE_PATH_NOT_EXPORTED` that the `require` condition was added to
  fix; neither does now.

### Changed

- The npm package's minimum Node.js version stays at `>=22`. `import` works on
  every Node.js 22, and the 22.12.0 floor applies only to `require()`, which is
  documented in both TypeScript READMEs rather than enforced for every
  consumer: narrowing `engines` would fail installs for ESM-only consumers on
  22.0–22.11 under `engine-strict=true` while only *warning* the CommonJS
  callers it targeted, since npm treats `EBADENGINE` as a warning by default.
- Both TypeScript READMEs now state the two limits the single-artifact design
  imposes on CommonJS callers: a CommonJS TypeScript project needs
  `moduleResolution: nodenext` (or `bundler`), because under `node16`
  importing this package from a CommonJS file raises `TS1479`; and `require()`
  returns Node's sealed module namespace, so the exports cannot be replaced by
  `jest.spyOn` or `sinon.stub`.

## [0.3.1] - 2026-08-13

### Changed

- Refresh the packaged READMEs displayed on npm, PyPI, and crates.io with the
  cross-language overview, registry links, and live version badges already
  available in the GitHub repository.

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

[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.1
[0.3.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.0
