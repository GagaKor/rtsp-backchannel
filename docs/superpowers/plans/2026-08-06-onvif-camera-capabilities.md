# ONVIF Camera Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add equivalent TypeScript, Python, and Rust APIs plus a `capabilities` CLI command that report a camera's declared ONVIF profiles, service inventory, PTZ abilities, event topics, and Media2/H.265 evidence without claiming certified conformance.

**Architecture:** Each implementation reuses its existing authenticated ONVIF client and adds a focused capability parser/orchestrator beside it. The orchestrator connects once, calls `GetScopes`, prefers `GetServices(IncludeCapability=true)` with a legacy `GetCapabilities(All)` fallback, then sends PTZ, Events, and Media2 read-only requests only to advertised XAddrs. Optional enrichment failures produce sanitized warnings and tri-state unknowns; connection/authentication failures still fail the call.

**Tech Stack:** Node.js 22 + TypeScript + `saxes`, Python 3.11+ standard-library `ElementTree`, Rust 1.86+ + existing `roxmltree`/`reqwest`, SOAP 1.2, ONVIF Device/Media/Media2/PTZ/Events services.

---

## Public contract

All three packages expose the same conceptual operation:

```ts
getCameraCapabilities({
  host,
  user,
  pass,
  deviceUrls?,
  timeoutMs?,
}): Promise<CameraCapabilityReport>
```

```python
get_camera_capabilities(
    *,
    host: str,
    user: str = "",
    password: str = "",
    device_urls: list[str] | None = None,
    timeout: float = 8.0,
) -> CameraCapabilityReport
```

```rust
get_camera_capabilities(
    &CameraCapabilityOptions,
) -> Result<CameraCapabilityReport, String>
```

The CLI form is equivalent in all packages:

```bash
rtsp-backchannel capabilities \
  --host "$CAMERA_HOST" \
  --user "$ONVIF_USER" \
  --device-url "$ONVIF_DEVICE_URL" \
  --timeout-ms 8000
```

The password comes from `ONVIF_PASSWORD` when `--pass` is omitted. The command prints exactly one camelCase JSON object.

### Shared JSON shape

```ts
interface CameraCapabilityReport {
  device: {
    manufacturer?: string;
    model?: string;
    firmware?: string;
    serial?: string;
  };
  scopes: string[];
  declaredProfiles: string[];
  serviceDiscovery: 'getServices' | 'getCapabilities' | 'unavailable';
  services: Array<{
    namespace: string;
    xaddr: string;
    version?: { major: number; minor: number };
  }>;
  profiles: Array<{
    token: string;
    name?: string;
    source: 'media1' | 'media2';
    hasAudioEncoder: boolean;
    hasAudioOutput: boolean;
    hasAudioSource: boolean;
    ptzConfigurationToken?: string;
    ptzNodeToken?: string;
  }>;
  ptz: {
    detected: boolean | null;
    panTiltSupported: boolean | null;
    zoomSupported: boolean | null;
    profileTokens: string[];
    serviceCapabilities?: {
      eFlip?: boolean;
      reverse?: boolean;
      getCompatibleConfigurations?: boolean;
      moveStatus?: boolean;
      statusPosition?: boolean;
    };
    nodes: Array<{
      token: string;
      name?: string;
      spaces: {
        absolutePanTilt: boolean;
        absoluteZoom: boolean;
        relativePanTilt: boolean;
        relativeZoom: boolean;
        continuousPanTilt: boolean;
        continuousZoom: boolean;
      };
      maximumPresets?: number;
      homeSupported?: boolean;
      auxiliaryCommands: string[];
    }>;
  };
  events: {
    detected: boolean | null;
    serviceCapabilities?: {
      wsSubscriptionPolicySupport?: boolean;
      wsPullPointSupport?: boolean;
      wsPausableSubscriptionManagerInterfaceSupport?: boolean;
      persistentNotificationStorage?: boolean;
      maxNotificationProducers?: number;
      maxPullPoints?: number;
      eventBrokerProtocols?: string[];
      maxEventBrokers?: number;
    };
    topics: Array<{ namespace?: string; path: string }>;
  };
  media2: {
    detected: boolean | null;
    encodings: string[];
    h265Supported: boolean | null;
  };
  warnings: Array<{ operation: string; message: string }>;
}
```

