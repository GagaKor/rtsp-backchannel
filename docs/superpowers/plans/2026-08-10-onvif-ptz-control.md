# ONVIF PTZ Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-based ONVIF PTZ movement control — continuous, absolute, and relative moves plus stop and status — to the TypeScript, Python, and Rust libraries, guarded by the capability data each session caches at open.

**Architecture:** Each language gains one `ptz` module beside its capability parser. The module reuses the existing authenticated ONVIF client unchanged — same WS-Security PasswordDigest, same clock-offset correction, same same-origin XAddr validation. A session opens once (connect → GetServices → GetNodes), caches the node's supported spaces, and rejects unsupported operations before any request is built. Control is a different SOAP body on the existing pipe, not a new transport.

**Tech Stack:** Node.js 22 + TypeScript + `saxes`, Python 3.11+ standard-library `ElementTree`, Rust 1.86 + `roxmltree`/`reqwest`, SOAP 1.2, ONVIF PTZ service `ver20`.

**Spec:** `docs/superpowers/specs/2026-08-10-onvif-ptz-control-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **No new dependencies.** npm has exactly one runtime dependency (`saxes`); Python has none; Rust adds none. Do not add any.
- **PTZ namespace:** `http://www.onvif.org/ver20/ptz/wsdl`. **Schema namespace** (for `PanTilt`/`Zoom` vectors): `http://www.onvif.org/ver10/schema`. Both already exist as constants in each capability module.
- **Canonical number formatting — this is what makes cross-language request parity possible.** Every `x`, `y`, and zoom value in a SOAP body is formatted as a **fixed 6-decimal string**: `0.5` → `0.500000`, `-1` → `-1.000000`, `0` → `0.000000`. Never use a language's default float-to-string. Normalize negative zero to `0.000000`.
- **Canonical duration formatting:** `Timeout` is `PT{seconds}S` with **exactly 3 decimals**: 1000 ms → `PT1.000S`, 1500 ms → `PT1.500S`.
- **Value ranges:** pan/tilt position and all velocities are `-1.0..1.0` inclusive; zoom position is `0.0..1.0` inclusive. Reject out-of-range values before building a request. Reject non-finite values (NaN, infinity).
- **Error messages never reflect response bytes, credentials, usernames, hostnames, or URLs.** Use fixed strings. This invariant is enforced by existing tests across the repository; do not weaken it.
- **Default continuous-move timeout:** 1000 ms.
- **Experimental marking.** Every public PTZ symbol carries a doc comment stating that physical movement is unverified against hardware. Exact wording in Task 3.
- **No CLI.** PTZ control is a library API only in this release. Do not add a `ptz` CLI command.
- Run all three suites from the repository root. Python needs `PYTHONPATH=.:python`.

---

## Shared Operation Contract

**All three implementations build these exact bodies and use these exact
rejection messages.** Tasks 2, 4, and 6 each implement this section
independently in their own language; none of them may deviate. Task 8 pins it
with a shared fixture.

### Value types

Named per language convention (`PtzVector` / `PtzVector` / `PtzVector`,
`PtzSessionOptions`, `PtzStatus`), with these fields:

```typescript
interface PtzVector { x: number; y: number }

interface PtzSessionOptions {
  host: string;
  user?: string;
  pass?: string;
  profileToken?: string;        // default: first media profile with a PTZConfiguration
  deviceUrls?: string[];
  timeoutMs?: number;           // per-request transport timeout
  defaultMoveTimeoutMs?: number; // ContinuousMove Timeout, default 1000
}

interface PtzStatus {
  panTilt?: PtzVector;          // from Position/PanTilt
  zoom?: number;                // from Position/Zoom x
  panTiltMoveStatus?: string;   // MoveStatus/PanTilt text
  zoomMoveStatus?: string;      // MoveStatus/Zoom text
  utcTime?: string;             // UtcTime text
}
```

**`PtzStatus` deliberately has no error field.** The ONVIF `GetStatusResponse`
carries a camera-supplied `<Error>` string; drop it entirely and surface it in
no form. Every message this library emits must be free of response bytes, and
that element is response bytes. A test asserts a `GetStatus` response
containing `<Error>` text does not put that text anywhere in the returned
status.

### SOAP bodies

