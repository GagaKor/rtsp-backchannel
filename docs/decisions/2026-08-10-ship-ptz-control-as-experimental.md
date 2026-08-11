# Ship PTZ movement control as experimental rather than blocking on hardware

**Date:** 2026-08-10
**Status:** Accepted
**Scope:** PTZ control feature in all three implementations; 0.3.0 release contents

## Context

PTZ control was requested for the 0.3.0 release. Every other feature in this
library was verified against a real camera before shipping — the capability
report was run end to end against a vht VNV84371MR and its output published in
the pull request. PTZ cannot get the same treatment: that camera reports
`ptz.detected: false`, so there is no PTZ hardware available to exercise
movement. Control also carries a risk class the read-only work did not. A wrong
read request returned wrong data; a wrong control request moves a physical
camera, and a `ContinuousMove` that is never stopped leaves it moving.

## Decision

Ship PTZ movement in 0.3.0 marked experimental. Verify everything that can be
verified without PTZ hardware — request-body correctness, capability guarding,
timeout inclusion, stop-on-close, and clean failure against a real camera that
has no PTZ service — and state explicitly, in both the six READMEs and the API
doc comments, that physical movement is unverified.

## Alternatives Considered

### Ship experimental with the boundary documented (chosen)

- **Description:** Release the feature, verify the request path exhaustively against fake servers plus the one real-hardware negative path, and mark the movement behaviour as unverified in READMEs and in JSDoc/docstrings/rustdoc.
- **Pros:**
  - The feature is usable now by anyone who does have PTZ hardware, which is how the gap actually gets closed.
  - Everything except the final physical effect genuinely is verified; withholding the whole feature over one unverifiable layer overstates the uncertainty.
  - Marking it in doc comments as well as the README reaches consumers who never open the README — editor tooltips and docs.rs.
- **Cons:**
  - A consumer could take "0.3.0 ships PTZ control" at face value and deploy it untested.
  - The library's existing standard — nothing ships unverified against real hardware — is relaxed.

### Block the feature until a PTZ camera is available

- **Description:** Build it, hold it out of 0.3.0, release once movement is confirmed on real hardware.
- **Pros:** Preserves the existing verification standard exactly; no consumer can be surprised.
- **Cons:** Ties the release to hardware procurement with no known date. The code would sit unreleased and drift from master, and nobody with PTZ hardware could help verify it because it would not be published.

### Ship unmarked

- **Description:** Release it like any other feature.
- **Pros:** Simplest.
- **Cons:** Misrepresents the verification state. Rejected without discussion.

## Reasoning

The verification gap is narrower than "PTZ control is untested" suggests, and
saying so precisely is more useful than withholding the feature. What cannot be
checked is exactly one layer: that a correct SOAP request produces the intended
physical motion. Everything beneath it — that the right bytes are sent, that
unsupported operations never reach the network, that a runaway cannot outlive
the client — is testable and is tested. Blocking would also be
self-defeating: unpublished code cannot be verified by the people who do own PTZ
cameras. Marking the boundary in doc comments rather than the README alone is
what makes this honest in practice, since library consumers read tooltips far
more often than they read READMEs.

## Trade-offs Accepted

- The project's "verified against real hardware before shipping" standard is relaxed for one feature, and the marking is the only thing preventing misuse. It has to be specific about which layer is unverified, not a vague "experimental" label.
- If the movement path turns out to be wrong on real cameras, a fix lands in a later 0.x release. Acceptable at 0.x; this decision should be revisited before 1.0.

## Related Code Paths

- `docs/superpowers/specs/2026-08-10-onvif-ptz-control-design.md` — the design this decision governs.
- `src/onvif/ptz.ts`, `python/rtsp_backchannel/ptz.py`, `rust/src/onvif/ptz.rs` — modules carrying the experimental doc comments.
- The six READMEs — where the verified/unverified boundary is stated.
- `CHANGELOG.md` — 0.3.0 entry noting the experimental status.

## Consequences

- The experimental marking is removed only after movement is exercised on real PTZ hardware; until then it stays in both the READMEs and the doc comments.
- Whoever obtains a PTZ camera should run continuous, absolute, and relative moves plus stop, and confirm the device-side timeout actually halts an un-stopped continuous move — that last one is the runaway guard and is the single most important thing to confirm.
- Future features that cannot be hardware-verified should follow this pattern: state the exact unverified layer rather than labelling the whole feature experimental.
