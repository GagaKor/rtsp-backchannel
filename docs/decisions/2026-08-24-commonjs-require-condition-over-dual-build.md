# Support CommonJS with a `require` export condition instead of a dual build

**Date:** 2026-08-24
**Status:** Accepted
**Scope:** npm package manifest (`exports`, `engines`); packaging contract test; TypeScript READMEs

## Context

A consumer reported this warning while using the package:

```
[MODULE_TYPELESS_PACKAGE_JSON] Warning: ... it doesn't parse as CommonJS.
Reparsing as ES module because module syntax was detected. This incurs a performance overhead.
```

That warning originates in the consuming project, not here — Node emits it when a `.js` file
containing `import` syntax sits under a `package.json` with no `"type"` field, and Node itself
names the fix. This repository already sets `"type": "module"`.

The warning did surface a real gap, though. `exports["."]` declared only an `import` condition, so
`require('rtsp-backchannel')` failed outright with `ERR_PACKAGE_PATH_NOT_EXPORTED`. A CommonJS
consumer's only recourse was to convert their project to ESM — exactly what the warning nags about.
The package advertised itself as installable by anyone while being reachable from one module system.

## Decision

Add a `require` condition to `exports["."]` pointing at the same `./dist/index.js` the `import`
condition already serves, and raise `engines.node` from `>=22` to `>=22.12.0`. Ship no second
build. Document both module formats, and the warning's actual cause, in `README.md` and
`README.ko.md`.

## Alternatives Considered

### Single ESM build with a `require` condition (chosen)

- **Description:** One compiled output. Both conditions resolve to `./dist/index.js`; CommonJS callers reach it through Node's `require(esm)`, unflagged since Node 22.12.0.
- **Pros:**
  - Zero source changes and zero build changes — the fix is three lines of manifest.
  - No dual-package hazard is even possible: both conditions name the same file, so a process can only ever hold one module instance.
  - The two entry paths cannot drift, because there is only one artifact to drift from.
- **Cons:**
  - Depends on a runtime capability rather than on emitted CommonJS, so it fails on Node 22.0–22.11 instead of degrading.
  - Constrains the published graph forever: introducing top-level await anywhere reachable from `src/index.ts` would silently break every `require()` caller.

### Full dual build (`dist/cjs/` compiled separately)

- **Description:** A second `tsc` pass emitting CommonJS into `dist/cjs/`, marked by a generated `dist/cjs/package.json` containing `{"type":"commonjs"}`, wired up through nested `exports` conditions.
- **Pros:** Works on every Node that understands `exports`, and on tooling that predates `require(esm)`.
- **Cons:**
  - Buys coverage for Node 22.0–22.11 only. `engines` already required `>=22`, and every Node 22 LTS release is 22.12 or newer, so that window is a superseded two-month pre-LTS band.
  - Reintroduces dual-package hazard and doubles the declaration surface.
  - Requires deleting the `import.meta.url` main-module guard from `src/cli.ts`, because `index.ts` re-exports `playFile` from it and `import.meta` is a syntax error under `module: commonjs`. That cascades into rewiring the CLI entry point onto `src/bin.ts`, repointing four spawn tests, and moving credential redaction into `bin.ts`.
  - TypeScript 7 removed `moduleResolution: node10` (`TS5108`), so the conventional recipe is unavailable; `module: commonjs` + `moduleResolution: bundler` is the only surviving combination.

### Documentation only

- **Description:** Explain in the READMEs that consumers must set `"type": "module"`.
- **Pros:** No code or manifest risk.
- **Cons:** Leaves `require()` broken. Rejected — it answers the warning while ignoring the gap the warning exposed.

### Keeping `engines.node` at `>=22`

- **Description:** Leave the range alone and note the 22.12 requirement in prose only.
- **Pros:** Does not narrow the declared support range for ESM callers, who genuinely work on 22.0.
- **Cons:** A consumer on 22.0–22.11 installs without any warning and discovers `ERR_REQUIRE_ESM` at runtime. Rejected: `engines` should describe where the package actually works, and 22.12.0 is Node 22's LTS promotion release, so nobody real is excluded.

## Reasoning

The decision was made from measurement, not from reasoning about the ecosystem. The package was
built, packed with `npm pack`, and installed into throwaway consumer projects. With only the
`require` condition added, all of the following passed: `require('rtsp-backchannel')` returning all
18 exports; a real audio-encoding call through the CommonJS path (1 s tone → 8000 samples → 8000
PCMA bytes); loading the graph including the CommonJS `saxes` dependency; the installed
`node_modules/.bin` CLI; an ESM consumer with no warning; and — decisively — a TypeScript consumer
in CommonJS mode under `module: nodenext`, which type-checked with zero errors, compiled to a
genuine `require("rtsp-backchannel")`, and ran. TypeScript 7.0.2 raises no `TS1479`.

`require(esm)`'s one hard constraint is that the graph contain no top-level await. The published
graph has none: the only top-level `await` in the repository is in `src/discover.ts`, which is
neither in the `files` allowlist nor reachable from `src/index.ts`.

So the dual build's entire marginal value is Node 22.0–22.11, and its cost includes dismantling a
working CLI entry point. The asymmetry is what settled it.

## Trade-offs Accepted

- The published module graph must stay free of top-level await permanently. This is enforced by the new CommonJS smoke test rather than by convention — a `require()` of an async module fails with `ERR_REQUIRE_ASYNC_MODULE`, so the test breaks the moment someone introduces one.
- Consumers on Node 22.0–22.11 lose `require()` support and are told so by `engines` at install time rather than by a runtime error. They keep working via `import`.
- If a future dependency or feature forces top-level await into the published graph, this decision has to be revisited, and the dual build becomes the fallback — `module: commonjs` + `moduleResolution: bundler` is the combination that survives in TypeScript 7.

## Related Code Paths

- `package.json` — `exports["."].require` and `engines.node`.
- `src/index.test.ts` — the packaging contract: manifest assertions plus the CommonJS `require()` smoke test that compares the CJS and ESM export key sets.
- `README.md`, `README.ko.md` — the Module Formats section documenting both entry paths and the warning's real cause.
- `src/index.ts` — the graph that must remain free of top-level await.

## Consequences

- `src/cli.ts`, `src/bin.ts`, and the build scripts are untouched; the CLI entry point keeps working exactly as before.
- The packaging test now fails if the CommonJS and ESM export surfaces diverge, which also catches an accidental top-level await.
- Python and Rust packages are unaffected — this is npm-only.
