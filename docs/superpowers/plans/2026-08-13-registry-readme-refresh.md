# Registry README Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the README content already merged on GitHub in new 0.3.1 artifacts for npm, PyPI, and crates.io.

**Architecture:** Treat this as a documentation-only synchronized patch release. Bump the five manifest/lock locations and the Python metadata assertion together, add a dated changelog entry, and leave the README install ranges unchanged because they already select every compatible 0.3.x patch. The existing `master` release workflow will compare 0.3.1 with the live 0.3.0 registries and publish all three packages only after PR merge.

**Tech Stack:** npm/package-lock, Python `pyproject.toml`/setuptools, Rust Cargo, GitHub Actions OIDC release workflow.

---

### Task 1: Synchronize the 0.3.1 package metadata

**Files:**
- Modify: `package.json:3`
- Modify: `package-lock.json:3,9`
- Modify: `python/pyproject.toml:7`
- Modify: `python/test_library_api.py:658`
- Modify: `rust/Cargo.toml:3`
- Modify: `rust/Cargo.lock` (`rtsp-backchannel` package entry only)
- Modify: `RELEASING.md:162`

- [ ] **Step 1: Bump npm metadata with the repository-supported command**

Run: `npm version 0.3.1 --no-git-tag-version`

Expected: `package.json`, the root version in `package-lock.json`, and `packages[""]` in `package-lock.json` all report `0.3.1`; no git tag is created.

- [ ] **Step 2: Bump Python metadata and its exact-version contract**

Change the project version and `test_declares_installable_wheel_metadata` expectation from `0.3.0` to `0.3.1`.

- [ ] **Step 3: Bump Rust metadata and refresh only the local package lock entry**

Change `rust/Cargo.toml` to `0.3.1`, then run:

`cargo update --manifest-path rust/Cargo.toml --package rtsp-backchannel --precise 0.3.1`

Expected: the `rtsp-backchannel` entry in `rust/Cargo.lock` reports `0.3.1`; unrelated dependency versions do not change.

- [ ] **Step 4: Verify all release version sources agree**

Run a local assertion over `package.json`, both root `package-lock.json` version fields, `python/pyproject.toml`, `rust/Cargo.toml`, and the local `rtsp-backchannel` Cargo lock entry.

Expected: all six extracted values equal `0.3.1`.

- [ ] **Step 5: Update the live manual-release example**

Change `VERSION=0.3.0` in the fallback GitHub Release example to
`VERSION=0.3.1`. Preserve historical prose that intentionally describes the
0.3.0 release.

### Task 2: Document the README-only patch release

**Files:**
- Modify: `CHANGELOG.md:8-10,62-63`
- Modify: `.github/workflows/release.yml:706-712`

- [ ] **Step 1: Add the dated 0.3.1 changelog section**

Immediately below `[Unreleased]`, add `## [0.3.1] - 2026-08-13` with a `Changed` entry stating that the npm, PyPI, and crates.io package pages now receive the current packaged READMEs, including the cross-language overview, registry links, and badges.

- [ ] **Step 2: Advance changelog comparison links**

Point `[Unreleased]` at `v0.3.1...HEAD`, add the `0.3.1` release link, and retain the historical `0.3.0` link.

- [ ] **Step 3: Remove the completed 0.3.0 backfill assumption from workflow comments**

Keep the workflow behavior unchanged. Rewrite only the stale comment that says no `v0.3.0` tag or Release exists, because PR #32 has already created both.

- [ ] **Step 4: Confirm README install ranges do not need edits**

Verify the TypeScript `@^0.3`, Python `>=0.3,<0.4`, and Rust `"0.3"` constraints in all six READMEs already select 0.3.1. Do not add redundant exact patch versions.

### Task 3: Verify each publishable artifact contains the current README

**Files:**
- Test: `README.md`
- Test: `python/README.md`
- Test: `rust/README.md`

- [ ] **Step 1: Run the TypeScript checks and inspect the npm package**

Run: `npm ci && npm test && npm run typecheck && npm run build`

Create a temporary directory, run `npm pack --json --pack-destination
<temporary-directory>`, extract the resulting tarball, and run `cmp` between
its `package/README.md` and the repository root `README.md`.

Expected: 200 tests pass, typecheck/build succeed, `README.md` plus
`README.ko.md` appear in the package file list, and the packaged English
README is byte-identical to the current root README.

- [ ] **Step 2: Run the Python checks and build validation**

Run: `PYTHONPATH=python:. python3 -m unittest discover -s python -p 'test_*.py'`

Run: `python3 -m build --outdir <temporary-directory> python && python3 -m twine check <temporary-directory>/*`

Extract the sdist and compare its `README.md` byte-for-byte with
`python/README.md`. Extract the wheel, parse the RFC 822 header/body boundary
in `*.dist-info/METADATA`, and compare its description body with
`python/README.md` after accounting only for the metadata serializer's final
newline convention.

Expected: 420 tests pass, both wheel and sdist build, Twine reports both
artifacts valid, and both packaged descriptions match the current
`python/README.md`.

- [ ] **Step 3: Run the Rust checks and package validation**

Run: `cargo +1.86.0 test --manifest-path rust/Cargo.toml --locked`

Run: `cargo +1.86.0 fmt --manifest-path rust/Cargo.toml --check`

Run: `cargo +1.86.0 clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings`

Run: `cargo +1.86.0 package --manifest-path rust/Cargo.toml --locked --allow-dirty`

Extract `rust/target/package/rtsp-backchannel-0.3.1.crate` into a temporary
directory and run `cmp` between its packaged `README.md` and
`rust/README.md`.

Expected: 159 tests pass; formatting, Clippy, and package verification
succeed; the packaged README is byte-identical to the current Rust README.

- [ ] **Step 4: Exercise the exact registry version gate against live state**

Extract and run the `check-versions` version gate from `.github/workflows/release.yml`.

Expected: the manifests report 0.3.1, the live registries report 0.3.0, and
`npm-publish`, `pypi-publish`, and `crates-publish` are all `true`.

- [ ] **Step 5: Run final repository checks**

Run: `actionlint -ignore 'unexpected key "queue"' .github/workflows/release.yml`

Run: `git diff --check origin/master...HEAD` after committing.

Expected: no actionable diagnostics and no whitespace errors.

### Task 4: Commit and open the release-preparation PR

**Files:**
- Include all files from Tasks 1-2 and this plan document.

- [ ] **Step 1: Review the complete diff**

Confirm there are no runtime source changes and no edits to historical design documents.

- [ ] **Step 2: Commit the synchronized patch release**

Commit message: `chore(release): prepare 0.3.1 README refresh`

- [ ] **Step 3: Exercise the exact GitHub Release state gate from the committed tree**

Extract and run the `release-state` shell block after committing, so its
`git show HEAD` checks inspect committed 0.3.1 metadata.

Expected: `v0.3.1` tag and Release are absent, `release-needed=true`,
`assets-needed=true`, and the selected release commit equals the new commit.

- [ ] **Step 4: Push the existing workspace branch and create a new PR to `master`**

Explain that merging the PR is the publication trigger; do not merge it. Include the live gate result and artifact/test evidence in the PR body.

- [ ] **Step 5: Watch PR checks**

Run: `gh pr checks <number> --watch`

Expected: TypeScript, Python 3.11, Python 3.14, and Rust 1.86 all pass.
