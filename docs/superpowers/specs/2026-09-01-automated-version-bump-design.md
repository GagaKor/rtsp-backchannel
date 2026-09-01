# Automated Version Bump Design

**Date:** 2026-09-01
**Status:** Approved
**Target release:** 0.4.0 (the first release this pipeline produces)

## Goal

Make the version rise on its own when work moves from `dev` to `master`, so
that merging a `dev` → `master` pull request publishes a release without anyone
hand-editing five manifests, six READMEs, a test assertion, and the changelog.

Today `release.yml` publishes only when a manifest version differs from what is
live on npm, PyPI, and crates.io. Nothing computes that difference. A merge with
no hand-written bump is not an error — it is a silent no-op that ships nothing,
which is how the VIGI transport, CommonJS support, and the `audioSend`
capability report have all accumulated under `[Unreleased]` while 0.3.1 stayed
live on all three registries.

## Non-goals

- **Changing `release.yml`.** The publish pipeline — OIDC trusted publishing,
  the pinned release SHA, the draft-release provenance marker, the five-binary
  asset set — is not touched. This design feeds it a correctly versioned commit
  and otherwise leaves it alone.
- **Generating changelog prose from commit messages.** The `[Unreleased]`
  entries are hand-written and carry facts no commit subject holds (the camera
  model and firmware a transport was verified against, the round-trip cost of a
  probe). The automation dates them; it does not write them.
- **Declaring 1.0.0.** While the major version is 0, no input causes a major
  bump. Leaving 0.x is a human decision.
- **Enforcing commit-message format.** No commitlint, no hooks. The convention
  is documented and load-bearing, not mechanically enforced.
- **Releasing from any branch other than `dev`.** Feature branches and
  Dependabot PRs targeting `master` are untouched.

## Trigger and flow

A new workflow, `.github/workflows/version-bump.yml`:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]
```

guarded to run only when all of the following hold:

- `github.repository == 'GagaKor/rtsp-backchannel'`
- `github.base_ref == 'master'`
- `github.head_ref == 'dev'`

It needs `permissions: contents: write` because the repository default is
`read`, and `concurrency` keyed on the PR number with `cancel-in-progress: true`
so two rapid pushes cannot race to write `dev`.

The flow:

1. A `dev` → `master` PR is opened.
2. The workflow computes the target version and rewrites the versioned files.
3. If anything changed, it commits as `github-actions[bot]` and pushes to `dev`.
   The PR diff now contains the bump.
4. A human reviews the version and the dated changelog section in the PR, then
   merges.
5. `master` receives a commit whose manifest version differs from every
   registry, so `release.yml` runs its normal `push` trigger and publishes.

`dev` and `master` cannot drift apart, because the bump happens on `dev` and
reaches `master` through the merge — never the other way around.

### Why no infinite loop

Two independent guards, either of which alone is sufficient:

- A push made with `GITHUB_TOKEN` does not trigger workflow runs. The push to
  `dev` raises no `synchronize` event.
- The bump is idempotent. A second run over an already-bumped branch computes
  the same end state, finds the working tree unchanged, and exits without
  committing.

### Why the bump lands on `dev` and not on `master`

Bumping after the merge, on `master`, was considered and rejected. It leaves
`dev` holding the old version, so the next `dev` → `master` PR either conflicts
across all five manifests or presents a diff that *lowers* the version. Closing
that hole requires a second automation to sync `master` back into `dev`. It also
needs `release.yml` to be re-invoked explicitly through `workflow_dispatch`,
because the bump push carries `GITHUB_TOKEN` and therefore triggers nothing, and
it breaks the day `master` gains branch protection.

## Deciding whether to release, and by how much

Two signals with two distinct jobs:

| Question | Source |
|---|---|
| Should this release at all? | Does `CHANGELOG.md` have content under `## [Unreleased]`? |
| How far should the version move? | Conventional-commit types since the last release |

**Release gate.** An empty `[Unreleased]` means no release: the workflow makes
no change, the PR merges normally, and `release.yml` no-ops exactly as it does
today. This is deliberate. Deciding releasability from commit types alone leaves
`docs`-only changes permanently ambiguous — this repository ships its READMEs
inside all three packages, so a docs change is sometimes worth a release and
sometimes not. Making the hand-written changelog the trigger resolves that
ambiguity in the one place a human already states intent, and matches the rule
that people write the prose and the machine adds the date.

**Level rules.** Commits are read from `last_released..HEAD` with `--no-merges`
(merge commits are the only non-conventional subjects in this history) and
mapped while the major version is 0:

| Commit signal | Bump |
|---|---|
| `BREAKING CHANGE:` in a body, or `!` before the `:` in a subject | minor |
| `feat` | minor |
| anything else (`fix`, `perf`, `refactor`, `docs`, `test`, `chore`, …) | patch |