Contract rules:

- `Profile/Streaming` maps to declared profile `S`; other `Profile/<value>` suffixes are URI-decoded and uppercased. Raw scopes are preserved and duplicates are removed deterministically.
- Declared profiles are device self-reports. They are not ONVIF certification claims. `media2.detected` and `h265Supported` are functional evidence, not proof of Profile T conformance.
- `false` means the corresponding successful response established absence. `null` means it was not possible to establish the fact because service discovery or an enrichment call failed. Optional object members are omitted when not reported by the device.
- A PTZ service, a media-profile PTZ binding, and actual movement spaces are separate facts. Zoom-only nodes never set `panTiltSupported`.
- Event topics include only elements marked with the WS-Topics `topic` attribute. Their path uses local names under `TopicSet`; the element namespace URI is retained separately when available. `MessageDescription`, `Source`, `Data`, and item-description nodes are not topics unless explicitly marked as such.
- The per-request timeout keeps the existing semantics. A report contains multiple requests and can therefore take more than one timeout interval.
- Service and topic arrays are sorted deterministically. Duplicate service namespaces may remain in the report, but enrichment uses the highest advertised version with a stable XAddr tie-break.
- No credential, WSSE digest input, URL userinfo, or real camera response is committed in fixtures or emitted in warnings.

Primary protocol references:

- Device `GetScopes`, `GetServices`, and `GetCapabilities`: <https://www.onvif.org/ver10/device/wsdl/devicemgmt.wsdl>
- PTZ `GetServiceCapabilities` and `GetNodes`: <https://www.onvif.org/ver20/ptz/wsdl/ptz.wsdl>
- Events `GetServiceCapabilities` and `GetEventProperties`: <https://www.onvif.org/ver10/events/wsdl/event.wsdl>
- Media2 encoder options: <https://www.onvif.org/ver20/media/wsdl/media.wsdl>
- Profile T interpretation: <https://www.onvif.org/profiles/profile-t/>

## Pre-implementation camera check

The requested target is `10.128.10.141`. A read-only network preflight was completed on
2026-08-06 before implementation:

- `route -n get 10.128.10.141` selected the normal `172.168.40.3` default gateway instead of a
  private/VPN route for `10.128.10.0/24`.
- ICMP received no replies, common ONVIF TCP ports `80`, `443`, `8000`, `8080`, and `8899` did
  not connect, and traceroute continued toward the public ISP network.
- No credential-bearing SOAP request was sent because the route did not remain on the intended
  private network. This is a network/VPN reachability blocker, not an authentication result.

Implementation proceeds against synthetic, credential-free fixtures. Task 8 repeats the preflight
and runs all three read-only CLIs only after the private route becomes available.

## File structure

### TypeScript

- Create `src/onvif/xml.ts`: namespace-aware bounded XML tree parsing and SOAP-fault helpers.
- Create `src/onvif/xml.test.ts`: parser, namespace, entity/DOCTYPE rejection, and topic-tree tests.
- Create `src/onvif/capabilities.ts`: report types, ONVIF response parsers, orchestration, and public wrapper.
- Create `src/onvif/capabilities.test.ts`: parser and fake-device orchestration tests.
- Modify `src/onvif/deviceClient.ts`: keep the already retained selected Device URL and expose it only through internal read-only raw operation calls without changing existing stream call sequences.
- Modify `src/onvif/deviceClient.test.ts`: exact request-body, response validation, and endpoint-routing coverage.
- Modify `src/index.ts`, `src/index.test.ts`: public function/type exports and declaration hygiene.
- Modify `src/cli.ts`, `src/cli.test.ts`: `capabilities` dispatch and JSON output.
- Modify `package.json`, `package-lock.json`, `THIRD_PARTY_NOTICES.md`: runtime `saxes` dependency and notices.
- Modify `README.md`, `README.ko.md`: API, CLI, and interpretation guidance.

