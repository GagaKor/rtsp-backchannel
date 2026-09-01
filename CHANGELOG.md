# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-09-01

### Added

- A second audio-send transport in the TypeScript package: TP-Link's VIGI
  OpenAPI `talk` protocol, for cameras that have a speaker but expose no
  ONVIF backchannel. Select it with `transport: 'onvif' | 'vigi' | 'auto'`
  (`--transport` on the CLI); the default `'auto'` tries ONVIF first and
  falls back only when the camera offers no sendonly audio track, so a
  network or authentication fault is never reported as a missing vendor API.
  The Python and Rust packages are unchanged and do not implement this
  transport. Verified end to end on a TP-Link VIGI C540V, firmware 2.3.3
  Build 260713: with `transport` left at its default, `playFile` fell back
  from the absent ONVIF backchannel to VIGI, opened a `pcma/8000 pt=8 ch=0`
  session, and played an audible tone through the camera's speaker.
- `getCameraCapabilities` gains an `audioSend` block naming the transport
  (`'onvif'`, `'vigi'`, or neither) that can reach a given camera. **The ONVIF
  half of this probe runs by default and adds real cost to every call**: it
  opens a second, fully authenticated ONVIF session to issue a real
  backchannel `DESCRIBE` — several extra SOAP round trips plus one full ONVIF
  re-authentication and re-discovery. Pass `probeAudioSend: false` to skip it.
  The VIGI half sends a credential-bearing `doAuth` to a port that counts
  failed attempts toward a device lockout, so it runs only when device
  information identifies TP-Link/VIGI hardware; `probeVigiTalk`
  (`'auto' | 'always' | 'never'`, `--probe-vigi-talk`) overrides that. Any
  fact a probe could not establish stays `null` rather than becoming `false`.
  This addition is TypeScript-only; the Python and Rust capability reports are
  unaffected and perform no such probe.
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

### Fixed

- PTZ sessions now send the mandatory `IncludeCapability` element in their
  `GetServices` request. A strict ONVIF stack answered the previous bare
  `<GetServices/>` with HTTP 400 and a `SOAP-ENV:Sender` Fault, so
  `openPtzSession` / `open_ptz_session` could not open against such a camera at
  all.
- Whole-second PTZ move timeouts are sent as `PT1S` rather than `PT1.000S`.
  Both spell the same `xs:duration`, but a strict stack rejects the decimal
  point with `ter:InvalidArgVal`, which made `continuousMove` fail at every
  timeout value — including the 1000 ms default. The whole-second test is
  applied to the rendered three-decimal text rather than to the raw quotient,
  so a timeout such as 999.9999 ms — which renders as `1.000` — is also sent
  as `PT1S` instead of the rejected spelling. Sub-second timeouts keep the
  fractional form, so a caller's requested duration is never silently changed.
  The minimum timeout is now 1 ms: anything smaller rendered as `PT0.000S`,
  a device-side runaway guard of zero. All three packages.
- A failed VIGI OpenAPI `doAuth` challenge is reported by its error code
  rather than as a malformed reply, so an account locked by the device's retry
  limit says so instead of surfacing as `invalid VIGI doAuth challenge`.
- Downlink audio no longer breaks the VIGI talk handshake. With
  `mode: 'aec'` the camera may push `$`-framed RTP on the same connection
  before the MULTITRANS reply arrives; those bytes were fed to the text parser,
  which found a `CRLFCRLF` inside the audio and failed the open with raw device
  bytes in the error message.

### Changed

- PTZ movement is no longer marked unverified against hardware. Every move
  method is now confirmed on a TP-Link VIGI C540V (firmware 2.2.0 and 2.3.3)
  for both
  pan/tilt and zoom. The feature stays experimental: only that one model has
  been exercised.
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
- Internal structure only, no API change: both backchannel transports now share
  one interleaved-write implementation instead of two copies of the same
  state machine, frame pacing moved to its own module so reaching the VIGI
  transport no longer needs a runtime `import()` to dodge a load-time cycle,
  and the ONVIF backchannel probe gained an injection seam and its first tests.
  `dist/index.d.ts` is unchanged.

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

[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.4.0
[0.3.1]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.1
[0.3.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.0
