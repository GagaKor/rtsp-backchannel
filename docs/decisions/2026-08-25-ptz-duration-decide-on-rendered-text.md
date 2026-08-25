# The whole-second test is applied to the rendered duration, not the raw quotient

**Date:** 2026-08-25
**Status:** Accepted
**Scope:** `formatPtzDuration` in all three ports — `src/onvif/ptz.ts`, `python/rtsp_backchannel/ptz.py`, `rust/src/onvif/ptz.rs`

## Context

`2026-08-24-ptz-duration-integer-seconds.md` established that whole seconds must
be emitted as `PT1S`, because the VIGI C540V rejects any decimal point with
`ter:InvalidArgVal`. All three ports implemented it by testing the raw quotient
for integrality and then rendering with three decimals. Testing one value and
printing another leaves two gaps:

- `formatPtzDuration(999.9999)` — the quotient `0.9999999` is not an integer, so
  it takes the fractional branch, where `toFixed(3)` renders `1.000`. The output
  is `PT1.000S`: exactly the spelling the earlier decision exists to avoid, for
  a value the accepted spelling could have expressed.
- `formatPtzDuration(0.4)` passes the `<= 0` guard and renders `PT0.000S`. That
  is a zero device-side stop deadline — the runaway guard removed entirely,
  which the earlier decision explicitly refused to accept when it rejected
  rounding down to `PT0S`.

Both reproduce identically in TypeScript, Python and Rust.

## Decision

Render first, then decide: compute the three-decimal text once, and emit the
integer form when its fractional part is all zeros. Separately, raise the lower
bound to 1 ms so a duration that cannot render as a non-zero guard is rejected
rather than emitted.

## Alternatives Considered

### Decide on the rendered text — chosen
- **Description:** `text = (ms/1000).toFixed(3)`; if the fraction is `000`, print the whole part alone.
- **Pros:** One rule closes both gaps, because the value that is tested is the value that is printed. `999.9999` becomes `PT1S`, which the strict camera accepts. No second rounding rule to keep in sync across three languages — verified that JS `toFixed(3)`, Python `f"{:.3f}"` and Rust `format!("{:.3}")` agree on every case here.
- **Cons:** A sub-microsecond difference between request and wire value (999.9999 ms is sent as 1 s). Inherent to a three-decimal format, not introduced here.

### Test `milliseconds % 1000 == 0` instead
- **Description:** Ask whether the input is a whole number of milliseconds that divides evenly into seconds.
- **Pros:** Reads directly as "is this a whole second"; no string handling.
- **Cons:** Fixes only the second gap. `999.9999 % 1000` is non-zero, so it still renders `PT1.000S` — the same test-one-value-print-another split, just moved. Would need the lower-bound change *and* a separate rounding rule to be complete.

### Clamp sub-millisecond timeouts up to the minimum instead of rejecting
- **Description:** `ms = max(ms, 1)`.
- **Pros:** No caller ever sees a new error.
- **Cons:** Silently changes the caller's requested duration, which is the specific thing the 2026-08-24 decision refused to do when it declined to round 250 ms up to a second. A guard the caller did not ask for is the same class of surprise as a guard four times too long, and the existing `<= 0` and `> 60000` errors show this function already answers impossible input with a raised error.

## Reasoning

The bug in both gaps has one shape — a decision made on a number that is not
the number being written — so the fix belongs at that seam rather than in a
patch per symptom. Deciding on the rendered text also makes the function's
contract inspectable: whatever the caller passes, the emitted string is the
three-decimal rendering, and the only question is whether it needs the
fraction. Rejecting rather than clamping sub-millisecond input follows the
precedent already set by the neighbouring guards in the same function, and
keeps the "never silently alter the caller's duration" rule intact.

## Trade-offs Accepted

- A timeout between 0 and 1 ms now raises where it previously returned
  `PT0.000S`. That return value was a camera-side guard of zero, so the change
  turns a silent safety hole into a visible error; no meaningful caller is
  affected.
- Inputs within half a millisecond of a whole second are sent as that whole
  second. The three-decimal format already committed to this precision.
- The error message for the lower bound changes, so any caller matching on the
  old "greater than 0" wording sees new text.

## Related Code Paths

- `src/onvif/ptz.ts` — `formatPtzDuration` and `MIN_MOVE_TIMEOUT_MS`
- `python/rtsp_backchannel/ptz.py` — `format_ptz_duration`
- `rust/src/onvif/ptz.rs` — `format_ptz_duration`
- the three ports' PTZ test suites — shared edge cases at 0.4, 999.9999, 1, 1000, 60000 ms

## Consequences

- The three ports keep byte-identical output; the shared edge cases are now
  pinned in each suite so a future divergence fails a test rather than being
  found on hardware.
- Supersedes nothing: `2026-08-24-ptz-duration-integer-seconds.md` stays
  accepted, and this refines how its rule is applied.