### Python

- Create `python/rtsp_backchannel/capabilities.py`: report dataclasses, XML parsers, orchestrator, and camelCase JSON conversion.
- Create `python/test_onvif_capabilities.py`: parser and orchestration tests.
- Modify `python/rtsp_backchannel/onvif.py`: return device information from `connect()`, retain endpoint access, and keep existing playback/stream behavior unchanged.
- Modify `python/rtsp_backchannel/__init__.py`, `python/test_library_api.py`: package exports and CLI/public contract tests.
- Modify `python/rtsp_backchannel/cli.py`: `capabilities` parser, environment password fallback, and one JSON object.
- Modify `python/README.md`, `python/README.ko.md`: Python usage and shared semantics.

### Rust

- Create `rust/src/onvif/capabilities.rs`: serializable report types, `roxmltree` parsers, orchestration, and tests.
- Modify `rust/src/onvif.rs`: declare/re-export the child module, retain selected Device XAddr, and return device information from `connect()` without extra calls.
- Modify `rust/tests/onvif_api.rs`: public API, mock-camera routing/fallback, CLI, and JSON tests.
- Modify `rust/src/cli.rs`, `rust/src/main.rs`: `CapabilitiesCli`, invocation dispatch, and one JSON object.
- Modify `rust/README.md`, `rust/README.ko.md`: Rust usage and shared semantics.
- Do not change `rust/Cargo.toml` or `rust/Cargo.lock`; existing crates are sufficient.

---

### Task 1: TypeScript namespace-aware XML foundation

**Files:**
- Create: `src/onvif/xml.ts`
- Create: `src/onvif/xml.test.ts`
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `THIRD_PARTY_NOTICES.md`

- [ ] **Step 1: Write failing XML-tree tests**

Test a namespaced document whose prefixes change, repeated sibling elements, namespaced attributes, text/entity decoding, malformed XML, and explicit rejection of `DOCTYPE`/`ENTITY`. The wished-for internal API is:

```ts
const root = parseXml(xml);
const response = requireDescendant(root, DEV_NS, 'GetServicesResponse');
const services = childElements(response, DEV_NS, 'Service');
attribute(node, WSTOP_NS, 'topic');
textOf(firstChild(node, DEV_NS, 'Namespace'));
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm test -- src/onvif/xml.test.ts`

Expected: FAIL because `src/onvif/xml.ts` does not exist.

- [ ] **Step 3: Add `saxes` as a runtime dependency**

Run: `npm install saxes@^6.0.0`

Record `saxes` (ISC) and its `xmlchars` dependency in `THIRD_PARTY_NOTICES.md`. Do not configure or resolve external entities.

- [ ] **Step 4: Implement the minimal immutable XML tree**

Use `SaxesParser({ xmlns: true })`, retain `{ uri, local, attributes, children, text }`, reject DTD/entity declarations before parsing, cap parser input at the existing SOAP body limit, and surface parse errors with operation-neutral messages.

- [ ] **Step 5: Run focused tests, typecheck, and build**

Run:

```bash
npm test -- src/onvif/xml.test.ts
npm run typecheck
npm run build
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/onvif/xml.ts src/onvif/xml.test.ts package.json package-lock.json THIRD_PARTY_NOTICES.md
git commit -m "feat(ts): add namespace-aware ONVIF XML parsing"
```

### Task 2: TypeScript capability parsing and orchestration

