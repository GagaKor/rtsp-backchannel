# Propagate GetCapabilities failures out of connect() instead of falling back to a derived Media URL

**Date:** 2026-08-07
**Status:** Accepted
**Scope:** TypeScript ONVIF device client (`src/onvif/deviceClient.ts`), cross-language connect() contract

## Context

`OnvifDevice.connect()` runs three requests: `GetSystemDateAndTime`, `GetDeviceInformation`, then
`GetCapabilities` to locate the Media service. The TypeScript implementation wrapped only the third
call in `try { ... } catch { /* fall through to derived URL */ }`, so a 401, 403, or SOAP Fault on
`GetCapabilities` was silently discarded and `connect()` succeeded with a guessed
`device_service` → `media_service` URL. A camera rejecting our credentials therefore produced a
"connected" client whose later media calls failed for reasons the caller could not attribute to auth.
A failing test added during the SOAP-Fault hardening pass required these third-stage failures to fail
the connect.

## Decision

Remove the `try`/`catch` entirely so every `GetCapabilities` error propagates out of
`discoverMediaUrl()` and fails `connect()`. The derived `media_service` URL is produced only when the
call *succeeds* but the response carries no `<XAddr>`.

## Alternatives Considered

### Propagate everything (chosen)

- **Description:** Delete the swallow; any non-2xx status or SOAP Fault on `GetCapabilities` aborts the connect.
- **Pros:**
  - Matches what Python and Rust already do, so all three implementations share one connect contract.
  - Auth failures surface at connect time rather than as a confusing later media failure.
  - Simplest code — no error classification logic in the connect path.
- **Cons:**
  - A camera that answers `GetCapabilities` with 400/404 but is otherwise usable can no longer connect.

### Propagate only auth failures and SOAP Faults, keep falling back otherwise

- **Description:** Inspect the failure and re-throw on 401/403/Fault, but keep deriving the Media URL for 404, 400, malformed responses, and transport errors.
- **Pros:**
  - Preserves tolerance for cameras with a broken or partial `GetCapabilities`.
  - Still satisfies the new hardening tests, which only exercise 401/403/Fault.
- **Cons:**
  - `soap()` collapses every non-2xx into a single fixed `ONVIF HTTP response error` message with no status code, precisely so error strings never reflect response bytes. Recovering the status code for classification means bypassing `soap()` and calling `soapResponse()` directly in `discoverMediaUrl`, duplicating the status/fault decision logic that was just centralised.
  - Would leave TypeScript as the only implementation with a lenient connect, re-opening the cross-language divergence this branch is trying to close.

## Reasoning

The deciding factor was cross-language parity, not the tests. Both other implementations already
propagate: Python's `_media_service_url` calls `_call_response` with no `except`, and Rust's
`connect` uses `self.soap(...)?`. TypeScript was the lone outlier, and the lenient alternative would
have preserved that divergence while also forcing `discoverMediaUrl` to re-derive status information
that `soap()` deliberately discards to keep error messages free of response bytes. The tolerance we
give up is speculative — `GetCapabilities` is mandatory in the ONVIF Device service — whereas the
silent-auth-failure bug it was masking is concrete.

## Trade-offs Accepted

- Cameras whose `GetCapabilities` is broken in a non-auth way can no longer connect at all, where they previously connected against a guessed Media URL. No such camera is known; if one appears, the fix belongs in all three implementations at once, not in TypeScript alone.
- `connect()` still reports every failure as `ONVIF connect failed: request failed`, so the caller learns that auth failed only from the fact that connect failed, not from the message. That is the existing non-reflection design (`safeConnectCause`) and was not changed here; `capabilities.ts` does its own 401/403 and fault-code classification on the `readOnlyCall` path.

## Related Code Paths

- `src/onvif/deviceClient.ts` — `discoverMediaUrl()` no longer catches; `soap()` now throws `ONVIF SOAP Fault` for Fault envelopes and `ONVIF HTTP response error` for other non-2xx.
- `python/rtsp_backchannel/onvif.py` — `_media_service_url()`, the already-strict reference behaviour.
- `rust/src/onvif.rs` — `connect()`, the already-strict reference behaviour.
- `src/onvif/capabilities.ts` — unaffected: it uses `readOnlyCall()` → `soapResponse()` and classifies auth failures itself via `parseReadOnlyResponse` and `isAuthenticationFailure`. (Note: `readOnlyCall`/`parseReadOnlyResponse` were renamed to `serviceCall`/`parseServiceResponse` on 2026-08-10; this line names them as they were when this decision was recorded.)

## Consequences

- The three implementations now share one connect contract, which the parity fixture work in the plan's Task 8 depends on.
- Any future relaxation of connect tolerance must be made in all three languages together to keep the contract.
- `soap()` is now the single place that decides status/fault handling for the legacy path; new legacy operations inherit the behaviour without their own error handling.