Single line, no whitespace between elements, attributes in the order written.
`TOK` is the XML-escaped profile token (use the `encodeXml` helper the device
client already applies to `GetStreamUri`). `PTZ_NS` is
`http://www.onvif.org/ver20/ptz/wsdl`; `SCHEMA_NS` is
`http://www.onvif.org/ver10/schema`.

A vector element is built as:

```
<TAG><PanTilt xmlns="SCHEMA_NS" x="NUM" y="NUM"/><Zoom xmlns="SCHEMA_NS" x="NUM"/></TAG>
```

with `PanTilt` omitted when no pan/tilt was given and `Zoom` omitted when no
zoom was given. `NUM` is the fixed 6-decimal format from Global Constraints.

| Call | Body |
|---|---|
| `continuousMove` | `<ContinuousMove xmlns="PTZ_NS"><ProfileToken>TOK</ProfileToken>` + vector `Velocity` + `<Timeout>PT1.000S</Timeout></ContinuousMove>` |
| `absoluteMove` | `<AbsoluteMove xmlns="PTZ_NS"><ProfileToken>TOK</ProfileToken>` + vector `Position` + optional vector `Speed` + `</AbsoluteMove>` |
| `relativeMove` | `<RelativeMove xmlns="PTZ_NS"><ProfileToken>TOK</ProfileToken>` + vector `Translation` + optional vector `Speed` + `</RelativeMove>` |
| `stop` | `<Stop xmlns="PTZ_NS"><ProfileToken>TOK</ProfileToken><PanTilt>BOOL</PanTilt><Zoom>BOOL</Zoom></Stop>` |
| `getStatus` | `<GetStatus xmlns="PTZ_NS"><ProfileToken>TOK</ProfileToken></GetStatus>` |

`absoluteMove` and `relativeMove` carry **no** `Timeout`.

### Capability guards

Checked before any body is built. If the guard rejects, **no request is
constructed and none is sent.**

| Call | Required space | Rejection message |
|---|---|---|
| `continuousMove` with pan/tilt | `continuousPanTilt` | `PTZ continuous pan/tilt is not supported` |
| `continuousMove` with zoom | `continuousZoom` | `PTZ continuous zoom is not supported` |
| `absoluteMove` with pan/tilt | `absolutePanTilt` | `PTZ absolute pan/tilt is not supported` |
| `absoluteMove` with zoom | `absoluteZoom` | `PTZ absolute zoom is not supported` |
| `relativeMove` with pan/tilt | `relativePanTilt` | `PTZ relative pan/tilt is not supported` |
| `relativeMove` with zoom | `relativeZoom` | `PTZ relative zoom is not supported` |

Other fixed messages:

| Condition | Message |
|---|---|
| Move call with neither pan/tilt nor zoom | `PTZ move requires pan/tilt or zoom` |
| Any call after `close()` | `PTZ session is closed` |
| No PTZ service advertised at open | `no ONVIF PTZ service` |
| `GetNodes` returned no node | `no ONVIF PTZ node` |
| Non-finite value | `PTZ value must be finite` |
| Timeout not finite or <= 0 | `PTZ timeout must be finite and greater than 0` |

### Session lifecycle

Open, in order: `connect()` → `GetServices` (locate the PTZ XAddr) → `GetNodes`
(cache spaces) → resolve the profile token.

`close()` sends `stop` with both axes true, inside a try/catch that **swallows a
stop failure**, then marks the session closed. A failing stop during close must
never replace an error the caller is already handling.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/onvif/ptzTypes.ts` | `PtzSpaces`, `PtzNode`, `PtzServiceCapabilities` value types, moved out of `capabilities.ts` so `capabilities` and `ptz` share them without importing each other |
| `src/onvif/ptz.ts` | Session, guards, SOAP body builders, response parsers |
| `src/onvif/ptz.test.ts` | TypeScript tests |
| `python/rtsp_backchannel/ptz_types.py` | Same value types for Python |
| `python/rtsp_backchannel/ptz.py` | Python session |
| `python/test_onvif_ptz.py` | Python tests |
| `rust/src/onvif/ptz_types.rs` | Same value types for Rust |
| `rust/src/onvif/ptz.rs` | Rust session + inline `#[cfg(test)]` tests |
| `rust/tests/fixtures/ptz-request-parity.json` | Expected SOAP request body per operation, shared by all three suites |

