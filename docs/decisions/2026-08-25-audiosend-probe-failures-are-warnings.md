# audioSend probe failures never abort the capability report

**Date:** 2026-08-25
**Status:** Accepted
**Scope:** `src/onvif/capabilities.ts` — the `audioSend` probe block and the `warn` helper

## Context

`getCameraCapabilitiesWithDependencies` collects every optional ONVIF fact
behind a `try`/`catch` that funnels into a single `warn(operation, error)`
helper. That helper deliberately re-throws when `isAuthenticationFailure(error)`
holds: if the credentials are wrong, every remaining call will fail too, so
failing fast with a real auth error beats handing back a report that is nothing
but warnings.

Adding the `audioSend` probes put an *optional, default-on* network probe
behind that same helper. An ONVIF account that can `connect()` and read device
info but is denied `GetStreamUri` — a routine role restriction — now makes
`defaultProbeOnvifBackchannel` throw `ter:NotAuthorized`, which `warn` re-throws,
which rejects the whole promise and discards a fully-assembled report. Before
the probe existed the same camera returned a complete report, so this is a
regression introduced by an additive feature.

## Decision

Introduce a separate `warnOnly(operation, error)` helper that records a warning
and never re-throws, and use it for the two `audioSend` probes. `warn` keeps its
fail-fast behaviour for every genuine ONVIF service call.

## Alternatives Considered

### (a) A separate `warnOnly` helper used only by the probe paths — chosen
- **Description:** Two named helpers with different contracts; the probe block calls the non-throwing one.
- **Pros:** The difference in intent is visible at the call site. `warn`'s fail-fast contract for real service calls is untouched, so no existing behaviour shifts. Smallest possible blast radius.
- **Cons:** Two similar-looking helpers in one function; a future contributor could reach for the wrong one.

### (b) Add a `rethrowAuth` boolean parameter to `warn`
- **Description:** `warn(operation, error, { rethrowAuth: false })` at the probe call sites.
- **Pros:** One helper, no duplication.
- **Cons:** A boolean at the call site reads as configuration rather than as a
  different contract, and the default is the dangerous one — a new probe added
  later silently inherits the aborting behaviour. Boolean-parameter call sites
  also read poorly against the ~10 existing plain `warn` calls.

### (c) Narrow `isAuthenticationFailure` so probe-shaped errors are not classified as auth failures
- **Description:** Teach the classifier to distinguish credential rejection from authorization/role denial.
- **Pros:** Fixes the root confusion — `ter:NotAuthorized` on one operation genuinely is not "your password is wrong".
- **Cons:** Changes behaviour for every existing caller of `warn`, including the
  paths where fail-fast is the whole point, and the ONVIF fault vocabulary does
  not cleanly separate the two cases across vendors. Too wide a change to make
  as a side effect of an audioSend fix.

## Reasoning

The device client has already completed `connect()` successfully by the time the
probes run, so the credentials are known to be accepted — an auth-classified
failure inside a probe is a *permission* fact about one operation, not evidence
that the session is unusable. At that point the report is also already fully
assembled, so there is nothing left to fail fast *for*: discarding it destroys
work that succeeded in order to report a problem with an optional extra.

(a) was chosen over (c) because the correct scope of this change is the probe
block, not the classifier every other call site depends on; (c) remains the
right long-term cleanup but belongs in its own change with its own tests. (a)
was chosen over (b) because the safe behaviour should be requested by name
rather than by a flag whose default is the aborting one.

## Trade-offs Accepted

- Two similarly-named helpers coexist in one long function. Mitigated by a
  comment on `warnOnly` stating exactly which call sites may use it and why.
- An operator whose ONVIF password is correct but whose *role* cannot reach
  `GetStreamUri` now sees a warning rather than a hard error, so a
  misconfigured role is slightly easier to overlook. The warning carries the
  fault code, and a silently-truncated report is the worse failure.
- The root confusion in `isAuthenticationFailure` (credential rejection vs.
  authorization denial) is left in place.

## Related Code Paths

- `src/onvif/capabilities.ts` — `warnOnly` added next to `warn`; the two `audioSend` probe `catch` blocks switched to it
- `src/onvif/capabilities.test.ts` — covers an auth-classified probe failure resolving to a warning instead of rejecting

## Consequences

- Any future optional probe added to this function must use `warnOnly`, not `warn`.
- `getCameraCapabilities` regains the invariant that a successful `connect()`
  always yields a report; only `connect()` itself and genuinely
  credential-fatal service calls can reject.
- Follow-up worth doing separately: split `isAuthenticationFailure` into
  "credentials rejected" and "operation not permitted", which would let `warn`
  make this distinction without a second helper.