**Files:**
- Create: `src/onvif/capabilities.ts`
- Create: `src/onvif/capabilities.test.ts`
- Modify: `src/onvif/deviceClient.ts`
- Modify: `src/onvif/deviceClient.test.ts`

- [ ] **Step 1: Write failing pure-parser tests**

Fixtures must cover:

- nested `Scopes/ScopeItem` and the `Streaming -> S` alias;
- direct-child `Service` parsing, duplicate namespace versions, and malformed/fault responses;
- legacy `GetCapabilities(All)` mapping for Device, Media, PTZ, Events, Imaging, Analytics, DeviceIO, Recording, Search, Replay, Receiver, and Display;
- Media1 and Media2 profile PTZ references without changing the existing `OnvifProfile` contract;
- PTZ capability attributes with `true/false/1/0`, multiple nodes, and zoom-only nodes;
- arbitrary-depth WS-Topics elements with prefix changes and schema-description descendants;
- repeated Media2 `Options/Encoding` values for H264 and H265.

- [ ] **Step 2: Run parser tests and verify RED**

Run: `npm test -- src/onvif/capabilities.test.ts`

Expected: FAIL because report types and parsers are absent.

- [ ] **Step 3: Implement report types and pure parsers**

Implement the shared shape exactly. Parse direct children where association matters, parse optional booleans and integers strictly, normalize/deduplicate without losing unknown vendor namespaces, and add operation-response validation that distinguishes SOAP faults from malformed responses.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run: `npm test -- src/onvif/capabilities.test.ts`

Expected: parser cases pass.

- [ ] **Step 5: Write failing device and orchestrator tests**

Use the existing local HTTP server style and a fake-device seam. Assert:

- `connect()` still sends only time, device information, and Media capability calls for existing stream consumers;
- the selected Device XAddr is retained;
- exact `GetScopes`, `GetServices` with `IncludeCapability=true`, `GetCapabilities(All)`, PTZ, Events, Media1 profiles, Media2 `GetProfiles` with `<Type>All</Type>`, and Media2 encoder-option bodies;
- PTZ/Event/Media2 calls use their advertised XAddr;
- `ActionNotSupported` or an invalid `GetServices` response falls back to All;
- authentication faults are not hidden by fallback;
- optional endpoint failures yield sanitized structured warnings and `null`, while initial connect/auth failure rejects;
- Media1 profile failure can still produce Media2 profiles.

- [ ] **Step 6: Run device/orchestrator tests and verify RED**

Run:

```bash
npm test -- src/onvif/deviceClient.test.ts src/onvif/capabilities.test.ts
```

Expected: new routing/orchestration assertions fail.

- [ ] **Step 7: Implement minimal device calls and `getCameraCapabilities`**

Do not move capability calls into `connect()`. Add internal raw read-only calls after connection, preserve the existing response-size and timeout limits, select enrichment endpoints deterministically, merge legacy Event capability fields, and make warnings credential-free.

- [ ] **Step 8: Run focused and full TypeScript checks**

Run:

```bash
npm test -- src/onvif/xml.test.ts src/onvif/deviceClient.test.ts src/onvif/capabilities.test.ts
npm test
npm run typecheck
npm run build
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/onvif/capabilities.ts src/onvif/capabilities.test.ts src/onvif/deviceClient.ts src/onvif/deviceClient.test.ts
git commit -m "feat(ts): report ONVIF camera capabilities"
```

### Task 3: TypeScript public export, CLI, and documentation

**Files:**
- Modify: `src/index.ts`
- Modify: `src/index.test.ts`
- Modify: `src/cli.ts`
- Modify: `src/cli.test.ts`
- Modify: `README.md`
- Modify: `README.ko.md`

- [ ] **Step 1: Write failing export and CLI tests**

Assert root exports for `getCameraCapabilities` and all public report types, clean generated declarations without dependency-injection seams, `capabilities --help`, repeated `--device-url`, finite positive `--timeout-ms`, `ONVIF_PASSWORD` fallback, one JSON log line, and unchanged discover/streams/direct-play behavior.

