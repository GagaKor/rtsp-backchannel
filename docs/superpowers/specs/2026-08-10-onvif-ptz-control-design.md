# ONVIF PTZ Control Design

**Date:** 2026-08-10
**Status:** Approved
**Target release:** 0.3.0

## Goal

Add PTZ movement control — continuous, absolute, and relative moves plus stop
and status — to the TypeScript, Python, and Rust libraries, as a session-based
API that reuses the existing authenticated ONVIF client. The camera capability
report added earlier already tells a caller exactly which of these a given
camera supports; this design turns that report into the precondition for
control.

## Non-goals

- Presets (`GotoPreset`, `SetPreset`, `RemovePreset`) and home position.
- Imaging control (brightness, exposure, focus).
- `SendAuxiliaryCommand` (wiper, IR illuminator).
- Event subscription. Event reporting was removed from the capability report by
  an earlier decision and is not reintroduced here.
- The publish pipeline. Auto-publishing to npm, PyPI, and crates.io is a
  separate subsystem and gets its own spec after this ships.

## Scope

Five operations, on the PTZ service (`http://www.onvif.org/ver20/ptz/wsdl`):

| Operation | Purpose |
|---|---|
| `ContinuousMove` | Move at a velocity until stopped or the device timeout expires |
| `Stop` | Halt pan/tilt and/or zoom |
| `AbsoluteMove` | Move to a coordinate |
| `RelativeMove` | Move by an offset from the current position |
| `GetStatus` | Read current position and move state |

## Architecture

Each implementation gains one control module beside its capability parser:

```
src/onvif/ptz.ts                 + src/onvif/ptz.test.ts
python/rtsp_backchannel/ptz.py   + python/test_onvif_ptz.py
rust/src/onvif/ptz.rs
```

Control reuses the existing transport unchanged: `OnvifDevice`, WS-Security
PasswordDigest with clock-offset correction, and the same-origin XAddr
validation that already guards every advertised service endpoint. Control is
not a new transport — it is a different SOAP body on the existing pipe.

**Transport entry point rename — TypeScript and Python only.** Those two expose
their authenticated SOAP entry point as `readOnlyCall` / `read_only_call`.
Control requests use the same entry point, which would make that name false, so
rename it to `serviceCall` / `service_call`. This is safe: neither the method
nor the `CameraCapabilityDevice` interface that declares it is exported from
`src/index.ts` or from the Python package `__init__`, so no published API
changes. Rust needs no rename — it already calls the neutrally named
`soap_response(endpoint, body, authenticated)` on the device directly. Read-only
guarantees move from the name to the operations a caller invokes.

**Module independence.** `capabilities` and `ptz` do not import each other.
Both depend only on the device client. The `PtzNode` and `PtzSpaces` value
types are currently declared in the capability module and re-exported publicly
in all three languages — including Rust's
`pub use capabilities::{… PtzNode, PtzServiceCapabilities, PtzSpaces …}`.
Extract them into a small shared types module that both modules import, keeping
every existing public export path byte-identical so this is not a breaking
change.

## Public API

```ts
openPtzSession({
  host, user, pass,
  profileToken?,           // default: first media profile carrying a PTZConfiguration
  deviceUrls?,
  timeoutMs?,              // per-request transport timeout
  defaultMoveTimeoutMs?,   // ContinuousMove Timeout, default 1000
}): Promise<PtzSession>

interface PtzSession {
  readonly profileToken: string;
  readonly node: PtzNode;                       // cached capabilities

  continuousMove({ panTilt?: { x, y }, zoom?, timeoutMs? }): Promise<void>;
  absoluteMove({ panTilt?: { x, y }, zoom?, speed? }): Promise<void>;
  relativeMove({ panTilt?: { x, y }, zoom?, speed? }): Promise<void>;
  stop({ panTilt?, zoom? }): Promise<void>;     // both default to true
  getStatus(): Promise<PtzStatus>;
  close(): Promise<void>;
}
```

Python and Rust expose the same operations with their own naming conventions
(`open_ptz_session`, `continuous_move`, …), matching how `discover_devices` and
`get_camera_capabilities` are already mirrored across the three packages.

A session is used rather than standalone per-call functions because
`ContinuousMove` now carries a short device timeout and must be re-issued to
keep moving. Reconnecting per call would cost three SOAP round trips each time,
which makes joystick-style control unusable. The library already has this
pattern in `openBackchannel`.

### Session open sequence

1. `connect()` — the existing three-call sequence.
2. `GetServices` — locate the PTZ service XAddr.
3. `GetNodes` — cache the node's supported spaces, and fail if PTZ is absent.
4. Resolve the profile token, either from `profileToken` or from the first
   media profile carrying a `PTZConfiguration`.

