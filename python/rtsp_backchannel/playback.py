"""One-shot audio-file playback API."""

import re
from dataclasses import dataclass

from backchannel_rtp import RtpBoundaryPlan, RtpPacer, RtpPacketizer
from onvif_play import file_audio, open_backchannel_transport


SAMPLE_RATE = 8000
SAMPLES_PER_PACKET = 320


@dataclass(frozen=True)
class PlaybackResult:
    codec: str
    sample_rate: int
    payload_type: int
    rtp_channel: int
    encoded_bytes: int
    packets_sent: int
    duration_seconds: float


def _pcma_payload_type(send_track):
    mapping = re.search(
        r"^a=rtpmap:(\d+)\s+PCMA/8000(?:\D|$)",
        send_track,
        re.IGNORECASE | re.MULTILINE,
    )
    if mapping is not None:
        return int(mapping.group(1))
    media = re.search(r"^m=audio\s+\d+\s+\S+\s+(.+)$", send_track, re.MULTILINE)
    if media is not None and "8" in media.group(1).split():
        return 8
    raise RuntimeError("camera SDP offers no PCMA/8000 backchannel codec")


def play_file(*, host, user, password, file, volume=0.05):
    """Play one audio file through a camera speaker, then close the session."""
    payload = file_audio(
        file,
        "pcma",
        volume,
        SAMPLE_RATE,
        encoder="python-alaw",
    )
    sent = 0
    with open_backchannel_transport(
        host,
        user,
        password,
        transport="tcp",
    ) as session:
        payload_type = _pcma_payload_type(session.send_track)
        packetizer = RtpPacketizer(payload_type)
        pacer = RtpPacer(SAMPLE_RATE, mode="rebase")
        boundaries = RtpBoundaryPlan.fixed(
            payload,
            SAMPLES_PER_PACKET,
            sample_rate=SAMPLE_RATE,
            bytes_per_sample=1,
        )
        for boundary in boundaries.packets:
            session.check_keepalive()
            pacer.wait(boundary.samples)
            packet = packetizer.build(
                boundary.payload,
                boundary.samples,
                marker=sent == 0,
            )
            session.send_rtp(packet)
            sent += 1
        session.check_keepalive()
        pacer.finish()
        session.check_keepalive()
        rtp_channel = session.rtp_channel

    return PlaybackResult(
        codec="PCMA",
        sample_rate=SAMPLE_RATE,
        payload_type=payload_type,
        rtp_channel=rtp_channel,
        encoded_bytes=len(payload),
        packets_sent=sent,
        duration_seconds=len(payload) / SAMPLE_RATE,
    )