- [ ] **Step 2: Run tests and verify RED**

Run: `npm test -- src/index.test.ts src/cli.test.ts`

- [ ] **Step 3: Implement exports and CLI dispatch**

Add `getCameraCapabilities` to `CommandDependencies`, print one native camelCase report object, and keep credentials out of help/errors/logs.

- [ ] **Step 4: Document API and interpretation**

Add equivalent English/Korean examples, field meanings, tri-state behavior, Profile T certification caveat, per-request timeout semantics, and `capabilities` CLI usage.

- [ ] **Step 5: Verify TypeScript packaging**

Run:

```bash
npm test
npm run typecheck
npm run build
npm pack --dry-run --json
```

- [ ] **Step 6: Commit**

```bash
git add src/index.ts src/index.test.ts src/cli.ts src/cli.test.ts README.md README.ko.md
git commit -m "feat(ts): expose camera capability reporting"
```

### Task 4: Python capability parser and client orchestration

**Files:**
- Create: `python/rtsp_backchannel/capabilities.py`
- Create: `python/test_onvif_capabilities.py`
- Modify: `python/rtsp_backchannel/onvif.py`

- [ ] **Step 1: Write failing parser tests**

Port the TypeScript semantic fixtures, not its implementation. Cover service association/version selection, legacy mapping, both profile formats, PTZ booleans/nodes, namespace-aware event topics, Media2 encodings, SOAP faults, and malformed XML.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python:. \
  python3 -m unittest python.test_onvif_capabilities -v
```

- [ ] **Step 3: Implement dataclasses and `ElementTree` parsers**

Keep `OnvifProfile` unchanged. Use a report-specific profile dataclass, namespace URI/local-name splitting, direct-child helpers, strict optional boolean/integer parsers, and deterministic sorting.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run the focused command again; parser cases pass.

- [ ] **Step 5: Write failing orchestration tests**

Patch/fake `OnvifDevice` and also use one local HTTP server test to verify exact request bodies (including Media2 `GetProfiles` with `<Type>All</Type>`), advertised endpoint routing, unsupported `GetServices` fallback, auth-fault propagation, Media2 profile recovery, warning sanitization, and no change to existing stream request order.

- [ ] **Step 6: Run tests and verify RED**

Run the focused command; new orchestration cases fail.

- [ ] **Step 7: Implement device information return and `get_camera_capabilities`**

Make `OnvifDevice.connect()` return a new immutable `DeviceInfo` while preserving callers that ignore its return. Add `_required_device_url()` and reuse `_call`; do not duplicate WSSE or HTTP code. Keep every optional enrichment best-effort and every initial connection/authentication failure fatal.

- [ ] **Step 8: Run focused and full Python tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python:. \
  python3 -m unittest python.test_onvif_capabilities -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python:. \
  python3 -m unittest discover -s python -p 'test_*.py'
```

- [ ] **Step 9: Commit**

```bash
git add python/rtsp_backchannel/capabilities.py python/test_onvif_capabilities.py python/rtsp_backchannel/onvif.py
git commit -m "feat(python): report ONVIF camera capabilities"
```

### Task 5: Python package export, CLI, and documentation

**Files:**
- Modify: `python/rtsp_backchannel/__init__.py`
- Modify: `python/rtsp_backchannel/cli.py`
- Modify: `python/test_library_api.py`
- Modify: `python/README.md`
- Modify: `python/README.ko.md`

- [ ] **Step 1: Write failing package and CLI tests**

Assert package-root exports, `capabilities` parsing, `ONVIF_PASSWORD` fallback, timeout validation, repeated device URLs, one camelCase JSON object, help text, and unchanged existing dispatch.