Opening fails if the device advertises no PTZ service. Every later call is
checked against the cached capabilities without another round trip.

### Coordinates and velocities

Values are ONVIF generic-space normalized: pan/tilt position and all velocities
are `-1.0..1.0`, zoom position is `0.0..1.0`. Out-of-range values are rejected
before a request is sent. Cameras clamp differently, and silently forwarding an
out-of-range value produces behaviour that cannot be reproduced across devices.

### `PtzStatus`

Exposes position, move status, and UTC time, and nothing else. The ONVIF
`GetStatus` response also carries a camera-supplied `Error` string; it is
deliberately dropped, not surfaced in any form, because every error and warning
message in this library is required to be free of response bytes, credentials,
hostnames, and URLs.

## Safety model

Runaway movement is the real risk in this feature. Three independent layers:

1. **Device-side timeout.** Every `ContinuousMove` carries a `Timeout` (default
   1 s). If the client process dies or the network drops, the camera stops on
   its own. Callers re-issue to keep moving.
2. **Stop on close.** `close()` sends `Stop` before releasing. That `Stop` is
   best-effort: if it fails while the session is closing because of an earlier
   error, the original error wins and is not masked.
3. **Pre-send capability guard.** An operation the cached node does not support
   is rejected with a fixed message and **no request leaves the process**.

## Error handling

Existing invariants carry over unchanged: fixed error strings, never reflecting
response bytes, credentials, usernames, hostnames, or URLs. Guard rejections use
their own fixed messages (for example, absolute pan/tilt not supported). SOAP
faults from the PTZ service are classified the same way the capability path
classifies them.

## Testing

Control inverts the risk of the read-only work. A wrong read request produced
wrong data; a wrong control request moves a physical camera. Testing therefore
pins requests, not just responses.

- **Request-body parity.** The shared fixture gains the expected SOAP body for
  each operation, and all three implementations must emit byte-equivalent
  requests. Three implementations that each send slightly different XML would
  produce a camera that misbehaves on one language only — the least
  reproducible class of bug available here.
- **Timeout is mandatory.** A test asserts every `ContinuousMove` carries a
  `Timeout`. Omitting it is the runaway case, so it is enforced rather than
  documented.
- **Close sends Stop**, and a failing Stop during close does not overwrite the
  original error.
- **Guarded calls send nothing.** For unsupported operations the fake server
  must record zero requests. Asserting that the call rejected is not enough;
  what matters is that nothing reached the network.
- **Range rejection** happens before any request.

### What real hardware can and cannot verify

The available test camera (vht VNV84371MR) reports `ptz.detected: false`, so
movement cannot be exercised. It can still verify the negative path: against a
real camera with no PTZ service, `openPtzSession()` must fail cleanly rather
than hang, crash, or emit a vague error. That check runs against real hardware.

Actual pan, tilt, and zoom behaviour ships unverified against hardware.

## Experimental marking

PTZ movement ships as **experimental** in 0.3.0. The boundary is stated
explicitly, in both the six READMEs and the API doc comments (JSDoc, docstrings,
rustdoc) so that it appears in editor tooltips and on docs.rs — most consumers
never read the README:

- Verified: session open, capability caching, guard rejection, request-body
  correctness, timeout inclusion, stop-on-close.
- Unverified: that a camera physically moves as intended.

When a PTZ camera becomes available, the movement path is verified and the
experimental marking is removed.

## Release sequencing

PR #24 (0.3.0 release preparation) is open, CI-green, and reviewable. Merge it
first, then add the control entry to the changelog as part of this work. Holding
it open only widens the gap with master.

## Decision Journey

### Initial Request

Add control for the capabilities the report already queries — PTZ and zoom —
into the 0.3.0 release, and add a pipeline that publishes to npm, PyPI, and
crates.io on merge to master.

### Plan Evolution

- **Decomposed into two specs.** Control and the publish pipeline are
  independent subsystems with different files, risks, and verification. The
  pipeline gets its own spec after this ships.
- **Control sequenced first**, per the request to include it in 0.3.0.
- **Scope trimmed** from the full PTZ service to movement and status only.
  Presets, home, imaging, and auxiliary commands were considered and dropped.
- **Auto-publish trigger flagged for the follow-up spec.** Publishing on every
  merge to master would publish on Dependabot dependency bumps, and npm, PyPI,
  and crates.io all forbid republishing a version. A tag trigger achieves the
  same goal without that failure mode. Deferred to that spec, not decided here.
- **Experimental release accepted** rather than blocking the feature until PTZ
  hardware is available.

### Outcome

A movement-only PTZ control API, session-based, guarded by the capability report,
with runaway protection at three layers, shipping in 0.3.0 with the
hardware-unverified portion explicitly marked.
