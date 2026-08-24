# VIGI OpenAPI Talk as a Selectable Audio-Send Transport

**Date:** 2026-08-24
**Status:** Approved
**Target release:** 0.4.0

## Goal

Let `playFile` and `openBackchannel` send audio to cameras that have a speaker
but no ONVIF backchannel, by adding TP-Link's VIGI OpenAPI `talk` protocol as a
second transport behind the existing `BackchannelSession` interface. The
transport is selectable, and when the caller says nothing the library picks one
by probing the camera.

## Motivation

A TP-Link VIGI C540V on the test network has a working speaker — `getSpeakerVolume`
returns 80, and a 3-second tone played audibly through it on 2026-08-24 — yet the
library cannot reach it. The camera answers an RTSP `DESCRIBE` carrying
`Require: www.onvif.org/ver20/backchannel` with the same SDP it returns without
the header: one `a=recvonly` PCMA track and no `a=sendonly` section, so
`openBackchannel` fails with `no sendonly backchannel audio track`. ONVIF
`GetAudioOutputs` and `GetAudioOutputConfigurations` both answer
`ter:ActionNotSupported`. This is unchanged between firmware 2.2.0 and 2.3.3
(the latest), and TP-Link's published position is that VIGI ONVIF targets
Profile S, which has no backchannel — so no firmware will fix it. TP-Link
exposes two-way audio only through its own OpenAPI, which
[56 IPC models](https://www.tp-link.com/en/vigi-open-api/product-list/) support.

The capability report is actively wrong about such a camera today: it reports
`hasAudioOutput: false` on every profile and `audiocheck` concludes
"양방향 음성 가능: ❌", when the hardware in fact has a reachable speaker.

## Non-goals

- **VIGI NVR OpenAPI.** A separate protocol with its own specification document.
  Only the IPC (camera) protocol is in scope.
- **Device speaker volume.** `setSpeakerVolume` changes persistent device
  configuration; the library will not call it. The existing software `volume`
  gain is unchanged.
- **Receiving audio, preview, playback, recording, or PTZ over OpenAPI.** Only
  the `talk` (audio send) operation.
- **OpenAPI Discovery Protocol (ODP).** ONVIF WS-Discovery already covers
  discovery.
- **Enabling OpenAPI on the camera.** It is off by default and must be turned on
  at Settings > Network Settings > OpenAPI in the camera's web UI. The library
  detects that it is off and says so; it does not change the setting.

## Transport selection

```ts
export type BackchannelTransport = 'auto' | 'onvif' | 'vigi';

export interface BackchannelOptions {
  codec?: CodecPreference;
  transport?: BackchannelTransport;   // default 'auto'
}
export interface PlaybackOptions {
  /* existing fields unchanged */
  transport?: BackchannelTransport;   // default 'auto'
}
```

`openBackchannel` becomes the selector:

| `transport` | Behaviour |
|---|---|
| `'onvif'` | Today's path, unchanged. Never attempts VIGI. |
| `'vigi'` | VIGI only. VIGI failures propagate as themselves. |
| `'auto'` (default) | ONVIF first; fall back to VIGI **only** on "no backchannel". |

### The fallback trigger is deliberately narrow

Exactly one ONVIF outcome triggers the VIGI attempt: the
`no sendonly backchannel audio track` condition raised after a successful
`DESCRIBE`. Every other failure — transport error, authentication failure, no
media profiles, a non-200 `OPTIONS` or `DESCRIBE` — propagates unchanged.

A broad fallback would hide real faults behind a second, unrelated failure. A
camera that is unreachable must report that it is unreachable, not
"VIGI OpenAPI unavailable".

### Account-lockout safety

TP-Link's control interface has a retry-lockout counter: error `-10022`,
"The number of retries has been exceeded, and it has been locked". A detection
path that authenticates speculatively could lock the operator out of their own
camera. Three rules prevent it:

1. Attempt `doAuth` only after confirming TCP reachability on the OpenAPI port,
   with a short timeout.
2. In `'auto'`, attempt VIGI only when ONVIF authentication has already
   succeeded. Reaching the `DESCRIBE` stage proves the credentials are correct,
   so detection can never advance the lockout counter with a bad password.
3. Attempt `doAuth` exactly once. Surface `-10020` (authentication failed) and
   `-10022` (locked) as distinct errors and never retry either.

Rule 2 does not apply to `transport: 'vigi'`, where the caller has named the
transport and no ONVIF exchange happens. There, a single `doAuth` attempt with
the caller's own credentials carries the same risk as any other login, and rules
1 and 3 still hold.

### Failure message

When `'auto'` exhausts both transports, one error names both attempts rather
than surfacing only the last:

```
no audio send path: ONVIF backchannel absent (no sendonly track);
VIGI OpenAPI unavailable (port 20443 closed)
```

### Compatibility

The default changes behaviour in exactly one case: a camera that previously
raised `no sendonly backchannel audio track` may now succeed. That is the point
of the feature. For a non-VIGI camera the cost is one extra TCP connect attempt
against the OpenAPI port before the same failure, bounded by a short timeout.

## The VIGI transport

### Protocol summary

Established by probing the camera and reading TP-Link's published
*VIGI IPC Open API Document V1.1*. Two channels:

**Control** — HTTPS on the OpenAPI control port. TP-Link documents 20443 as the
default and ODP as the way to discover a changed one; since ODP is a non-goal,
the port is an option defaulting to 20443. The *stream* port is not guessed — the
control channel's `getStreamPort` reports it.

1. `POST https://<host>:20443` with `{"method":"doAuth","params":null}`. The
   challenge comes back under an `authenticate` object carrying `realm`,
   `nonce`, `algorithm`, `uri`, and `method`, alongside `errCode: -10020`.
2. `A1 = SHA256(admin:<realm>:<password>)`, `A2 = SHA256(<method>:<uri>)`,
   `response = SHA256(A1:<nonce>:A2)`.
3. `POST` again with `{"method":"doAuth","params":{nonce, response}}` to receive
   `stok`, valid for half an hour.
4. Later calls `POST` to `https://<host>:20443/stok=<stok>`.

**Stream** — RTSP-framed on the stream port (554 by default):

```
MULTITRANS rtsp://<host>/multitrans RTSP/1.0
CSeq: <n>
Content-Type: application/json
Content-Length: <n>
Authorization: Digest ...

{"type":"request","seq":"1","params":{"method":"get","talk":{"mode":"half_duplex"}}}
```

A `200 OK` carries `{"error_code":0,"session_id":"..."}`. Audio then flows on the
same socket as interleaved RTP over TCP — `$`, 1-byte channel, 2-byte length,
RTP header, payload — G.711 only.

Verified working parameters: mode `half_duplex`, channel `0`, payload type 8
(PCMA), 20 ms / 160-byte frames, marker bit set on the first packet.

### Module layout

Each port mirrors its existing conventions:

```
src/vigi/control.ts              + src/vigi/control.test.ts
src/vigi/talk.ts                 + src/vigi/talk.test.ts
python/rtsp_backchannel/vigi.py  + python/test_vigi.py
rust/src/vigi/{control.rs,talk.rs}
```

`control` covers `doAuth` → `stok`, `getStreamPort`, and `getAudioCapability`.
`talk` covers the `MULTITRANS` handshake and the RTP send loop.

### Reuse

Most of what the transport needs already exists and is used unchanged:

- `src/rtp/sender.ts` — `RtpPacketizer` and `interleave(channel, rtp)` already
  produce the exact `$ / channel / length / RTP` framing VIGI specifies.
- `src/audio/g711.ts` — `pcm16ToG711` for a-law encoding.
- `src/backchannel.ts` — `sendPacedG711` for real-time pacing.

Genuinely new: the control client and the `MULTITRANS` handshake.

### Digest authentication changes

Digest computation currently lives inside `RtspClient` and is MD5-only.
`MULTITRANS` needs `algorithm="SHA-256"` with `qop="auth"`. Extract the
computation into `src/rtsp/digest.ts` (and each port's equivalent) and split the
two needs:

- **Algorithm-aware hashing is shared.** Selecting MD5 or SHA-256 from the
  server's `algorithm` parameter is RFC 7616 conformance, and it benefits the
  ONVIF path too. Against an MD5-only server the request bytes are unchanged.
- **Quoting `qop` and `nc` is isolated behind a client flag.** RFC 7616 defines
  `qop` in the `Authorization` header as a token, so the standard form is
  unquoted, and it must stay that way for ONVIF. This camera answers `401` to
  the standard form and `200` to the quoted form shown in TP-Link's document, so
  the VIGI client sets `quoteAuthParameters` and a comment records the measured
  evidence.

The extraction is scoped to what this work needs. `RtspClient` keeps its own
socket and response handling; the VIGI talk client is a separate thin client
because it issues one request type, so no JSON-body branch is added to
`RtspClient`.

### Session lifecycle: the stream connection is lazy

`openBackchannel` returning a VIGI session performs the control exchange only —
`doAuth`, `getStreamPort`, `getAudioCapability`. The `MULTITRANS` TCP session
opens on the first `send()`.

`playFile` opens the session before transcoding, and an ffmpeg decode can take
seconds. VIGI documents no keep-alive for a talk session, so holding one open
across a decode would be an unforced risk. Deferring the connection removes the
question, and `withKeepAlive` on a VIGI session simply runs the operation
directly.

### Detection predicate

A camera is VIGI-talk-capable when `doAuth` succeeds **and**
`getAudioCapability` reports a speaker. Either one alone is not enough:
authentication proves only that OpenAPI is on.

### Codec

A VIGI session reports `codec` as PCMA / 8000 Hz / payload type 8.

Codec handling follows the rule already stated in `src/rtsp/sdp.ts`: "Explicit
preferences never fall back to a different codec." So `codec: 'auto'` — the
default — resolves to PCMA and simply works, while an explicit `'aac'` or
`'g726'` is rejected when the session opens, exactly as the ONVIF path rejects a
codec a camera did not offer. Silently substituting for VIGI alone would make it
the one transport that ignores an explicit instruction.

Explicit `'pcmu'` is rejected on the same rule. TP-Link's document says only
"G711", the camera advertises `G711alaw`, and µ-law has not been tested against
hardware.

## Capability report

`CameraCapabilityReport` gains an `audioSend` block, shaped like the existing
`ptz` and `media2` blocks — a `detected` summary plus the specifics behind it,
with `null` meaning the fact could not be established:

```jsonc
"audioSend": {
  "detected": true,
  "transport": "vigi",        // 'onvif' | 'vigi' | null
  "onvifBackchannel": false,  // sendonly track present in the SDP
  "vigiTalk": true            // OpenAPI reachable and reporting a speaker
}
```

This costs the report one RTSP `DESCRIBE` — it performs no RTSP today — and,
when that finds no backchannel, one OpenAPI probe. Both follow the report's
existing enrichment contract, already documented in the README: an enrichment
failure adds a `warnings` entry and leaves its field `null`, and never fails the
report. The lockout rules apply unchanged; the report has authenticated over
ONVIF before the VIGI probe can run, so rule 2 is satisfied by construction.

`src/audiocheck.ts` and its sibling scripts are updated to report the VIGI path.
They currently conclude that two-way audio is impossible on a camera where it
works.

## Public surface

- **TypeScript** — export `BackchannelTransport`; add `transport` to
  `BackchannelOptions` and `PlaybackOptions`; add `--transport auto|onvif|vigi`
  to the `play` CLI. The `capabilities` CLI gains `audioSend` through the report.
- **Python** — a `transport` keyword argument on the equivalent entry points.
- **Rust** — a `BackchannelTransport` enum and the matching option field.

## Legal and documentation constraints

TP-Link's *VIGI IPC Open API Document V1.1* is downloadable from the public
support site without login or click-through, and its 149 pages carry no
copyright notice, confidentiality marking, licence terms, or NDA language.
TP-Link documents the API for third-party integration and requires no
registration, API key, or developer agreement. Implementing a protocol from such
a document is interoperability work. Authentication uses the operator's own
credentials against their own device, so nothing is circumvented.

Three constraints follow and are binding on the implementation:

1. **Do not redistribute the document, and do not copy its prose or tables.**
   All descriptions in this repository are written from scratch. Method names,
   field names, and error codes are functional identifiers and may be used.
2. **Trademark.** `VIGI` and `TP-Link` are TP-Link marks. Descriptive use — a
   `'vigi'` option value, "supports TP-Link VIGI cameras" — is nominative fair
   use. Package names must not contain `vigi`, and nothing may imply affiliation
   or endorsement. `THIRD_PARTY_NOTICES.md` gains a disclaimer to that effect.
3. **No TP-Link code enters the repository.** Only the published document was
   read, so the MIT OR Apache-2.0 dual licence is unaffected.

The README must state that VIGI support is model-dependent, pointing at
TP-Link's maintained device list rather than claiming every VIGI camera, and
that NVRs are out of scope.

## Testing

Unit tests use in-process fake servers, following the pattern already in
`src/onvif/ptz.test.ts`: a fake HTTPS control endpoint and a fake `MULTITRANS`
server.

The selection matrix is the centre of the test effort:

| Camera | `'auto'` | `'onvif'` | `'vigi'` |
|---|---|---|---|
| ONVIF backchannel present, no OpenAPI | ONVIF session | ONVIF session | VIGI error (never silently uses ONVIF) |
| VIGI only | VIGI session | `no sendonly...` | VIGI session |
| Neither | combined error | `no sendonly...` | VIGI error |
| Unreachable / auth failure | **propagates, no fallback** | propagates | propagates |

That last row is the regression guard for the narrow-trigger rule.

A `rust/tests/fixtures/vigi-request-parity.json` fixture — the `doAuth` body, the
`MULTITRANS` request line and JSON body, and the RTP framing bytes — is read by
all three test suites, mirroring `ptz-request-parity.json`.

Digest tests pin that the MD5 path still produces byte-identical output, cover
the SHA-256 path, and cover the parameter-quoting flag in both positions.

Hardware verification cannot run in CI. The manual procedure is documented:
enable OpenAPI, lower the device speaker volume, play a short tone, restore the
volume.

## Open questions

None. Selection semantics, lockout safety, codec policy, and legal constraints
are settled above.