- [ ] **Step 2: Run tests and verify RED**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python:. python3 -m unittest python.test_library_api -v`

- [ ] **Step 3: Implement export, JSON conversion, and CLI dispatch**

Keep Python dataclasses snake_case while producing the shared camelCase CLI object. Do not use `dataclasses.asdict()` without an explicit recursive key mapping.

- [ ] **Step 4: Document Python API and CLI**

Mirror the shared semantic guidance in both Python READMEs. Update the repository changelog once in Task 8 after all three implementations pass.

- [ ] **Step 5: Verify Python package**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python:. \
  python3 -m unittest discover -s python -p 'test_*.py'
python3 -m build python
python3 -m twine check python/dist/*
```

If `build` or `twine` is not installed, create a temporary virtual environment and install only those packaging tools; do not add them to runtime dependencies.

- [ ] **Step 6: Commit**

```bash
git add python/rtsp_backchannel/__init__.py python/rtsp_backchannel/cli.py python/test_library_api.py python/README.md python/README.ko.md
git commit -m "feat(python): expose camera capability reporting"
```

### Task 6: Rust capability parser and client orchestration

**Files:**
- Create: `rust/src/onvif/capabilities.rs`
- Modify: `rust/src/onvif.rs`
- Modify: `rust/tests/onvif_api.rs`

- [ ] **Step 1: Write failing `roxmltree` parser tests**

Port the shared semantic fixtures. Assert `Option`/`null` unknown semantics, stable service ordering, Media1/Media2 profile parsing, zoom-only PTZ behavior, exact namespace URIs in event topics, and H.265 detection.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `cargo +1.86.0 test --manifest-path rust/Cargo.toml onvif::capabilities::tests`

- [ ] **Step 3: Implement serializable report types and pure parsers**

Derive `Debug`, `Clone`, `PartialEq`, `Eq`, and `Serialize`, use `#[serde(rename_all = "camelCase")]`, skip optional object members but serialize tri-state `Option<bool>` fields as JSON `null`, and use stable sorting/BTree collections.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run the focused command again; parser cases pass.

- [ ] **Step 5: Write failing client integration tests**

Use the existing sequential mock-camera server to verify:

- `connect()` request count is unchanged and now retains the selected Device XAddr;
- the full GetServices path routes enrichment to advertised endpoints;
- a SOAP `ActionNotSupported` response triggers `GetCapabilities(All)`;
- auth faults remain fatal;
- optional probe failures return sanitized warnings and unknowns;
- `get_camera_capabilities` is usable through `rtsp_backchannel::onvif`.

- [ ] **Step 6: Run integration tests and verify RED**

Run: `cargo +1.86.0 test --manifest-path rust/Cargo.toml --test onvif_api capability`

- [ ] **Step 7: Implement `get_camera_capabilities` and parent-module plumbing**

Add Media2/PTZ/Events constants, `DeviceInfo`, selected `device_url`, and child-module re-exports. Reuse `soap_response()` so strict operation validation can inspect status/faults; do not loosen the 1 MiB limit or TLS/redirect policy.

- [ ] **Step 8: Run focused and full Rust tests**

Run:

```bash
cargo +1.86.0 fmt --manifest-path rust/Cargo.toml --check
cargo +1.86.0 test --manifest-path rust/Cargo.toml --locked
cargo +1.86.0 clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
```

- [ ] **Step 9: Commit**

```bash
git add rust/src/onvif/capabilities.rs rust/src/onvif.rs rust/tests/onvif_api.rs
git commit -m "feat(rust): report ONVIF camera capabilities"
```

### Task 7: Rust CLI and documentation

**Files:**
- Modify: `rust/src/cli.rs`
- Modify: `rust/src/main.rs`
- Modify: `rust/tests/onvif_api.rs`
- Modify: `rust/README.md`
- Modify: `rust/README.ko.md`

- [ ] **Step 1: Write failing CLI and JSON tests**

Assert `CapabilitiesCli`, environment password behavior, timeout/device URL arguments, help output, one camelCase JSON object with explicit null tri-state fields, and existing command compatibility.