---

### Task 1: TypeScript shared types and transport rename

**Files:**
- Create: `src/onvif/ptzTypes.ts`
- Modify: `src/onvif/capabilities.ts`
- Modify: `src/onvif/deviceClient.ts`
- Modify: `src/index.ts`

**Interfaces:**
- Produces: `PtzSpaces`, `PtzNode`, `PtzServiceCapabilities` exported from `src/onvif/ptzTypes.ts`; `OnvifDevice.serviceCall(body: string, endpoint?: string): Promise<OnvifRawResponse>`.

- [ ] **Step 1: Move the PTZ value types into a new module**

Create `src/onvif/ptzTypes.ts` containing exactly the three interfaces currently declared in `src/onvif/capabilities.ts` — `PtzServiceCapabilities`, `PtzSpaces`, `PtzNode` — copied verbatim, with this file header:

```typescript
/**
 * PTZ value types shared by the read-only capability report and PTZ control.
 * They live here so neither module has to import the other.
 */
```

Delete those three interface declarations from `capabilities.ts` and add at its imports:

```typescript
export type { PtzNode, PtzServiceCapabilities, PtzSpaces } from './ptzTypes.ts';
import type { PtzNode, PtzServiceCapabilities, PtzSpaces } from './ptzTypes.ts';
```

The `export type` re-export keeps `src/index.ts` working unchanged. Verify `src/index.ts` still names these three types.

- [ ] **Step 2: Rename the transport entry point**

In `src/onvif/deviceClient.ts`, rename the method `readOnlyCall` to `serviceCall`. Its body and signature are otherwise unchanged. In `src/onvif/capabilities.ts`, rename `readOnlyCall` to `serviceCall` in the `CameraCapabilityDevice` interface and at both call sites, and rename the helper `parseReadOnlyResponse` to `parseServiceResponse`.

Rust needs no equivalent rename — it already calls the neutrally named `soap_response`.

- [ ] **Step 3: Run the full TypeScript suite**

Run: `npm test && npm run typecheck && npm run build`
Expected: all pass. This is a pure rename and type move; any failure means something was missed.

- [ ] **Step 4: Commit**

```bash
git add src/onvif/ptzTypes.ts src/onvif/capabilities.ts src/onvif/deviceClient.ts src/index.ts
git commit -m "refactor(ts): share PTZ value types and neutralize the transport name"
```

---

### Task 2: TypeScript PTZ session

**Files:**
- Create: `src/onvif/ptz.ts`
- Create: `src/onvif/ptz.test.ts`

**Interfaces:**
- Consumes: `PtzNode`, `PtzSpaces` from Task 1; `OnvifDevice.serviceCall` from Task 1; `parseXml`, `firstChild`, `childElements`, `attribute`, `textOf` from `src/onvif/xml.ts`.
- Produces: `openPtzSession(options, dependencies?)`, `PtzSession`, `PtzSessionOptions`, `PtzVector`, `PtzStatus`, `formatPtzNumber(value)`, `formatPtzDuration(ms)`.

- [ ] **Step 1: Write the failing formatter tests**

```typescript
test('formats PTZ numbers as fixed six-decimal strings', () => {
  assert.equal(formatPtzNumber(0.5), '0.500000');
  assert.equal(formatPtzNumber(-1), '-1.000000');
  assert.equal(formatPtzNumber(0), '0.000000');
  assert.equal(formatPtzNumber(-0), '0.000000');
  assert.equal(formatPtzNumber(0.1 + 0.2), '0.300000');
});

test('formats PTZ durations as fixed three-decimal seconds', () => {
  assert.equal(formatPtzDuration(1000), 'PT1.000S');
  assert.equal(formatPtzDuration(1500), 'PT1.500S');
  assert.equal(formatPtzDuration(250), 'PT0.250S');
});
```

- [ ] **Step 2: Run the test and verify RED**

Run: `npm test -- --test-name-pattern="PTZ numbers"`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the formatters**

```typescript
export function formatPtzNumber(value: number): string {
  if (!Number.isFinite(value)) throw new RangeError('PTZ value must be finite');
  const text = value.toFixed(6);
  return text === '-0.000000' ? '0.000000' : text;
}

export function formatPtzDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) {
    throw new RangeError('PTZ timeout must be finite and greater than 0');
  }
  return `PT${(milliseconds / 1000).toFixed(3)}S`;
}
```

