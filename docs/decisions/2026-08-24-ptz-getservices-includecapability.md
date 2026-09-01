# Send IncludeCapability in the PTZ session's GetServices instead of the bare element

**Date:** 2026-08-24
**Status:** Accepted
**Scope:** PTZ session open path in all three implementations (`src/onvif/ptz.ts`, `python/rtsp_backchannel/ptz.py`, `rust/src/onvif/ptz.rs`)

## Context

`openPtzSession` had never been run against real PTZ hardware — the README's PTZ section said so
explicitly ("Unverified: that a camera physically moves as intended — no PTZ hardware was
available"). The first such camera became available on 2026-08-24: a TP-Link VIGI C540V (firmware
2.2.0 Build 250904 Rel.60109n) at `10.128.10.115`, reached after a WS-Discovery sweep — the address
originally reported for it, `10.128.20.115`, had no device on it at all (ARP incomplete, every
common port closed, while neighbours on the same `/24` answered normally), and the supplied
credentials authenticating against `10.128.10.115` identified the real host.

`openPtzSession` failed immediately on that camera with `PtzResponseError: SOAP Fault: Fault`, raised
by the `operationResponse` call that parses `GetServices`. `capabilities.ts`'s `getCameraCapabilities`
succeeded against the same camera at the same moment, so the failure was specific to the PTZ path.

The two modules were sending different bodies for the same operation:

- `capabilities.ts`: `<GetServices><IncludeCapability>true</IncludeCapability></GetServices>`
- `ptz.ts`: `<GetServices/>`

Direct probing confirmed the camera's behaviour: the bare form returns HTTP 400 with a
`SOAP-ENV:Sender` Fault, and `IncludeCapability=false` returns HTTP 200 with 22 services.
`IncludeCapability` is `minOccurs=1` in the ONVIF Device WSDL, so the bare form was never
spec-conformant — the camera is strict, not broken.

The camera was later updated to firmware 2.3.3 Build 260713 and re-probed: the bare form still
returns HTTP 400, now with subcode `ter:InvalidArgVal`. The strictness is current TP-Link
behaviour, not an artefact of one stale build.

## Decision

Send `<IncludeCapability>false</IncludeCapability>` from the PTZ session's `GetServices` in all three
implementations. `false`, not `true`: this path only needs the XAddr list, which is exactly what the
original "plain form" comment was trying to achieve.

## Alternatives Considered

### Add the element with `false` (chosen)

- **Description:** Emit the mandatory child, asking for no capability payload.
- **Pros:**
  - Spec-conformant, so it works on strict and lenient stacks alike.
  - Preserves the original intent — no capability data on the wire, no pressure on the shared 1MB body cap.
  - One-line change per language, no new code paths.
- **Cons:**
  - A few bytes larger than the bare form.

### Reuse `capabilities.ts`'s `IncludeCapability=true` body

- **Description:** Share one constant across both modules.
- **Pros:**
  - A single definition, no chance of the two drifting again.
- **Cons:**
  - Inflates the response with capability XML that the PTZ path discards, against the shared 1MB body cap — the exact cost the original comment called out.

### Catch the Fault and retry with the element present

- **Description:** Keep the bare form, fall back on failure.
- **Pros:**
  - No behaviour change for cameras that already accept the bare form.
- **Cons:**
  - Two round trips on every strict camera, for a request that was simply malformed.
  - Adds a retry branch to a path that has no other retry logic.

## Reasoning

The bare form was not a tolerated shorthand that one strict camera happened to reject — it omitted a
required element. Once that is established, the only question is which boolean to send, and `false`
matches what this module actually wants. `capabilities.ts` already proved on the same camera, in the
same session, that the compliant form works, so there was no need to guess or probe further.

## Trade-offs Accepted

- The stale comment "Plain form, unlike capabilities.ts's IncludeCapability=true" was replaced rather than kept; the new comment records why the element cannot be dropped, so the optimisation is not re-attempted.
- No regression test asserts the failure mode itself — the fake devices in all three suites match on the request body, so they were updated in lockstep and would catch a revert, but nothing reproduces a strict camera's 400.

## Related Code Paths

- `src/onvif/ptz.ts` — `GET_SERVICES`; the TypeScript fake in `src/onvif/ptz.test.ts` matches on the same literal.
- `python/rtsp_backchannel/ptz.py` — `_GET_SERVICES`; `python/test_onvif_ptz.py` mirrors it.
- `rust/src/onvif/ptz.rs` — `GET_SERVICES`; the in-file test server compares against the same const.
- `src/onvif/capabilities.ts` — the already-correct reference body.

## Consequences

- `openPtzSession` now opens successfully against the VIGI C540V, which unblocked the first real-hardware validation of the PTZ path (see `2026-08-24-ptz-duration-integer-seconds.md` for the second defect that validation surfaced).
- The three implementations still send byte-identical PTZ session bodies, so the cross-language parity contract is unchanged.
