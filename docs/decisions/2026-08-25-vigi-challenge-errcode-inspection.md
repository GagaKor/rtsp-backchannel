# The doAuth challenge's errCode is read only when the challenge block is absent

**Date:** 2026-08-25
**Status:** Accepted
**Scope:** `src/vigi/control.ts` — the first leg of the doAuth handshake

## Context

`openVigiControlWithDependencies` posts `{method:'doAuth', params:null}`, then
reaches straight into `.authenticate` and throws a flat
`invalid VIGI doAuth challenge` if it is not an object. The reply's `errCode`
is never read, so a device that has already locked the OpenAPI account —
answering `{method:'doAuth', errCode:-10022}` with no challenge at all — is
reported as a malformed challenge. `describeErrorCode` already carries the
right sentence ("VIGI OpenAPI account is locked by retry limit"), but the
challenge path cannot reach it. That is the diagnostic that matters most,
since lockout is this module's headline hazard.

The obvious fix — route the reply through `requireSuccess` like every other
call — is wrong, and the fixtures show why: a *successful* challenge carries
`errCode: -10020` ("authentication failed") **alongside** the `authenticate`
block. That is what a challenge is: the device is saying "you are not
authenticated yet, here is the nonce". `requireSuccess` throws on any non-zero
code, so it would reject the normal handshake on its first leg.

## Decision

Inspect `errCode` only on the branch where `authenticate` is missing or
unusable. A non-zero code there is reported as a `VigiControlError` through
`describeErrorCode`; the generic message remains for a reply that has no
usable challenge and no error code either.

## Alternatives Considered

### Read errCode only when the challenge block is absent — chosen
- **Description:** A helper that turns a challenge-less reply into the most specific error available.
- **Pros:** Leaves the normal handshake untouched, since the `-10020`-with-challenge case never reaches the helper. Surfaces lockout, unauthorized, and unsupported-operation codes with the wording `describeErrorCode` already has.
- **Cons:** Two places now decide what a doAuth failure means (`requireSuccess` for the second leg, this helper for the first).

### Route the challenge reply through `requireSuccess`
- **Description:** Treat the challenge like every other call.
- **Pros:** One code path, no second helper.
- **Cons:** Breaks the protocol. `-10020` on the challenge leg is expected, not an error, so this rejects every successful handshake. Verified against the module's own fixtures, which encode `errCode: -10020` in the good-path challenge.

### Whitelist `-10020` inside `requireSuccess` when the operation is the challenge
- **Description:** `requireSuccess(reply, 'doAuth', { allow: [-10020] })`.
- **Pros:** Keeps a single validation helper.
- **Cons:** Puts protocol-specific knowledge into the generic checker and makes `-10020` permanently invisible on the *second* leg, where it is a genuine failure worth reporting. Trades a real diagnostic for code-shape tidiness.

## Reasoning

The two legs of the handshake genuinely have different success conditions, so
one validator cannot serve both without being told which leg it is on —
and once it has to be told, a separate branch is simpler than a parameter.
Gating on "is there a usable challenge block" rather than on the error code
also means the check keys off the thing the code actually needs next, so a
firmware that returns some new code alongside a valid challenge keeps working.

## Trade-offs Accepted

- Two failure-classification sites in one module. Mitigated by a comment at
  each explaining which leg it guards and why the other's rule does not apply.
- A reply carrying both a usable challenge and a fatal code would be accepted
  and fail on the next leg instead. No firmware is known to do this, and
  guessing which of the two signals to trust would be inventing a rule.

## Related Code Paths

- `src/vigi/control.ts` — `challengeFailure` helper and the `authenticate` guard
- `src/vigi/control.test.ts` — locked-account and unauthorized challenge replies

## Consequences

- Lockout is now reported by name from the first request that sees it, which is
  the request most likely to see it.
- If a third leg is ever added, it needs its own explicit decision about which
  codes are expected; there is no longer a single answer to "is this reply ok".
