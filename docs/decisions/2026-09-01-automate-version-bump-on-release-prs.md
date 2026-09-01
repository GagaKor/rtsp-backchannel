# Bump the version on the dev → master pull request, not after the merge

**Date:** 2026-09-01
**Status:** Accepted

## Context

`release.yml` publishes to npm, PyPI and crates.io on every push to `master`,
but only for packages whose manifest version differs from the version already
live on that registry. Nothing computed that difference. A merge carrying no
hand-written bump was not an error — it was a silent no-op that shipped
nothing, which is how the VIGI transport, CommonJS support and the `audioSend`
capability report all accumulated under `[Unreleased]` while 0.3.1 stayed live
on all three registries.

A version lives in thirteen files: five manifests and lock files that the release
gate cross-checks, six README install pins, one hard-coded test assertion, and
the changelog. Moving them by hand is the step that got skipped.

## Decision

A `pull_request` workflow on `dev` → `master` computes the next version, rewrites
every versioned file, verifies the result against the release gate, and pushes
one commit to `dev`. The pull request carries the bump; merging it publishes.

## Alternatives Considered

### Bump on the pull request (chosen)

The bump lands on `dev` before the merge, so `master` never exists in a state
that needs a follow-up bump, and the *version* cannot regress — it only moves
forward as the bump reaches `master` through the merge, never the other way
around. The branches themselves still drift apart (`master` gains its own
commits, and the release tag lands on the merge commit, which is never an
ancestor of a later `dev`), which is why the last release is resolved by tag
existence rather than by reachability from `dev`. A human reviews the version
and the dated changelog section in the same diff as the work. `release.yml`
is not modified.

The cost is that the bump happens when the PR opens rather than when it merges,
so work added afterwards triggers a recompute — handled by recomputing the whole
end state from the last release rather than patching the previous run's output.

### Bump on master after the merge

Rejected. It leaves `dev` at the old version, so the next `dev` → `master` pull
request either conflicts across all five manifests or presents a diff that
*lowers* the version; closing that hole needs a second automation to sync
`master` back into `dev`. It also needs `release.yml` to be re-invoked through
`workflow_dispatch`, because a push made with `GITHUB_TOKEN` triggers no
workflow run, and it breaks the day `master` gains branch protection.

### Bump inside release.yml

Rejected. One workflow instead of two, but the pinned-SHA, tag and
draft-provenance logic all assume `github.sha` is the released commit.
Reworking a pipeline that already publishes correctly to three registries over
OIDC trusted publishing is risk without a matching gain, and the file's path and
environment name are registered with each registry.

## Reasoning

Two signals with two distinct jobs. The hand-written `[Unreleased]` section
decides *whether* to release; the conventional-commit log decides *by how much*.
Deciding releasability from commit types alone leaves `docs`-only changes
permanently ambiguous in a repository that ships its READMEs inside all three
packages — sometimes a docs change is worth a release and sometimes it is not.
The changelog is where a human already states that intent.

Breaking changes map to minor while the major version is 0. The install pins
(`^0.3`, `>=0.3,<0.4`, `"0.3"`) already treat the minor position as the
compatibility boundary, so a consumer pinned to 0.3 does not silently receive a
breaking 0.4. Declaring 1.0.0 is a claim automation should not make.

## Trade-offs Accepted

- The bot pushes directly to `dev`. Neither branch is protected today; if `dev`
  gains protection the bot needs an exception.
- A `GITHUB_TOKEN` push starts no workflow run, so the pull request's checks do
  not re-run against the bump commit. The bump job therefore reproduces the
  release gate itself and fails before pushing.
- Commit-type discipline becomes load-bearing without mechanical enforcement. A
  `feat` mislabeled as `fix` produces a patch release of a feature; the changelog
  review in the pull request is the backstop.

## Related Code Paths

- `tools/bump_version.py`
- `.github/workflows/version-bump.yml`
- `.github/workflows/release.yml` (unmodified; consumes the bumped commit)
- `docs/superpowers/specs/2026-09-01-automated-version-bump-design.md`

## Consequences

Releasing becomes: write the changelog entry, open the pull request, review one
generated commit, merge. The version references that used to be updated by hand
— and were the step that got skipped — are no longer a manual step at all.
