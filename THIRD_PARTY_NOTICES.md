# Third-Party Notices

## FFmpeg

This project does not include or link FFmpeg. At runtime, it launches a
separately installed `ffmpeg` executable to decode input audio.

FFmpeg licensing depends on how the executable was configured and built.
FFmpeg is LGPL 2.1-or-later by default, builds that enable GPL components are
GPL 2.0-or-later, and builds configured with nonfree components are not
redistributable. Anyone who bundles or redistributes FFmpeg with this project
must independently comply with the terms that apply to that exact FFmpeg
build.

See <https://ffmpeg.org/legal.html>.

## saxes

The TypeScript package uses `saxes` to parse namespace-aware ONVIF XML.
`saxes` is licensed under the ISC License.

Upstream: <https://github.com/lddubeau/saxes>

## xmlchars

`xmlchars` is a transitive dependency of `saxes` and is licensed under the MIT
License.

Upstream: <https://github.com/lddubeau/xmlchars>

## ONVIF implementation and trademark

The ONVIF, WS-Discovery, RTP, and RTSP support in this repository is an
independent protocol implementation. The distributed packages do not include
a third-party ONVIF SDK.

ONVIF is a trademark of ONVIF, Inc. This project is not affiliated with or
endorsed by ONVIF, Inc. and does not claim ONVIF Profile conformance.

## Trademarks

TP-Link and VIGI are trademarks of TP-Link Systems Inc. This project is not
affiliated with, endorsed by, or sponsored by TP-Link. The VIGI OpenAPI
transport is an independent implementation written from TP-Link's publicly
published protocol documentation; no TP-Link code or documentation is
redistributed here.