- [ ] **Step 4: Run and verify GREEN**

Run: `npm test -- --test-name-pattern="PTZ"`
Expected: PASS.

- [ ] **Step 5: Write the failing guard and request-body tests**

Use the existing fake-dependency pattern from `src/onvif/capabilities.test.ts`: a `createDevice` stub recording `{ body, endpoint }` per call. Write tests asserting:

```typescript
test('rejects an unsupported absolute move without sending a request', async () => {
  const calls: { body: string; endpoint?: string }[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { absolutePanTilt: false }),
  );
  const sentBefore = calls.length;

  await assert.rejects(
    session.absoluteMove({ panTilt: { x: 0.5, y: 0 } }),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, 'PTZ absolute pan/tilt is not supported');
      return true;
    },
  );

  assert.equal(calls.length, sentBefore);
});

test('sends every continuous move with an explicit timeout', async () => {
  const calls: { body: string; endpoint?: string }[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  await session.continuousMove({ panTilt: { x: 0.5, y: -0.25 } });

  const move = calls.at(-1)!.body;
  assert.match(move, /<Timeout>PT1\.000S<\/Timeout>/);
  assert.match(move, /x="0\.500000"/);
  assert.match(move, /y="-0\.250000"/);
});

test('rejects out-of-range values without sending a request', async () => {
  const calls: { body: string; endpoint?: string }[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  const sentBefore = calls.length;
  for (const bad of [1.5, -1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    await assert.rejects(session.continuousMove({ panTilt: { x: bad, y: 0 } }));
  }
  assert.equal(calls.length, sentBefore);
});

test('stops both axes on close and keeps the original error', async () => {
  const calls: { body: string; endpoint?: string }[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  await session.close();
  const stop = calls.at(-1)!.body;
  assert.match(stop, /<PanTilt>true<\/PanTilt>/);
  assert.match(stop, /<Zoom>true<\/Zoom>/);
});
```

- [ ] **Step 6: Run and verify RED**

Run: `npm test -- --test-name-pattern="PTZ|continuous|absolute"`
Expected: FAIL — `openPtzSession` is not defined.

- [ ] **Step 7: Implement the session**

Implement the **Shared Operation Contract** section near the top of this plan —
value types, SOAP bodies, guards, fixed messages, and session lifecycle. That
section is the authority; everything below is TypeScript-specific help.

A vector builder in TypeScript:

```typescript
const PTZ_NS = 'http://www.onvif.org/ver20/ptz/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';

function vectorXml(tag: string, panTilt?: PtzVector, zoom?: number): string {
  let inner = '';
  if (panTilt) {
    inner += `<PanTilt xmlns="${SCHEMA_NS}" x="${formatPtzNumber(panTilt.x)}"`
      + ` y="${formatPtzNumber(panTilt.y)}"/>`;
  }
  if (zoom !== undefined) {
    inner += `<Zoom xmlns="${SCHEMA_NS}" x="${formatPtzNumber(zoom)}"/>`;
  }
  return `<${tag}>${inner}</${tag}>`;
}
```

Reuse the existing parsing helpers for `GetStatus`: `parseXml`, `firstChild`,
`attribute`, and `textOf` from `src/onvif/xml.ts`, following how
`parsePtzNodesResponse` in `capabilities.ts` reads `SCHEMA_NS` children out of a
`PTZ_NS` response.

Send every request through `device.serviceCall(body, ptzXAddr)` — the same
method the capability report uses, renamed in Task 1. The device client already
validates that `ptzXAddr` shares the device URL's origin, so control inherits
that guard with no extra work.

- [ ] **Step 8: Run the full TypeScript suite**

