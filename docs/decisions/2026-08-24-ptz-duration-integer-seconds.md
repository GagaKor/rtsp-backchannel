# Emit whole-second PTZ move timeouts as PT1S rather than PT1.000S

**Date:** 2026-08-24
**Status:** Accepted
**Scope:** `format_ptz_duration` / `formatPtzDuration` in all three implementations, and the shared `ptz-request-parity.json` fixture

## Context

With the `GetServices` defect fixed (see `2026-08-24-ptz-getservices-includecapability.md`),
`openPtzSession` opened against the first real PTZ camera — a TP-Link VIGI C540V, firmware 2.2.0 —
and `relativeMove` worked on both axes. `continuousMove` then failed with a SOAP Fault carrying
subcode `ter:InvalidArgVal`.

Isolating the request body one element at a time against the camera showed the `Timeout` element was
the only cause, and that the camera's parser rejects a decimal point outright rather than a
particular magnitude:

| `<Timeout>` | Result |
|---|---|
| `PT0.500S` | HTTP 400, `ter:InvalidArgVal` |
| `PT0.800S` | HTTP 400, `ter:InvalidArgVal` |
| `PT1.000S` | HTTP 400, `ter:InvalidArgVal` |
| `PT2.500S` | HTTP 400, `ter:InvalidArgVal` |
| `PT0S` | HTTP 200 |
| `PT1S` | HTTP 200 |
| `PT2S` | HTTP 200 |

The same table was reproduced after the camera was updated to firmware 2.3.3 Build 260713, so this
is current behaviour rather than one stale build.

All three implementations formatted every timeout as `PT{seconds:.3f}S`, so the library's own default
of 1000 ms went out as `PT1.000S` — meaning `continuousMove` could never succeed on this camera at
any timeout value. `PT1.000S` is valid `xs:duration`; the camera is at fault. But the library has a
choice of two equally valid spellings for the common case, and only one of them is universally
accepted.

## Decision

Emit the fractionless form when the timeout is a whole number of seconds (`PT1S`, `PT2S`, `PT60S`),
and keep three-decimal seconds otherwise (`PT1.500S`, `PT0.250S`).

## Alternatives Considered

### Fractionless only for whole seconds (chosen)

- **Description:** Branch on `seconds` being integral; the fractional path is untouched.
- **Pros:**
  - The library default (1000 ms) and every round-second value a caller is likely to pick now work on strict cameras.
  - No semantic change: `PT1S` and `PT1.000S` denote the same duration, so lenient cameras see no difference in behaviour.
  - Sub-second timeouts keep their only faithful representation.
- **Cons:**
  - Two spellings exist for the element, so a test asserting a literal body must pick the right one per value.
  - Sub-second timeouts still fail on this camera — the library reports the camera's Fault rather than silently substituting a different duration.

### Round every timeout to whole seconds

- **Description:** Emit `PT{round(ms/1000)}S` always, so sub-second requests become `PT0S` or `PT1S`.
- **Pros:**
  - One spelling, works everywhere.
- **Cons:**
  - Silently changes the caller's requested duration. The `Timeout` element is the runaway guard for `continuousMove`; rounding 250 ms up to 1 s quadruples how long a camera keeps moving after a client dies, and rounding down to `PT0S` removes the guard entirely.
  - Hides a real camera limitation behind a value the caller never asked for.

### Trim trailing zeros (`PT0.8S`)

- **Description:** Emit the shortest exact decimal.
- **Pros:**
  - Smaller bodies; still exact.
- **Cons:**
  - Does not fix anything — this camera rejects the decimal point itself, not the trailing zeros. Confirmed by `PT0.500S` and `PT2.500S` both failing.

### Leave the formatter alone and document the limitation

- **Description:** Treat it as a camera bug and tell users to avoid `continuousMove` on strict stacks.
- **Pros:**
  - No code change; the library stays on one spelling.
- **Cons:**
  - `continuousMove` would be unusable on this camera at every timeout value, including the default, for a defect the library can sidestep with a spelling both forms of camera accept.

## Reasoning

The deciding constraint was not to alter the caller's requested duration. `Timeout` is the
device-side stop that makes a crashed or disconnected client safe, so any option that quietly
substitutes a different value trades a correctness property for compatibility. Choosing between two
exact spellings costs nothing, whereas rounding costs the guarantee. The whole-second branch covers
the default and the values callers actually pass; a caller who explicitly asks for 250 ms on a camera
that cannot express it gets an honest Fault rather than a silent 1-second move.

## Trade-offs Accepted

- Sub-second `continuousMove` remains impossible on cameras with this parser. That is a real limitation, surfaced as the camera's own Fault rather than papered over.
- The formatter now has a branch, and body-literal assertions in all three suites plus the shared parity fixture had to move from `PT1.000S` to `PT1S` together.

## Related Code Paths

- `src/onvif/ptz.ts` — `formatPtzDuration`; assertions in `src/onvif/ptz.test.ts`.
- `python/rtsp_backchannel/ptz.py` — `format_ptz_duration`; assertions in `python/test_onvif_ptz.py`.
- `rust/src/onvif/ptz.rs` — `format_ptz_duration` and its in-file tests.
- `rust/tests/fixtures/ptz-request-parity.json` — the cross-language `continuousMove` body, read by all three suites.

## Consequences

- The full PTZ surface is now confirmed on real hardware: `relativeMove`, `continuousMove`, and `absoluteMove` on both pan/tilt and zoom moved the VIGI C540V in the requested direction, `getStatus` tracked each move, and the camera returned exactly to its starting coordinates. The whole run was repeated unchanged on firmware 2.3.3.
- The "Unverified: that a camera physically moves as intended — no PTZ hardware was available" note was replaced in all four READMEs and in every `openPtzSession` doc comment with what was actually verified and on which model. PTZ stays marked experimental: one camera is not a fleet, and no optical-zoom or preset-tour model has been exercised.
