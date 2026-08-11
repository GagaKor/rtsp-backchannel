# Releasing

This repository publishes the same version under the `rtsp-backchannel` name
to npm, PyPI, and crates.io. FFmpeg is an external runtime prerequisite and
must never be bundled in these artifacts.

## Automated releases (the normal path)

`.github/workflows/release.yml` publishes on every push to `master`. It reads
the version out of `package.json`, `python/pyproject.toml`, and
`rust/Cargo.toml`, compares each one against what is currently live on npm,
PyPI, and crates.io, and publishes only the packages whose manifest version
differs from the published version. A merge that doesn't change a version
number (a Dependabot bump, a docs fix, etc.) publishes nothing. Each publish
job runs its language's full test suite first and authenticates to its
registry with OIDC trusted publishing — no tokens are stored in the
repository or its secrets.

Before publishing, the workflow verifies that all five versioned manifest and
lock files agree and that `CHANGELOG.md` has exactly one dated section for that
version. It then tags the commit and creates a GitHub Release when the version
has no complete release yet, attaching single-file Rust CLI binaries for Linux
(x64 and ARM64), macOS (Apple Silicon and Intel), and Windows (x64). This runs
independently of whether any package actually published, so it also backfills
a release for a version that is already live on every registry but was never
tagged. The tag is pushed before any registry job starts, so even a partial
registry failure leaves a durable source SHA for every retry. Before those
jobs begin, a private draft Release stores the same SHA in a hidden note
marker. Tag, draft, and asset state are checked separately: a retry checks out
that pinned commit and continues from the same source. Every registry and
platform build uses the SHA, and the tag is checked again before publication.
A draft, missing asset, or missing marker makes the workflow rebuild and
replace the entire five-binary set while the Release is private; a marker that
names a different SHA is a hard error requiring manual repair, never a reason
to reinterpret already-published artifacts. The workflow also repairs the
repository's `Latest` designation so the README release badge and
`/releases/latest` links resolve to the current version. Once all five assets
exist on the published release, routine master pushes skip the binary builds.
Tagging and releasing use the automatic `GITHUB_TOKEN`; no additional secret
is involved.

To ship a release:

1. Do step 1 below ("Prepare the release") to bump the versions and update
   the version references that live outside the manifests.
2. Merge that change to `master`.
3. The workflow runs automatically, pins the tag and provenance draft, then
   publishes whichever packages changed version and completes the GitHub
   Release. Watch the run in the Actions tab; each registry's publish job is
   skipped (not failed) for a package whose version didn't change.

You can also trigger a run manually from the Actions tab
(`workflow_dispatch`) if you need to retry a publish without pushing a new
commit — it applies the same per-package version check.

**Repository setup:** the workflow runs its publish jobs in the existing
GitHub Actions environment named `release`. Keep that environment and the
registries' Trusted Publisher configuration pointed at `release.yml` and the
`release` environment. No secrets need to be added to it — OIDC trusted
publishing requires none.

The manual process below remains the fallback if the workflow can't run, and
is also the process for the very first publish to each registry (creating a
new project on npm/PyPI/crates.io, and configuring Trusted Publisher settings
for this repository) and for authenticating locally.

## 1. Prepare the release

1. Update the version in `package.json`, `package-lock.json`,
   `python/pyproject.toml`, `rust/Cargo.toml`, and `rust/Cargo.lock`.
   `npm version <new> --no-git-tag-version` covers the first two, and
   `cargo update --manifest-path rust/Cargo.toml --package rtsp-backchannel
   --precise <new>` refreshes the Cargo lock after the manifest edit.
2. Update the version references that live outside the manifests, or the test
   suite will fail: the install pins in all six READMEs
   (`rtsp-backchannel@^X.Y` for npm, `'rtsp-backchannel>=X.Y,<X.Z'` for pip,
   `rtsp-backchannel = "X.Y"` for Cargo) and the asserted version in
   `python/test_library_api.py::test_declares_installable_wheel_metadata`.
   `git grep` for the outgoing version to catch any others.

   The version now appears exactly once per README, inside the install
   command. It used to also appear in the surrounding prose, which is how
   `README.md` shipped 0.3.0 still claiming the release line was `0.2` — the
   prose and the command drifted apart. Keep it stated once.
3. Move the pending entries in `CHANGELOG.md` to a dated release section and
   update the link references at the bottom of the file.
