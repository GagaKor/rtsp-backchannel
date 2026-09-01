# The automatic VIGI probe is gated on vendor evidence, not on a blanket flag

**Date:** 2026-08-25
**Status:** Accepted
**Scope:** `src/onvif/capabilities.ts` — the `audioSend` VIGI probe and its new `probeVigiTalk` option

## Context

`getCameraCapabilities` runs its `audioSend` probes by default
(`probeAudioSend ?? true`). The ONVIF half is harmless: it reuses credentials
`connect()` has already proven and does a read-only DESCRIBE. The VIGI half is
not. It calls `openVigiControl`, which performs a credential-bearing `doAuth`
against the OpenAPI control port, and that port counts failed attempts toward a
device-side lockout — `src/vigi/control.ts` carries a dedicated message for
error `-10022`, "account is locked by retry limit".

Two facts make the default-on behaviour actively harmful. First, the module's own
comment states that on VIGI hardware the ONVIF account and the OpenAPI admin
account are configured separately, so a successful ONVIF exchange does not prove
the OpenAPI password — a nearby comment claiming the probe runs "against a host
we already know accepts these credentials" contradicts it and is wrong. Second,
`defaultProbeVigiTalk` forwarded the ONVIF username verbatim, so
`--user viewer` sent `doAuth` as `viewer` against an API documented as
admin-only: a guaranteed failed attempt, every run.

The overwhelming majority of ONVIF cameras are not TP-Link VIGI hardware, so the
default was spending lockout attempts on devices where the vendor extension
cannot apply at all.

## Decision

Gate the automatic VIGI probe on vendor evidence already present in the report:
probe only when `GetDeviceInformation` identifies TP-Link/VIGI hardware. Expose
`probeVigiTalk?: 'auto' | 'always' | 'never'` (default `'auto'`) for the cases
vendor strings cannot settle. Stop forwarding the ONVIF username, letting
`openVigiControl` use its documented `admin` default.

## Alternatives Considered

### Vendor-evidence gate with an `'auto' | 'always' | 'never'` override — chosen
- **Description:** `vigiHardwareLikelihood(device)` returns true/false/null from `manufacturer` and `model`; `'auto'` probes only on true.
- **Pros:** Removes the lockout exposure entirely for every camera that is not VIGI, which is nearly all of them, while keeping automatic detection working on real VIGI hardware with no flag to discover. Preserves a meaningful `detected: false` for non-VIGI cameras. Uses information the report already collects, so it costs no extra request.
- **Cons:** Depends on vendor strings, which are free-form; a VIGI camera with an unusual `manufacturer` needs `'always'`. Three-state option is more surface than a boolean.

### Flip `probeAudioSend` to default false
- **Description:** Turn both probes off by default.
- **Pros:** Trivially safe; one-line change.
- **Cons:** Throws away the ONVIF backchannel probe too, which carries no risk and is the feature's main value — nobody gets audio-send detection by default, which is most of the point of the PR. Punishes the safe probe for the unsafe one's problem.

### Keep `probeAudioSend` default true, add a default-false boolean for the VIGI half only
- **Description:** `allowVigiTalkProbe?: boolean`, default false.
- **Pros:** Simple boolean; closes the hazard.
- **Cons:** VIGI speakers are then never found unless the caller already suspects VIGI and reads the docs to find the flag — self-defeating for a capability-discovery API. It also makes `detected: null` the default outcome for every camera without an ONVIF backchannel, so the report can essentially never say "no audio-send path" without opt-in.

### Probe only when the ONVIF username is `admin`
- **Description:** Narrow the guaranteed-failure case without touching the default.
- **Pros:** Smallest change; fixes the `--user viewer` case.
- **Cons:** Leaves the real hazard untouched — an `admin` ONVIF account whose OpenAPI password differs still burns lockout attempts on every call, on any vendor's camera.

## Reasoning

The hazard is not "probing VIGI" but "probing VIGI on hardware where VIGI cannot
exist". Vendor evidence separates those two cases using data the report already
has, so the default becomes safe without becoming useless — which is what
distinguishes it from both blanket-flag alternatives. Between the flags, a
default-false boolean is the worst of the options considered: it keeps the API
honest but makes the feature undiscoverable, and it degrades `detected` to
`null` for every non-backchannel camera.

Dropping the forwarded username is chosen over mirroring `openVigiBackchannel`'s
`user || 'admin'` because the two call sites differ in kind: an explicit
`openVigiBackchannel` call is a user asking for VIGI and may legitimately name a
non-default account, whereas this is an automatic probe the caller did not ask
for, where an unusable username converts a possible success into a certain
failed auth attempt.

## Trade-offs Accepted

- Vendor-string matching is inherently approximate. A VIGI device that reports an
  unexpected `manufacturer` is not probed automatically; `'always'` covers it,
  and the report distinguishes "not VIGI" from "vendor unknown" so the gap is
  visible rather than silent.
- A three-state option is more API surface than a boolean. Justified because the
  three states are genuinely distinct intentions (trust the vendor gate / I know
  this is VIGI / never touch the OpenAPI port) and collapsing any two loses one.
- An operator whose OpenAPI account is deliberately not `admin` cannot have it
  probed automatically. Consistent with `VigiControlOptions`, which documents
  admin as the only OpenAPI account.
- The probe still costs one `doAuth` attempt per call on genuine VIGI hardware
  whose OpenAPI password differs from the ONVIF one. `'never'` is the escape
  hatch; eliminating this entirely would need separate VIGI credentials, which
  would diverge from how `openVigiBackchannel` takes credentials today.

## Related Code Paths

- `src/onvif/capabilities.ts` — `vigiHardwareLikelihood`, the `probeVigiTalk` option, the gated probe, and `defaultProbeVigiTalk` no longer forwarding the username
- `src/onvif/capabilities.test.ts` — vendor-gate cases and the username assertion
- `src/cli.ts` — `--probe-vigi-talk` passes the option through
- `src/backchannel.ts` — unchanged; `openVigiBackchannel` keeps `user || 'admin'` because an explicit VIGI request is a different situation

## Consequences

- Non-VIGI cameras never receive an unsolicited `doAuth`, so the default
  `getCameraCapabilities` call can no longer contribute to an OpenAPI lockout on
  hardware where the API does not exist.
- `audioSend.detected` gains a third meaningful outcome: `false` when VIGI is
  established as inapplicable, `null` when the vendor is unknown or the caller
  opted out.
- If more vendor-specific audio-send transports are added later, this gate is the
  place they should hook into rather than each adding its own default-on probe.
- Follow-up worth doing separately: `src/audiocheck.ts` runs the VIGI probe with
  no gate at all and should reuse this decision's helper.