Run: `npm test && npm run typecheck && npm run build`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/onvif/ptz.ts src/onvif/ptz.test.ts
git commit -m "feat(ts): add guarded ONVIF PTZ movement control"
```

---

### Task 3: TypeScript exports and documentation

**Files:**
- Modify: `src/index.ts`, `src/index.test.ts`
- Modify: `README.md`, `README.ko.md`

**Interfaces:**
- Consumes: everything Task 2 produced.

- [ ] **Step 1: Write the failing export test**

In `src/index.test.ts`, extend the existing public-export assertions to require `openPtzSession` and the types `PtzSession`, `PtzSessionOptions`, `PtzVector`, `PtzStatus`.

- [ ] **Step 2: Run and verify RED**

Run: `npm test -- --test-name-pattern="export"`
Expected: FAIL.

- [ ] **Step 3: Add the exports with the experimental doc comment**

In `src/index.ts`:

```typescript
export { openPtzSession } from './onvif/ptz.ts';
export type {
  PtzSession,
  PtzSessionOptions,
  PtzStatus,
  PtzVector,
} from './onvif/ptz.ts';
```

Put this doc comment on `openPtzSession` in `src/onvif/ptz.ts`:

```typescript
/**
 * Open a PTZ control session.
 *
 * @experimental Physical movement is unverified against real PTZ hardware.
 * Request construction, capability guarding, the device-side move timeout, and
 * stop-on-close are covered by tests; that a camera actually moves as intended
 * is not.
 */
```

- [ ] **Step 4: Document in both root READMEs**

Add a `### PTZ Control` subsection under `## Public API` in `README.md` and its Korean equivalent in `README.ko.md`, containing a worked example and a clearly marked experimental note. The note must state the verified/unverified split, not merely the word "experimental":

> Verified: session open, capability guarding, request construction, timeout inclusion, and stop-on-close. Unverified: that a camera physically moves as intended — no PTZ hardware was available.

- [ ] **Step 5: Run the full TypeScript suite**

Run: `npm test && npm run typecheck && npm run build`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/index.ts src/index.test.ts src/onvif/ptz.ts README.md README.ko.md
git commit -m "feat(ts): expose experimental PTZ control"
```

---

### Task 4: Python shared types, transport rename, and PTZ session

**Files:**
- Create: `python/rtsp_backchannel/ptz_types.py`, `python/rtsp_backchannel/ptz.py`, `python/test_onvif_ptz.py`
- Modify: `python/rtsp_backchannel/capabilities.py`, `python/rtsp_backchannel/onvif.py`

**Interfaces:**
- Produces: `open_ptz_session(...)`, `PtzSession`, `PtzVector`, `PtzStatus`, `format_ptz_number(value)`, `format_ptz_duration(milliseconds)`; `OnvifDevice.service_call`.

- [ ] **Step 1: Move the PTZ dataclasses and rename the transport**

Create `python/rtsp_backchannel/ptz_types.py` holding `PtzSpaces`, `PtzNode`, `PtzServiceCapabilities` copied verbatim from `capabilities.py`, and re-import them there so `__init__.py` keeps working. Rename `OnvifDevice.read_only_call` to `service_call` in `onvif.py` and update its call sites in `capabilities.py`.

- [ ] **Step 2: Run the Python suite**

Run: `PYTHONPATH=.:python python3 -m unittest test_library_api test_onvif_library test_onvif_capabilities`
Expected: all pass — pure rename and move.

- [ ] **Step 3: Write the failing formatter and guard tests**

Mirror Task 2's tests exactly, in `python/test_onvif_ptz.py`:

```python
def test_formats_ptz_numbers_as_fixed_six_decimals(self):
    self.assertEqual(format_ptz_number(0.5), "0.500000")
    self.assertEqual(format_ptz_number(-1), "-1.000000")
    self.assertEqual(format_ptz_number(-0.0), "0.000000")
    self.assertEqual(format_ptz_number(0.1 + 0.2), "0.300000")

def test_formats_ptz_durations_as_fixed_three_decimals(self):
    self.assertEqual(format_ptz_duration(1000), "PT1.000S")
    self.assertEqual(format_ptz_duration(250), "PT0.250S")
```

- [ ] **Step 4: Run and verify RED**

Run: `PYTHONPATH=.:python python3 -m unittest test_onvif_ptz`
Expected: FAIL — module not found.

- [ ] **Step 5: Implement the formatters and session**

```python
def format_ptz_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("PTZ value must be finite")
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def format_ptz_duration(milliseconds: float) -> str:
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        raise ValueError("PTZ timeout must be finite and greater than 0")
    return f"PT{milliseconds / 1000:.3f}S"