Breaking changes map to minor rather than major because at 0.x the minor
position *is* the compatibility boundary, and the install pins already encode
that: `rtsp-backchannel@^0.3`, `'rtsp-backchannel>=0.3,<0.4'`, and
`rtsp-backchannel = "0.3"` all stop at the next minor. A consumer pinned to 0.3
does not silently receive a breaking 0.4. Auto-promoting to 1.0.0 would claim a
stability guarantee nobody decided to make.

Applied to `dev` as of this writing — 6 `feat`, 18 `fix`, 8 `docs`, 3
`refactor`, 2 `test`, 1 `chore` since v0.3.1 — the target is **0.4.0**.

### Establishing `last_released`

In order:

1. `v<version in package.json>`, if that tag exists. This is the normal case:
   the manifest holds the last released version and `release.yml` tagged it.
2. Otherwise the most recent `v*` tag reachable from `HEAD`. This covers a
   branch that already carries a bump commit, where the manifest version is
   ahead of every tag.
3. Otherwise the full history.

## Re-running on an updated PR

The hardest case, and the main correctness risk of bumping on the PR: the PR is
open, a bump commit already exists, and `dev` receives more work — possibly a
`feat` that raises the level, and certainly new `[Unreleased]` prose that must
end up in the release.

The script does not try to patch the previous run's output incrementally. It
recomputes the complete desired end state from `last_released` and the current
file contents, then writes it. Concretely, for the changelog:

- Let `target` be the computed version and `pending` be everything currently
  under `## [Unreleased]`.
- A **staged section** is a dated section whose version is greater than
  `last_released`. There is at most one; it is the previous run's output.
- If a staged section exists, rename its heading to
  `## [target] - <today, UTC>` and append `pending` to its body.
- Otherwise create `## [target] - <today, UTC>` directly below `[Unreleased]`
  with `pending` as its body.
- Leave `## [Unreleased]` present and empty.
- Rewrite the bottom link references: `[Unreleased]` points at
  `compare/v<target>...HEAD`, and a `[target]` reference points at
  `releases/tag/v<target>`. A stale staged reference is replaced, not
  accumulated.

Emptying `[Unreleased]` also serves as the idempotency latch: a re-run with no
new work sees an empty `[Unreleased]`, hits the release gate, and stops before
touching anything.

The manifests need no special case — they are overwritten with `target`
unconditionally.

## The bump engine

`tools/bump_version.py`, a dependency-free Python script. The job runs
`actions/checkout` plus the Python already present on `ubuntu-latest`; no Node
or Rust toolchain is installed.

It edits files directly rather than calling `npm version --no-git-tag-version`
and `cargo update --precise`, the commands `RELEASING.md` prescribes for humans.
The gate in `release.yml` reads five specific values with `jq`, `grep -m1`, and
an `awk` scan of `Cargo.lock`; writing those five values directly makes the
result deterministic and independent of npm's and cargo's lockfile behaviour,
and keeps two toolchain installs out of the critical path.

| File | Change | When |
|---|---|---|
| `package.json` | `.version` | always |
| `package-lock.json` | `.version` **and** `.packages[""].version` | always |
| `python/pyproject.toml` | `version = "…"` | always |
| `rust/Cargo.toml` | `version = "…"` | always |
| `rust/Cargo.lock` | `version` in the `rtsp-backchannel` package block | always |
| `CHANGELOG.md` | section promotion and link references, per the algorithm above | always |
| `python/test_library_api.py` | the asserted `project.version` | always |
| `README.md`, `README.ko.md` | `rtsp-backchannel@^<major>.<minor>` | minor bumps only |
| `python/README.md`, `python/README.ko.md` | `'rtsp-backchannel>=<major>.<minor>,<<major>.<minor+1>'` | minor bumps only |
| `rust/README.md`, `rust/README.ko.md` | `rtsp-backchannel = "<major>.<minor>"` | minor bumps only |

The install pins carry only major and minor, so a patch bump must leave all six
READMEs untouched. Rewriting them on a patch would be a no-op at best and drift
at worst.

Every edit is verified in place: the script asserts it matched exactly the
expected number of occurrences per file and fails loudly otherwise. A silent
zero-match substitution is how a manifest gets left behind, and a manifest left
behind fails `release.yml` at the gate with all three registries already
half-considered.

## Pre-push gate

A push made with `GITHUB_TOKEN` does not start new workflow runs, so the PR's
CI checks do **not** re-run against the bump commit. The bump would otherwise
reach `master` unverified, and surface only when `release.yml` runs the full
test suites immediately before publishing.

The bump job therefore reproduces the `release.yml` gate locally and fails
without pushing if any check fails:

