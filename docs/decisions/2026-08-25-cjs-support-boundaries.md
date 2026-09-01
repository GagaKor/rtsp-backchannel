# CommonJS support is documented at its real boundaries rather than widened

**Date:** 2026-08-25
**Status:** Accepted
**Scope:** `package.json` (`engines`, `exports`), both READMEs, `src/index.test.ts`

## Context

`2026-08-24-support-commonjs-via-require-condition.md` added a `require`
condition pointing at the single ESM artifact, so `require('rtsp-backchannel')`
works on Node 22.12+ via `require(esm)`. Review of the merged change found the
advertised support is wider than the real support in three places, all
measured on this repo's toolchain:

- **TypeScript CJS consumers on `moduleResolution: node16` cannot compile.**
  Both conditions share one ESM `types` entry, so TypeScript resolves `require`
  to an ESM declaration file and raises `TS1479` ("the referenced file is an
  ECMAScript module and cannot be imported with 'require'"). Verified: the same
  consumer project is clean under `nodenext` and `bundler` and fails under
  `node16`. The decision doc's line "TypeScript 7.0.2 raises no TS1479" holds
  only for `nodenext`.
- **`engines.node` was narrowed from `>=22` to `>=22.12.0`,** which regresses
  install for ESM-only consumers on 22.0–22.11 whose `import` path works
  perfectly. The stated justification — that `engines` tells them at install
  time rather than at runtime — does not hold by default: npm emits EBADENGINE
  as a *warning* and installs anyway, so the narrowing does not prevent the
  runtime error it was meant to pre-empt, while it does hard-fail installs
  under `engine-strict=true`.
- **`require()` returns a frozen module namespace,** which the README's new
  Module Formats section does not mention. Verified on this build:
  `Object.isExtensible` is false and `playFile` is `configurable: false`, so
  `Object.defineProperty` throws `Cannot redefine property` — which is what
  `jest.spyOn(require('rtsp-backchannel'), 'playFile')` and `sinon.stub` do.
  Plain assignment throws in strict mode and **silently no-ops** in sloppy CJS.

## Decision

Keep the single ESM artifact and describe its limits precisely: restore
`engines.node` to `>=22`, document that the `require` path needs 22.12+ and
that TypeScript CJS consumers need `nodenext`/`bundler` resolution, and
document the frozen-namespace consequence for test doubles. Separately, close
the `exports` map with `"default"` and `"./package.json"`.

## Alternatives Considered

### Document the real boundaries; keep one artifact — chosen
- **Description:** No build change. `engines` covers what ESM needs; prose covers what CJS additionally needs.
- **Pros:** Keeps the property the original decision was built for — one artifact means the dual-package hazard is impossible, not merely unlikely. Costs ESM-only users nothing. `nodenext`/`bundler` is where `require(esm)` support lives and is the setting new projects get.
- **Cons:** A `node16` consumer must change one tsconfig line before they can compile, and they learn that from the README rather than from a working build.

### Ship a `.d.cts` for the `require` condition
- **Description:** `require: { types: './dist/index.d.cts', default: './dist/index.js' }`.
- **Pros:** `node16` consumers compile with no tsconfig change.
- **Cons:** Declares CJS semantics for a file that is ESM — the pattern `attw` flags as false-CJS. Doing it honestly needs the whole declaration tree emitted twice, not one file, because a `.d.cts` that re-exports `./index.js` lands back on ESM declarations. That is a build-system change (a second tsc pass or a tool like tshy) for a tsconfig setting the consumer can flip, and it would state something untrue about the artifact.

### Full dual CJS+ESM build
- **Description:** Emit real CommonJS alongside ESM.
- **Pros:** Every consumer configuration works with no caveats.
- **Cons:** Reintroduces the dual-package hazard the original decision existed to eliminate — two module instances in one process, `instanceof` failing across them. Explicitly rejected there; nothing has changed to reopen it.

### Keep `engines.node` at `>=22.12.0`
- **Description:** One supported floor for both entry points.
- **Pros:** A CJS user on 22.5 gets a warning at install rather than a runtime error.
- **Cons:** Only a warning by default, so it does not actually prevent that error; meanwhile it hard-fails installs for ESM-only users on 22.0–22.11 under `engine-strict=true`, which is common in CI images. An additive feature should not remove support from users who never touch it.

## Reasoning

The original decision traded consumer convenience for the guarantee that only
one module instance can ever exist, and that trade is still right — so the fix
belongs in what the package *claims*, not in what it ships. Between the two
`engines` options, one warns a minority and the other breaks a majority's
installs, and the warning does not even deliver the benefit it was claimed to.
The `.d.cts` route is the only alternative that would genuinely improve a
consumer's experience, and it pays for that with a duplicated declaration tree
plus a statement about the artifact that is not true.

## Trade-offs Accepted

- `moduleResolution: node16` remains unsupported for CJS type-checking. It is
  documented in both READMEs at the point the `require` example appears, and
  `attw` would still report CJSResolvesToESM for anyone auditing the package.
- A CJS consumer on Node 22.0–22.11 installs cleanly and then fails at runtime
  with `ERR_REQUIRE_ESM`. The README states the 22.12 floor for that path; the
  alternative punished a larger group for it.
- Consumers cannot stub the library's exports through `require()`. Documented
  with the working alternative (stub the module the library calls, or use
  `esmock`/loader mocks), but it is a real limitation of `require(esm)`.

## Related Code Paths

- `package.json` — `engines.node`, `exports["."].default`, `exports["./package.json"]`
- `README.md`, `README.ko.md` — Module Formats and Requirements
- `src/index.test.ts` — manifest assertions, and the README requirement line derived from `engines.node` rather than hard-coded

## Consequences

- The supported matrix is now stated once and asserted: ESM on Node 22+, CJS on
  Node 22.12+, TypeScript CJS on `nodenext`/`bundler`.
- If the package ever adds a default export, the frozen-namespace and
  interop notes need revisiting, since named-export-only is what makes
  `require(esm)` behave close enough to CJS to document simply.
- Revisiting the `.d.cts` route only makes sense together with a build-tool
  change; it should not be bolted on as a single hand-written file.

## Additional Decisions

*(Recorded here rather than as their own file: the session's decision-log budget
was spent, and this one shares the concern above — what the published artifact
can be statically analysed to contain.)*

### Pacing moves to its own module instead of keeping the dynamic-import workaround

**Context.** `backchannel.ts` reached its VIGI transport through
`await import('./vigi/talk.ts')`. That was not a style choice: `talk.ts`
computed a top-level constant from `backchannel.ts`'s `SAMPLE_RATE`, so a
static import formed a load-time cycle. Reproduced by converting it — a process
that starts with `backchannel.ts` throws
`Cannot access 'SAMPLE_RATE' before initialization`, and `backchannel.test.ts`
fails outright.

**Decision.** Extract `SAMPLE_RATE`, `PACKET_MS`, `PacingClock`,
`sendPacedFrames`, `sendPacedG711` and the internal clock into
`src/audio/pacing.ts`, have both transports take them from there, and make the
import static. `backchannel.ts` re-exports the public names, so the emitted
`dist/index.d.ts` is byte-identical.

**Alternatives.** Keeping the dynamic import leaves the cycle in place for
whoever adds the next such import, and puts an unanalysable runtime `import()`
on the VIGI open path — a hazard for bundlers and for the `require` entry point
this document is about. Moving only `SAMPLE_RATE` would have split the pacing
primitives across two modules for no gain, since `sendPacedFrames` and
`sendPacedG711` are what `talk.ts` is actually there for.

**Trade-offs.** One more module in `src/audio/`, and `backchannel.ts` now
re-exports rather than defines four public names — an indirection a reader has
to follow. Accepted because the alternative is a cycle that only stays harmless
while nobody touches it.

### The ONVIF backchannel probe gets its own seam rather than routing through `createDevice`

**Context.** `defaultProbeOnvifBackchannel` constructed `OnvifDevice` and
`RtspClient` directly, so the module's `createDevice` injection point did not
apply to it. Its behaviour — profile selection, DESCRIBE handling, socket
cleanup — had no test coverage at all, and `capabilities.test.ts` had to stub
the whole probe out to keep the network out of unrelated tests.

**Decision.** Give the probe its own narrow dependencies rather than widening
`createDevice`.

**Reasoning.** `CameraCapabilityDevice` exposes only `connect`,
`connectedMediaUrl` and `serviceCall`; the probe needs `getProfiles` and
`getStreamUri`. Routing it through `createDevice` would mean adding both to an
interface the main path never calls them on, so every existing test double
would have to grow two methods to satisfy a caller it does not exercise. A
separate seam keeps each interface to what its own caller uses.

**Trade-offs.** Two device-construction seams in one module instead of one, and
a reader has to notice that `createDevice` deliberately does not cover the
probe. Mitigated by a comment at the probe saying so and why.