```

Then implement the **Shared Operation Contract** section near the top of this
plan — value types, SOAP bodies, guards, fixed messages, and session lifecycle.
Send requests through `device.service_call(body, ptz_xaddr)`, renamed in Step 1.
Parse `GetStatus` with `ElementTree`, following how
`_parse_ptz_nodes_response` in `capabilities.py` reads schema-namespace children
out of a PTZ-namespace response.

- [ ] **Step 6: Run and verify GREEN**

Run: `PYTHONPATH=.:python python3 -m unittest test_onvif_ptz`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add python/rtsp_backchannel/ptz_types.py python/rtsp_backchannel/ptz.py \
        python/rtsp_backchannel/capabilities.py python/rtsp_backchannel/onvif.py \
        python/test_onvif_ptz.py
git commit -m "feat(python): add guarded ONVIF PTZ movement control"
```

---

### Task 5: Python exports and documentation

**Files:**
- Modify: `python/rtsp_backchannel/__init__.py`, `python/test_library_api.py`
- Modify: `python/README.md`, `python/README.ko.md`

- [ ] **Step 1: Write the failing export test**

Extend the export-contract test in `python/test_library_api.py` to require `open_ptz_session`, `PtzSession`, `PtzVector`, and `PtzStatus` in `rtsp_backchannel.__all__`.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=.:python python3 -m unittest test_library_api`
Expected: FAIL.

- [ ] **Step 3: Add exports and the experimental docstring**

Add the four names to the imports and `__all__` in `__init__.py`. Give `open_ptz_session` this docstring paragraph:

```
Experimental: physical movement is unverified against real PTZ hardware.
Request construction, capability guarding, the device-side move timeout, and
stop-on-close are covered by tests; that a camera actually moves as intended
is not.
```

- [ ] **Step 4: Document in both Python READMEs**

Add a `### open_ptz_session` subsection under `## Public API`, structured like
the existing `### get_camera_capabilities` section in the same file: a signature
block, a prose paragraph, and a worked example that reads the password from
`ONVIF_PASSWORD`. Close it with this note, translated for the Korean file:

> Verified: session open, capability guarding, request construction, timeout
> inclusion, and stop-on-close. Unverified: that a camera physically moves as
> intended — no PTZ hardware was available.

- [ ] **Step 5: Run the full Python suite**

Run: `PYTHONPATH=.:python python3 -m unittest test_library_api test_onvif_library test_onvif_capabilities test_onvif_ptz test_backchannel_audio test_backchannel_rtp test_onvif_play`
Expected: OK, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add python/rtsp_backchannel/__init__.py python/rtsp_backchannel/ptz.py \
        python/test_library_api.py python/README.md python/README.ko.md
git commit -m "feat(python): expose experimental PTZ control"
```

---

### Task 6: Rust PTZ session

**Files:**
- Create: `rust/src/onvif/ptz_types.rs`, `rust/src/onvif/ptz.rs`
- Modify: `rust/src/onvif.rs`, `rust/src/onvif/capabilities.rs`

**Interfaces:**
- Produces: `open_ptz_session(&PtzSessionOptions) -> Result<PtzSession, String>`, `PtzSession::{continuous_move, absolute_move, relative_move, stop, get_status, close}`, `format_ptz_number`, `format_ptz_duration`.

- [ ] **Step 1: Move the PTZ types**

Create `rust/src/onvif/ptz_types.rs` holding `PtzSpaces`, `PtzNode`, `PtzServiceCapabilities` moved verbatim from `capabilities.rs`, keeping every `serde` attribute. Add `mod ptz_types;` and re-export from `capabilities.rs` so the existing `pub use capabilities::{… PtzNode, PtzServiceCapabilities, PtzSpaces …}` in `rust/src/onvif.rs` keeps compiling unchanged.

Rust needs no transport rename — `soap_response` is already neutral.

- [ ] **Step 2: Run the Rust suite**

Run: `cargo test --manifest-path rust/Cargo.toml --locked`
Expected: all pass — pure move.

- [ ] **Step 3: Write the failing formatter tests**

In `rust/src/onvif/ptz.rs`'s `#[cfg(test)]` module:

```rust
#[test]
fn formats_ptz_numbers_as_fixed_six_decimals() {
    assert_eq!(format_ptz_number(0.5).unwrap(), "0.500000");
    assert_eq!(format_ptz_number(-1.0).unwrap(), "-1.000000");
    assert_eq!(format_ptz_number(-0.0).unwrap(), "0.000000");
    assert_eq!(format_ptz_number(0.1 + 0.2).unwrap(), "0.300000");
    assert!(format_ptz_number(f64::NAN).is_err());
}

#[test]
fn formats_ptz_durations_as_fixed_three_decimals() {
    assert_eq!(format_ptz_duration(1000.0).unwrap(), "PT1.000S");
    assert_eq!(format_ptz_duration(250.0).unwrap(), "PT0.250S");
    assert!(format_ptz_duration(0.0).is_err());
}
```

- [ ] **Step 4: Run and verify RED**

Run: `cargo test --manifest-path rust/Cargo.toml --locked ptz`
Expected: FAIL — does not compile.

- [ ] **Step 5: Implement formatters, guards, and session**

```rust
fn format_ptz_number(value: f64) -> Result<String, String> {
    if !value.is_finite() {
        return Err("PTZ value must be finite".to_owned());
    }
    let text = format!("{value:.6}");
    Ok(if text == "-0.000000" { "0.000000".to_owned() } else { text })
}

fn format_ptz_duration(milliseconds: f64) -> Result<String, String> {
    if !milliseconds.is_finite() || milliseconds <= 0.0 {
        return Err("PTZ timeout must be finite and greater than 0".to_owned());
    }
    Ok(format!("PT{:.3}S", milliseconds / 1000.0))
}
```

Then implement the **Shared Operation Contract** section near the top of this
plan — value types, SOAP bodies, guards, fixed messages, and session lifecycle.
Send requests through `device.soap_response(ptz_xaddr, &body, true)`, the same
method `capabilities.rs` already uses. Parse `GetStatus` with `roxmltree`,
following `parse_ptz_nodes_response` in `capabilities.rs`.

- [ ] **Step 6: Run all Rust checks**

Run:
```bash
cargo fmt --manifest-path rust/Cargo.toml --check
cargo test --manifest-path rust/Cargo.toml --locked
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
```
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add rust/src/onvif/ptz_types.rs rust/src/onvif/ptz.rs rust/src/onvif.rs \
        rust/src/onvif/capabilities.rs
git commit -m "feat(rust): add guarded ONVIF PTZ movement control"
```

---

### Task 7: Rust exports and documentation

**Files:**
- Modify: `rust/src/onvif.rs`, `rust/tests/public_api.rs`
- Modify: `rust/README.md`, `rust/README.ko.md`

- [ ] **Step 1: Write the failing export test**

In `rust/tests/public_api.rs`, add a test that references `rtsp_backchannel::onvif::{open_ptz_session, PtzSession, PtzSessionOptions, PtzStatus, PtzVector}` so the crate fails to compile if any is missing.

- [ ] **Step 2: Run and verify RED**

Run: `cargo test --manifest-path rust/Cargo.toml --locked --test public_api`
Expected: FAIL — unresolved imports.

- [ ] **Step 3: Add the re-exports and the rustdoc marker**

Extend the `pub use` block in `rust/src/onvif.rs`. Put this rustdoc on `open_ptz_session`:

```rust
/// Open a PTZ control session.
///
/// **Experimental.** Physical movement is unverified against real PTZ
/// hardware. Request construction, capability guarding, the device-side move
/// timeout, and stop-on-close are covered by tests; that a camera actually
/// moves as intended is not.
```

- [ ] **Step 4: Document in both Rust READMEs**

Add a `### PTZ Control` subsection mirroring the existing `### Camera Capability Evidence` section, with a worked example and the same verified/unverified split note.

- [ ] **Step 5: Run all Rust checks and packaging**

Run:
```bash
cargo fmt --manifest-path rust/Cargo.toml --check
cargo test --manifest-path rust/Cargo.toml --locked
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
cargo package --manifest-path rust/Cargo.toml --locked --allow-dirty
```
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add rust/src/onvif.rs rust/src/onvif/ptz.rs rust/tests/public_api.rs \
        rust/README.md rust/README.ko.md
