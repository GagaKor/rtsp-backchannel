# ONVIF Backchannel Library Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the validated TypeScript, Python, and Rust backchannel implementations so another project can install `onvif-backchannel` and call one high-level file playback API.

**Architecture:** Keep three native implementations and expose the same `playFile`/`play_file` operation in each language. The existing CLI remains a thin wrapper around library code. Registry publication is not performed automatically; reproducible npm tarball, Python wheel, Cargo package, and clean consumer install tests prove each package is ready for publication or Git-based installation.

**Tech Stack:** Node.js 22, TypeScript 5.9, npm package exports, Python 3.11+ with setuptools/PEP 517, Rust 1.85+ with Cargo.

---

### Task 1: TypeScript npm package

**Files:**
- Create: `src/index.ts`
- Create: `src/index.test.ts`
- Create: `tsconfig.build.json`
- Modify: `package.json`
- Modify: `src/cli.ts`

- [ ] Write a public-entry test that imports `playFile`, `openBackchannel`, G.711 helpers, and public types from `src/index.ts`.
- [ ] Run `npm test` and confirm the missing public entry fails.
- [ ] Add the public entry point and keep the existing CLI behavior intact.
- [ ] Configure `tsc` to emit ESM JavaScript and declaration files into `dist/`, rewriting relative `.ts` imports to `.js`.
- [ ] Configure package name `onvif-backchannel`, `exports`, `types`, `files`, CLI `bin`, `build`, `prepack`, and Node engine metadata.
- [ ] Run `npm run build`, `npm test`, and inspect `npm pack --dry-run --json`.
- [ ] Create the tarball with `npm pack --json`, install it into an empty temporary consumer, and run an ESM import smoke test.

### Task 2: Python wheel package

**Files:**
- Create: `python/pyproject.toml`
- Create: `python/onvif_backchannel/__init__.py`
- Create: `python/onvif_backchannel/playback.py`
- Create: `python/test_library_api.py`

- [ ] Add a test for `play_file()` using fake decode/session dependencies, asserting PCMA 8 kHz, 40 ms packetization, result metadata, final pacing wait, and cleanup.
- [ ] Run the focused unittest and confirm the public API is absent.
- [ ] Implement `PlaybackResult` and the one-shot `play_file()` wrapper over the validated decoder, ONVIF/RTSP transport, packetizer, and rebase pacer.
- [ ] Add setuptools metadata for distribution name `onvif-backchannel`, explicitly include `backchannel_audio`, `backchannel_rtp`, and `onvif_play` as `py-modules`, and add the `onvif-backchannel` CLI entry point.
- [ ] Run all Python tests and build a wheel with `pip wheel`.
- [ ] Install the wheel into an isolated target directory and run an import/API smoke test.

### Task 3: Rust crate API and package

**Files:**
- Create: `rust/src/playback.rs`
- Modify: `rust/src/lib.rs`
- Modify: `rust/src/main.rs`
- Modify: `rust/Cargo.toml`
- Modify: `rust/Cargo.lock`

- [ ] Add unit tests for the high-level playback result and guaranteed session cleanup through injected fake decode/session operations.
- [ ] Run the focused Cargo test and confirm the high-level API is absent.
- [ ] Implement `PlaybackConfig`, `PlaybackResult`, and `play_file()` while preserving decode, negotiated G.711, send pacing, and TEARDOWN behavior.
- [ ] Refactor the Rust binary to call the library API.
- [ ] Rename the distributable package to `onvif-backchannel`, remove `publish = false`, and add package metadata.
- [ ] Run Cargo tests, formatting, Clippy, release build, inspect `cargo package --list`, and create the `.crate` with `cargo package`.
- [ ] Extract the generated `.crate` and compile a temporary consumer against the extracted packaged contents rather than the source tree.

### Task 4: Installation documentation and release checks

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`

- [ ] Document npm/PyPI/crates.io names, Git-based installation before registry publication, and minimal API examples for all three languages.
- [ ] Document `npm publish`, Python upload, and `cargo publish` as explicit maintainer steps requiring registry credentials.
- [ ] Ignore generated package/build artifacts without hiding source or lock files.
- [ ] Run TypeScript, Python, and Rust full regression suites.
- [ ] Inspect package contents for source credentials, local paths, tests, and unwanted build artifacts.
- [ ] Run a final actual-camera playback through one unchanged implementation only if runtime protocol code changed.
