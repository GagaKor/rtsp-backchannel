# Contributing

## License

Unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in this project by you, as defined in the Apache
License, Version 2.0, is dual-licensed under `MIT OR Apache-2.0`, without any
additional terms or conditions.

Only submit material that you have the right to license under both licenses.
If a contribution contains third-party material, identify its source and
license in the pull request and do not submit it unless it is compatible with
both project licenses.

## FFmpeg

Do not commit or package FFmpeg source code or binaries in this repository.
The project invokes a separately installed `ffmpeg` executable from `PATH`.
Anyone distributing FFmpeg alongside this project is responsible for the
license terms that apply to that exact FFmpeg build.

## Branches

Work reaches `master` through `dev`:

    feature branch -> dev -> master

- **Open every pull request against `dev`.** A branch merged straight into
  `master` skips `version-bump.yml`, whose job runs only when the head branch
  is `dev`. The `CHANGELOG.md` entry then exists on `master` alone, so the next
  `dev` -> `master` pull request reads an empty `[Unreleased]` and publishes
  nothing. The release is lost with every check green, which is why this is
  worth a rule rather than a habit.
- **`dev` -> `master` is the release pull request.** Opening it bumps the
  version on `dev`; merging it publishes. See `RELEASING.md`.
- **Dependabot is the one exception** and targets `master` directly. Its
  updates carry no `[Unreleased]` entry and are not meant to ship on their own.

Two things enforce this:

- The `Branch flow` job in `.github/workflows/ci.yml` fails any pull request
  into `master` whose head branch is neither `dev` nor a dependabot branch.
  Make it a required status check on `master` so the merge button stays
  disabled rather than merely red.
- `.githooks/pre-push` refuses a direct push to `master`. Cloning does not
  install hooks, so enable them once per clone:

      git config core.hooksPath .githooks

## Commit messages and releases

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/)
— `type(scope): description`. Nothing enforces this mechanically, and two parts
of the release now depend on it:

- **`feat`, and any `!` or `BREAKING CHANGE:` footer, move the minor version.**
  Everything else moves the patch version. While the major version is 0, a
  breaking change moves the minor rather than the major: at 0.x the minor
  position is the compatibility boundary, and the install pins already stop at
  the next minor. Nothing promotes the project to 1.0.0 automatically.
- **`## [Unreleased]` in `CHANGELOG.md` decides whether a release happens at
  all.** Write the entry as part of the change. An empty `[Unreleased]` means a
  `dev` → `master` merge publishes nothing, which is the correct outcome for
  work that should not ship on its own.

Opening the `dev` → `master` pull request bumps the version; merging it then
publishes automatically. See `RELEASING.md`.
