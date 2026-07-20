# Releasing

This repository publishes the same version under the `rtsp-backchannel` name
to npm, PyPI, and crates.io. FFmpeg is an external runtime prerequisite and
must never be bundled in these artifacts.

## 1. Prepare the release

1. Update the version in `package.json`, `package-lock.json`,
   `python/pyproject.toml`, `rust/Cargo.toml`, and `rust/Cargo.lock`.
2. Move the pending entries in `CHANGELOG.md` to a dated release section.
3. Merge the version change to `master` and work from a clean checkout of that
   commit.
4. Confirm that `npm whoami`, PyPI authentication, and a crates.io API token
   are available before creating the tag.

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

After the initial npm and PyPI projects exist, configure their trusted
publisher settings for this GitHub repository before automating subsequent
releases. Store a crates.io token as a GitHub Actions secret if crates.io
publishing is later automated.

## 5. Create the GitHub release

```bash
gh release create v0.1.0 \
  --verify-tag \
  --title 'rtsp-backchannel 0.1.0' \
  --notes-from-tag
```

Verify that all three registry pages show the expected version and metadata,
then install each package in a clean consumer project and run its CLI help.