4. Merge the version change to `master` and work from a clean checkout of that
   commit.
5. For the manual fallback only, confirm that `npm whoami`, PyPI
   authentication, and a crates.io API token are available before creating
   the tag.

## 2. Verify source and artifacts

Run from the repository root:

```bash
npm ci
npm test
npm run typecheck
npm pack --dry-run --json

PYTHONPATH=python:. python3 -m unittest discover -s python -p 'test_*.py'
python3 -m pip install --upgrade build twine
(cd python && python3 -m build)
python3 -m twine check python/dist/*

cargo test --manifest-path rust/Cargo.toml --locked
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
cargo package --manifest-path rust/Cargo.toml --locked
```

Inspect the npm tarball, Python wheel and source archive, and Cargo package.
They must contain the license and notice files and must not contain an FFmpeg
binary, credentials, media files, packet captures, or build caches.

## 3. Tag the verified commit

This step and step 5 are now the fallback: the automated workflow above
already tags the commit and creates the GitHub Release for you once it
publishes to master. Only do this by hand if the workflow can't run, or to
tag a commit outside that path.

Replace `0.1.0` with the version being released:

```bash
git tag -a v0.1.0 -m 'rtsp-backchannel 0.1.0'
git push origin v0.1.0
```

## 4. Publish registries

The first release requires an account with publish access to each registry.
Never store tokens in the repository.

```bash
# npm: run from the repository root after `npm login`
npm publish --access public

# PyPI: upload the artifacts built in step 2
python3 -m twine upload python/dist/*

# crates.io: authenticate with `cargo login` first
cargo publish --manifest-path rust/Cargo.toml --locked
```

After the initial projects exist, configure npm, PyPI, and crates.io trusted
publishing for this GitHub repository before automating subsequent releases.
The automated workflow exchanges GitHub OIDC identity for short-lived
credentials and does not store a registry token as an Actions secret.

## 5. Create the GitHub release

The automated workflow is strongly preferred because a compliant GitHub
Release requires all five native binaries. If automation is unavailable,
build on native hosts using the same target matrix from `release.yml`, collect
the binaries under their exact release names, and extract the matching
changelog section before creating the Release:

```bash
VERSION=0.3.0
TARGET=x86_64-unknown-linux-gnu # Select the native target for this host.
SUFFIX=
[ "$TARGET" = x86_64-pc-windows-msvc ] && SUFFIX=.exe
RELEASE_COMMIT=$(git rev-parse HEAD)
[ "$(git rev-list -n 1 "v${VERSION}")" = "$RELEASE_COMMIT" ]

cargo +1.86.0 build --release --locked \
  --manifest-path rust/Cargo.toml \
  --target "$TARGET"

mkdir -p release-dist
cp "rust/target/${TARGET}/release/rtsp-backchannel${SUFFIX}" \
  "release-dist/rtsp-backchannel-${VERSION}-${TARGET}${SUFFIX}"

# Required asset names (the Windows binary keeps its .exe suffix):
# rtsp-backchannel-${VERSION}-x86_64-unknown-linux-gnu
# rtsp-backchannel-${VERSION}-aarch64-unknown-linux-gnu
# rtsp-backchannel-${VERSION}-aarch64-apple-darwin
# rtsp-backchannel-${VERSION}-x86_64-apple-darwin
# rtsp-backchannel-${VERSION}-x86_64-pc-windows-msvc.exe

awk -v ver="$VERSION" '
  /^## \[/ {
    if (in_section) exit
    if (index($0, "## [" ver "] - ") == 1) in_section = 1
  }
  in_section { print }
' CHANGELOG.md > release-notes.md
test -s release-notes.md
printf '\n<!-- rtsp-backchannel-release-commit: %s -->\n' \
  "$RELEASE_COMMIT" >> release-notes.md

gh release create "v${VERSION}" \
  --verify-tag \
  --title "rtsp-backchannel ${VERSION}" \
  --notes-file release-notes.md \
  release-dist/*
```

Do not publish an assetless fallback Release. If all five native binaries are
not available, leave only the pushed tag and run `workflow_dispatch` later;
the automated workflow detects the tag without a complete Release and
backfills the matching assets from that tag commit.

Verify that all three registry pages show the expected version and metadata,
then install each package in a clean consumer project and run its CLI help.