1. The five manifest and lock values all equal `target`.
2. `CHANGELOG.md` contains exactly one dated section for `target` — the same
   `awk` predicate `release.yml` uses.
3. `PYTHONPATH=python:. python -m unittest test_library_api` passes, exercising
   the version assertion against the real `pyproject.toml`.
4. The six README pin lines and the Python assertion hold their expected new
   values.

Check 4 asserts specific lines rather than grepping the tree for the old version
string. A blanket grep produces false positives on legitimate historical text:
`CHANGELOG.md` keeps every past `## [0.3.1]` section, and `RELEASING.md`'s
manual fallback contains a `VERSION=0.3.1` example.

The failure surfaces in the PR, before `master`, which is the whole point of
bumping on the PR rather than after the merge.

## Testing

`python/test_release_bump.py`, picked up by the existing
`unittest discover -s python` in `ci.yml`. Each test builds a fixture tree
holding the real file shapes and runs the script against it.

- An empty `[Unreleased]` yields no release and leaves every file byte-identical.
- `feat` present yields a minor bump; only `fix` yields a patch.
- `feat!` and `BREAKING CHANGE:` yield a minor bump, never a major, while the
  major version is 0.
- A patch bump leaves all six READMEs untouched; a minor bump rewrites all six.
- The changelog promotion produces exactly one dated section for the target,
  an empty `[Unreleased]`, and correct link references.
- Re-running on an already-bumped tree changes nothing.
- Re-running after new `[Unreleased]` content and a new `feat` folds the prose
  into the staged section and raises the heading from patch to minor.
- A manifest whose expected pattern is missing raises rather than silently
  skipping.
- The result satisfies all four pre-push gate checks.

## Documentation

- `RELEASING.md` — rewrite "Automated releases (the normal path)" around the new
  flow. The version-editing steps become the fallback. The claim that the test
  suite fails when README pins are stale is corrected: no test asserts the pins;
  only `python/test_library_api.py` hard-codes the version.
- `CONTRIBUTING.md` — state that Conventional Commit types now determine the
  version and that `[Unreleased]` decides whether a release happens at all.
  Nothing enforces either mechanically.
- `docs/decisions/2026-09-01-automate-version-bump-on-release-prs.md` — an ADR
  recording the choice of bumping on the PR.

## Trade-offs accepted

- **The bump happens when the PR opens, not when it merges.** Work added to an
  open PR triggers a recompute and may add a second bump commit. Handled by the
  re-run algorithm above, but it is real added complexity.
- **The bot pushes directly to `dev`.** Neither branch is protected today. If
  `dev` gains protection, the bot needs an exception.
- **CI does not re-run on the bump commit.** Mitigated by the pre-push gate,
  which is strictly stronger for the versioned files than a re-run of `ci.yml`
  would be, but weaker for anything else.
- **Commit-type discipline becomes load-bearing without enforcement.** A `feat`
  mislabeled as `fix` produces a patch release of a feature. The changelog
  review in the PR is the backstop.

## Decision Journey

### Initial Request

Make the version rise naturally as work moves from `dev` to `master`, via CI.

### Plan Evolution

- **Bumping on the PR beat bumping on `master`.** Bumping on `master` strands
  `dev` at the old version, which forces either a manifest conflict or a
  version-lowering diff on the next cycle, and needs a second automation to
  resolve. It also needs a `workflow_dispatch` chain, because a `GITHUB_TOKEN`
  push triggers nothing.
- **Bumping inside `release.yml` was rejected.** One workflow instead of two,
  but the pinned-SHA, tag, and draft-marker logic all assume `github.sha` is the
  released commit. Reworking a pipeline that already publishes correctly to
  three registries over OIDC is risk without a matching gain.
- **The changelog became the release trigger, not just its content.** Deciding
  releasability from commit types leaves `docs`-only changes ambiguous forever
  in a repository that ships its READMEs inside its packages.
- **Breaking changes map to minor, not major**, while the major version is 0.
  The install pins already treat minor as the compatibility boundary.
- **Direct file edits beat `npm version` and `cargo update`.** The gate reads
  five specific values; writing them directly is deterministic and avoids
  installing two toolchains for a job that only rewrites text.
- **The pre-push gate was added once it was clear CI cannot see the bump
  commit.** A `GITHUB_TOKEN` push starts no workflow run, so without it the
  bump would be verified for the first time during publishing.

### Outcome

A single `pull_request` workflow on `dev` → `master` that reads the changelog
for intent and the commit log for magnitude, rewrites seven versioned files —
thirteen when the bump is a minor and the six install pins move with it —
verifies them against the publish pipeline's own gate, and pushes one reviewable
commit, leaving `release.yml` untouched.