- [ ] **Step 2: Run tests and verify RED**

Run: `cargo +1.86.0 test --manifest-path rust/Cargo.toml --test onvif_api capabilities_cli`

- [ ] **Step 3: Implement CLI dispatch and documentation**

Add the command to help/dispatch, build `CameraCapabilityOptions`, serialize once with `serde_json`, and mirror the shared interpretation guidance in both Rust READMEs.

- [ ] **Step 4: Verify Rust packaging**

Run:

```bash
cargo +1.86.0 fmt --manifest-path rust/Cargo.toml --check
cargo +1.86.0 test --manifest-path rust/Cargo.toml --locked
cargo +1.86.0 clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
cargo +1.86.0 package --manifest-path rust/Cargo.toml --locked
```

- [ ] **Step 5: Commit**

```bash
git add rust/src/cli.rs rust/src/main.rs rust/tests/onvif_api.rs rust/README.md rust/README.ko.md
git commit -m "feat(rust): expose camera capability reporting"
```

### Task 8: Cross-language parity, packaging, and live read-only verification

**Files:**
- Modify `CHANGELOG.md` once for the complete cross-language feature.
- Modify only other files required by failures found during this task.

- [ ] **Step 1: Add/compare one shared semantic fixture**

Ensure all three implementations produce equivalent facts from semantically identical XML: declared S/T, Media1 and Media2 services, one zoom-only PTZ node plus one pan/tilt node, nested motion/tamper topics, and H264/H265 encodings. Language-native object naming may differ; CLI JSON must match the shared camelCase contract.

- [ ] **Step 2: Run all repository checks**

Run:

```bash
npm test
npm run typecheck
npm run build
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=python:. \
  python3 -m unittest discover -s python -p 'test_*.py'
cargo +1.86.0 fmt --manifest-path rust/Cargo.toml --check
cargo +1.86.0 test --manifest-path rust/Cargo.toml --locked
cargo +1.86.0 clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
```

- [ ] **Step 3: Inspect release artifacts**

Run npm dry-run packing, build/check the Python wheel, and
`cargo +1.86.0 package --manifest-path rust/Cargo.toml --locked`. Inspect file lists for local
paths, real device data, passwords, and fixture captures.

- [ ] **Step 4: Run live read-only probes when the private route exists**

Define the requested target and preflight before sending credentials:

```bash
export CAMERA_HOST=10.128.10.141
route -n get "$CAMERA_HOST"
nc -G 2 -z "$CAMERA_HOST" 80
```

Proceed only if the route stays inside the intended private/VPN network. Run each `capabilities` CLI with the password supplied through `ONVIF_PASSWORD`, save outputs under gitignored `.context/`, and compare service/profile/PTZ/event/Media2 facts. Do not run PTZ movement, event subscription, or any write operation.

```bash
node --experimental-transform-types src/cli.ts capabilities \
  --host "$CAMERA_HOST" --user admin > .context/capabilities-ts.json

PYTHONPATH=python python3 -m rtsp_backchannel.cli capabilities \
  --host "$CAMERA_HOST" --user admin > .context/capabilities-python.json

cargo +1.86.0 run --quiet --manifest-path rust/Cargo.toml -- capabilities \
  --host "$CAMERA_HOST" --user admin > .context/capabilities-rust.json
```

If the route is still unavailable, record the route/traceroute evidence and leave live validation explicitly pending rather than fabricating a camera result.

- [ ] **Step 5: Scan and review the final diff**

Run:

```bash
git diff --check
git diff --stat origin/master...
git grep -n -I -E 'qwer' -- . ':!.context' || true
```

Request final spec-compliance and code-quality reviews. Fix and re-run the relevant checks for every accepted issue.

- [ ] **Step 6: Commit integration fixes, if any**

```bash
git add CHANGELOG.md <only-related-files>
git commit -m "test: verify capability report parity"
```
