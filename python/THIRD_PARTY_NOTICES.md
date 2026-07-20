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

## ONVIF implementation

The ONVIF, WS-Discovery, RTP, and RTSP support in this repository is an
independent protocol implementation. The distributed packages do not include
a third-party ONVIF SDK.
