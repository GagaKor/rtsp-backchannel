# Drop event reporting from the camera capability report

**Date:** 2026-08-07
**Status:** Accepted
**Scope:** Capability report in all three implementations (TypeScript, Python, Rust), plus the shared parity fixture and all six READMEs

## Context

The capability feature was scoped in brainstorming as "PTZ presence + declared
conformance profiles, events excluded." The implementation plan and all three
implementations nevertheless shipped an `events` member carrying Events service
capabilities and the full `GetEventProperties` topic set — roughly 750
references across code, tests, and docs. The divergence surfaced during review
after the work was already green and committed. The project owner then
restated the original scope: the report does not need to show which events a
camera supports.

## Decision

Remove event reporting entirely from the capability report in all three
implementations: the `events` report member, the Events `GetServiceCapabilities`
and `GetEventProperties` requests, the topic-set tree walk, and the four
event-only retention budgets. The Events service remains listed in the generic
`services` inventory, because that is service discovery rather than event
reporting.

## Alternatives Considered

### Keep the events code (recommended at the time, overruled)

- **Description:** Leave the shipped events reporting in place as extra information.
- **Pros:**
  - The code was already green, reviewed, and covered by tests in all three languages.
  - `GetEventProperties` is only sent when the Events service is advertised, so cameras without events pay nothing.
  - Zero regression risk; no surgery on three working implementations.
- **Cons:**
  - The public report carries a member nobody asked for, and every consumer has to understand and ignore it.
  - The API surface and the documented field list stay larger than the agreed scope.

### Remove events entirely (chosen)

- **Description:** Delete the report member, the two SOAP calls, the topic parser, the budgets, their tests, and their documentation.
- **Pros:**
  - The shipped API matches the agreed scope; the report answers exactly the question it was built for.
  - Removes the largest and most intricate parser in the feature — an arbitrary-depth topic tree with four separate size budgets — and with it that parser's attack surface.
- **Cons:**
  - Touches three working implementations at once, with real regression risk in code that was already green.
  - Discards working, tested functionality that a future requirement might want back.

### Keep `events.detected`, drop only the topic set

- **Description:** Report whether an Events service exists but never enumerate topics.
- **Pros:** Removes the heavy parser while keeping a one-bit fact.
- **Cons:** `services` already records that the Events service was advertised, so `events.detected` would have been a second, redundant spelling of the same fact.

## Reasoning

Scope was the deciding factor, not code quality. The owner set the scope twice —
once in brainstorming and again after seeing the shipped report — and the
implementation had drifted from it, so the drift is what needed correcting. The
third option collapsed on inspection: `services` already carries the Events
service entry, so keeping `events.detected` would have duplicated it. Removing
the topic parser also retires the feature's most intricate untrusted-input path,
which is a real if secondary benefit.

## Trade-offs Accepted

- Working, tested code was deleted rather than left in place. If event topics are wanted later they must be rebuilt, though the git history on this branch preserves a complete working implementation to restore from.
- Three implementations were edited concurrently, which is where regression risk lives. Mitigated by the shared parity fixture: it was updated once, up front, and every language's test suite had to agree with the new shape.

## Related Code Paths

- `src/onvif/capabilities.ts`, `src/index.ts` — TypeScript report and exports.
- `python/rtsp_backchannel/capabilities.py`, `python/rtsp_backchannel/cli.py`, `python/rtsp_backchannel/__init__.py` — Python report, CLI JSON, exports.
- `rust/src/onvif/capabilities.rs`, `rust/src/onvif.rs` — Rust report and re-exports.
- `rust/tests/fixtures/capability-parity.json` — shared fixture; `expectedReport.events` and the two `Events*` operations removed first so all three suites had one target.
- All six READMEs — field list, limit sentence, and embedded JSON sample.

## Consequences

- `CameraCapabilityReport` is now `device`, `scopes`, `declaredProfiles`, `serviceDiscovery`, `services`, `profiles`, `ptz`, `media2`, `warnings` in every language.
- The event-only retention budgets (1,024 topics, 4,096-byte path, 2,048-byte namespace, 256 KiB aggregate) are gone from code, docs, and the README-content assertions that pinned them.
- The shared 64-level XML depth guard survives, but in Python and Rust the only test driving it to the boundary was the deleted event-topic test. Both were rewritten against surviving parsers rather than dropped; TypeScript already had independent coverage in `src/onvif/xml.test.ts`. Any future removal of a parser should check whether it was the last thing exercising a shared guard.
- `docs/superpowers/plans/2026-08-06-onvif-camera-capabilities.md` still describes events throughout. It carries a scope-change note at the top rather than being rewritten, so it remains an accurate record of what was planned and built.