git commit -m "feat(rust): expose experimental PTZ control"
```

---

### Task 8: Cross-language request parity, live negative check, and changelog

**Files:**
- Create: `rust/tests/fixtures/ptz-request-parity.json`
- Modify: `src/onvif/ptz.test.ts`, `python/test_onvif_ptz.py`, `rust/src/onvif/ptz.rs`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write the shared request-body fixture**

Create `rust/tests/fixtures/ptz-request-parity.json` mapping each operation to the exact SOAP body all three implementations must emit for one fixed input. Use profile token `MediaProfile00000`, pan/tilt `x = 0.5`, `y = -0.25`, zoom `0.75`, timeout 1000 ms:

```json
{
  "profileToken": "MediaProfile00000",
  "panTilt": { "x": 0.5, "y": -0.25 },
  "zoom": 0.75,
  "timeoutMs": 1000,
  "requests": {
    "continuousMove": "<ContinuousMove xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"><ProfileToken>MediaProfile00000</ProfileToken><Velocity><PanTilt xmlns=\"http://www.onvif.org/ver10/schema\" x=\"0.500000\" y=\"-0.250000\"/><Zoom xmlns=\"http://www.onvif.org/ver10/schema\" x=\"0.750000\"/></Velocity><Timeout>PT1.000S</Timeout></ContinuousMove>",
    "absoluteMove": "<AbsoluteMove xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"><ProfileToken>MediaProfile00000</ProfileToken><Position><PanTilt xmlns=\"http://www.onvif.org/ver10/schema\" x=\"0.500000\" y=\"-0.250000\"/><Zoom xmlns=\"http://www.onvif.org/ver10/schema\" x=\"0.750000\"/></Position></AbsoluteMove>",
    "relativeMove": "<RelativeMove xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"><ProfileToken>MediaProfile00000</ProfileToken><Translation><PanTilt xmlns=\"http://www.onvif.org/ver10/schema\" x=\"0.500000\" y=\"-0.250000\"/><Zoom xmlns=\"http://www.onvif.org/ver10/schema\" x=\"0.750000\"/></Translation></RelativeMove>",
    "stop": "<Stop xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"><ProfileToken>MediaProfile00000</ProfileToken><PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>",
    "getStatus": "<GetStatus xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"><ProfileToken>MediaProfile00000</ProfileToken></GetStatus>"
  }
}
```

- [ ] **Step 2: Add a fixture test to each language**

Each suite reads the fixture, drives its session with the fixture's inputs against a recording fake, and asserts the recorded request body is **string-equal** to the fixture entry. Follow how each suite already loads `rust/tests/fixtures/capability-parity.json`.

- [ ] **Step 3: Run all three suites and verify GREEN**

Run:
```bash
npm test && npm run typecheck && npm run build
PYTHONPATH=.:python python3 -m unittest test_library_api test_onvif_library \
  test_onvif_capabilities test_onvif_ptz test_backchannel_audio \
  test_backchannel_rtp test_onvif_play
cargo fmt --manifest-path rust/Cargo.toml --check
cargo test --manifest-path rust/Cargo.toml --locked
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
```
Expected: all pass. Any mismatch here is a genuine cross-language divergence — fix the implementation, never the fixture, unless the fixture itself is wrong.

- [ ] **Step 4: Run the live negative check**

The test camera has no PTZ service, so this verifies the failure path against real hardware. Only run when the camera is routable:

```bash
export CAMERA_HOST=10.128.10.141 ONVIF_PASSWORD=...
route -n get "$CAMERA_HOST" && nc -G 2 -z "$CAMERA_HOST" 80
```

Then open a session against it from any one language and confirm it fails with `no ONVIF PTZ service` — promptly, with no hang and no stack trace leaking a host or credential. Record the result. If the camera is unroutable, leave this step unchecked rather than claiming it passed.

- [ ] **Step 5: Update the changelog**

Add under `## [Unreleased]`:

```markdown
### Added

- Experimental ONVIF PTZ movement control in all three packages:
  `openPtzSession` / `open_ptz_session` for continuous, absolute, and relative
  moves plus stop and status. Every continuous move carries a device-side
  timeout so a stopped client cannot leave a camera moving, and operations the
  camera does not advertise are rejected before a request is sent. Physical
  movement is unverified against real PTZ hardware.
```

- [ ] **Step 6: Commit**

```bash
git add rust/tests/fixtures/ptz-request-parity.json src/onvif/ptz.test.ts \
        python/test_onvif_ptz.py rust/src/onvif/ptz.rs CHANGELOG.md
git commit -m "test: pin PTZ request bodies across all three implementations"
```
