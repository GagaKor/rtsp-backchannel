# ONVIF Discovery And Stream URI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ONVIF WS-Discovery device lookup and profile-level RTSP URI resolution to the TypeScript, Python, and Rust libraries without breaking existing one-shot playback usage.

**Architecture:** Each language exposes `discoverDevices`/`discover_devices` and `getStreamUris`/`get_stream_uris` with equivalent result fields. Discovery uses a standards-based `NetworkVideoTransmitter` multicast probe and namespace-insensitive XML parsing; stream lookup reuses each implementation's authenticated ONVIF client and returns every media profile URI without injecting credentials. Existing playback syntax remains valid while `discover` and `streams` CLI dispatch paths provide direct operational access.

**Tech Stack:** Node.js UDP/HTTP built-ins, Python standard library sockets/XML, Rust `std::net` plus existing `roxmltree`/`reqwest`, WS-Discovery, ONVIF Media `GetProfiles` and `GetStreamUri`.

---

### Public contract

- TypeScript `discoverDevices({ timeoutMs = 3000, interfaces? })`, Python `discover_devices(timeout=3.0, interfaces=None)`, and Rust `discover_devices(&DiscoveryOptions)` return equivalent records: `ip`, `xaddrs`, `scopes`, optional `name`, optional `hardware`, and optional endpoint reference.
- TypeScript `getStreamUris({ host, user, pass, deviceUrls?, timeoutMs? })`, Python `get_stream_uris(host=..., user=..., password=..., device_urls=None, timeout=8.0)`, and Rust `get_stream_uris(&StreamUriOptions)` return equivalent records: profile token, optional profile name, and the exact SOAP-provided RTSP URI.
- Passwords are used only in WS-Security/HTTP transport. APIs and CLI never append userinfo to an RTSP URI. Tests use reserved characters in passwords and assert returned URIs remain byte-for-byte unchanged after XML entity decoding.
- `onvif-backchannel discover [--timeout-ms N] [--interface IPv4 ...]` and `onvif-backchannel streams --host H [--user U] [--pass P] [--device-url URL ...]` emit one JSON object per result and exit zero for an empty discovery result. Protocol/authentication failures exit nonzero. Calling the CLI directly with the existing playback flags remains unchanged.
- All implementations probe every explicitly selected IPv4 interface concurrently and collect responses until one shared deadline. Defaults use available non-loopback IPv4 addresses where the standard library exposes them, with the host's multicast route address as a fallback.
- Existing TypeScript playback exports, Python `PlaybackResult`/`play_file`, and all existing Rust public modules remain available unchanged.

### Task 1: TypeScript discovery and streams APIs

**Files:**
- Create: `src/onvif/discovery.ts`
- Create: `src/onvif/discovery.test.ts`
- Create: `src/onvif/streams.ts`
- Create: `src/onvif/streams.test.ts`
- Modify: `src/discover.ts`
- Modify: `src/streams.ts`
- Modify: `src/index.ts`
- Modify: `src/index.test.ts`
- Modify: `src/cli.ts`
- Modify: `src/cli.test.ts`

- [x] Write failing tests for namespace-insensitive ProbeMatch parsing, deduplication, shared-deadline interface probing, exact all-profile URI lookup with reserved-character credentials, public exports, JSON Lines CLI output, and backward-compatible CLI dispatch.
- [x] Run focused Node tests and confirm the new modules and exports are absent.
- [x] Implement concurrent multicast discovery with one bounded shared deadline and per-interface probing.
- [x] Implement all-profile stream URI lookup over `OnvifDevice`.
- [x] Refactor the existing scripts and add `discover`/`streams` CLI dispatch without changing direct playback arguments.
- [x] Run the focused tests, full npm tests, typecheck, and build.

### Task 2: Python discovery and streams APIs

**Files:**
- Create: `python/onvif_backchannel/onvif.py`
- Create: `python/test_onvif_library.py`
- Modify: `python/onvif_backchannel/__init__.py`
- Modify: `python/onvif_backchannel/cli.py`
- Modify: `python/test_library_api.py`

- [x] Write failing tests for structured ProbeMatch parsing, deduplication, shared-deadline interface probing, exact all-profile URI lookup with reserved-character credentials, package exports, JSON Lines CLI output, and CLI delegation.
- [x] Run focused unittest targets and confirm the API is absent.
- [x] Implement concurrent multicast discovery through every selected interface using standard-library sockets, one shared deadline, and `ElementTree`.
- [x] Implement authenticated all-profile lookup using the validated ONVIF SOAP helpers.
- [x] Add backward-compatible `discover` and `streams` CLI dispatch.
- [x] Run focused and full Python tests, then rebuild and install the wheel in a clean virtual environment.

### Task 3: Rust discovery and streams APIs

**Files:**
- Create: `rust/src/discovery.rs`
- Create: `rust/tests/onvif_api.rs`
- Modify: `rust/src/lib.rs`
- Modify: `rust/src/onvif.rs`
- Modify: `rust/src/cli.rs`
- Modify: `rust/src/main.rs`

- [x] Write failing tests for ProbeMatch parsing/merge behavior, shared-deadline interface probing, public API compilation, exact all-profile resolution with reserved-character credentials, JSON Lines output, and CLI parsing.
- [x] Run focused Cargo tests and confirm the API is absent.
- [x] Implement concurrent bounded UDP multicast discovery with configurable IPv4 interfaces and one shared deadline.
- [x] Add `StreamUriOptions`, `StreamUri`, and all-profile `get_stream_uris()` over `OnvifDevice`.
- [x] Add backward-compatible `discover` and `streams` CLI dispatch.
- [x] Run formatting, Clippy, all tests, release build, crate packaging, and extracted-package consumer compilation.

### Task 4: Documentation and end-to-end verification

**Files:**
- Modify: `README.md`

- [x] Document equivalent discovery and stream lookup APIs and CLI commands for all three languages.
- [x] Rebuild npm tarball, Python wheel, and Cargo crate and verify clean consumers.
- [x] Scan archives and staged changes for credentials and local paths.
- [x] Run actual LAN discovery and authenticated RTSP URI lookup against the existing camera without playing audio.
- [x] Request code review and prepare only the related files for commit.
