"""One-shot audio-file playback API."""

import re
from dataclasses import dataclass

from backchannel_rtp import RtpBoundaryPlan, RtpPacer, RtpPacketizer
from onvif_play import (
    aac_rfc3640_payload,
    file_aac,
    file_audio,
    file_g726,
    open_backchannel_transport,
)


SAMPLE_RATE = 8000
SAMPLES_PER_PACKET = 320
AAC_SAMPLES_PER_FRAME = 1024
CODEC_PREFERENCES = (
    "pcma",
    "pcmu",
    "g726-32",
    "g726-24",
    "g726-16",
    "g726-40",
    "aac",
)
_AAC_SAMPLE_RATES = (
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
)


@dataclass(frozen=True)
class PlaybackResult:
    codec: str
    sample_rate: int
    payload_type: int
    rtp_channel: int
    encoded_bytes: int
    packets_sent: int
    duration_seconds: float


@dataclass(frozen=True)
class _CodecSelection:
    codec: str
    payload_type: int
    sample_rate: int
    bits_per_sample: int | None = None


def _media_payload_types(send_track):
    media = re.search(
        r"^m=audio[ \t]+\d+[ \t]+\S+[ \t]+([^\r\n]+)",
        send_track,
        re.IGNORECASE | re.MULTILINE,
    )
    if media is None:
        return []
    return [
        int(value)
        for value in media.group(1).split()
        if value.isdecimal() and 0 <= int(value) <= 127
    ]


def _rtp_mappings(send_track):
    mappings = {}
    invalid = set()
    pattern = re.compile(
        r"^a=rtpmap:(\d+)[ \t]+([^/\s]+)/([0-9]+)(?:/([0-9]+))?[ \t]*\r?$",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(send_track):
        payload_type = int(match.group(1))
        mapping = (
            match.group(2).upper(),
            int(match.group(3)),
            int(match.group(4) or "1"),
        )
        if payload_type in mappings and mappings[payload_type] != mapping:
            invalid.add(payload_type)
        mappings[payload_type] = mapping
    for payload_type in invalid:
        mappings.pop(payload_type, None)
    return mappings


def _fmtp_mappings(send_track):
    mappings = {}
    pattern = re.compile(
        r"^a=fmtp:(\d+)[ \t]+([^\r\n]+)",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(send_track):
        payload_type = int(match.group(1))
        parameters = mappings.setdefault(payload_type, {})
        for raw_parameter in match.group(2).split(";"):
            if "=" not in raw_parameter:
                continue
            key, value = raw_parameter.split("=", 1)
            parameters[key.strip().lower()] = value.strip()
    return mappings


def _read_bits(data, offset, count):
    if offset + count > len(data) * 8:
        raise ValueError("truncated AudioSpecificConfig")
    value = int.from_bytes(data, "big")
    shift = len(data) * 8 - offset - count
    return (value >> shift) & ((1 << count) - 1), offset + count


def _aac_config(config):
    if not re.fullmatch(r"[0-9A-Fa-f]+", config or "") or len(config) % 2:
        raise ValueError("invalid AudioSpecificConfig")
    data = bytes.fromhex(config)
    audio_object_type, offset = _read_bits(data, 0, 5)
    if audio_object_type == 31:
        extension, offset = _read_bits(data, offset, 6)
        audio_object_type = 32 + extension
    frequency_index, offset = _read_bits(data, offset, 4)
    if frequency_index == 15:
        sample_rate, offset = _read_bits(data, offset, 24)
    elif frequency_index < len(_AAC_SAMPLE_RATES):
        sample_rate = _AAC_SAMPLE_RATES[frequency_index]
    else:
        raise ValueError("invalid AAC frequency index")
    channels, offset = _read_bits(data, offset, 4)
    if audio_object_type == 2:
        frame_length_flag, offset = _read_bits(data, offset, 1)
        if frame_length_flag:
            raise ValueError(
                "frameLengthFlag=1 selects 960-sample AAC-LC frames"
            )
        depends_on_core_coder, offset = _read_bits(data, offset, 1)
        if depends_on_core_coder:
            raise ValueError("dependsOnCoreCoder=1 is unsupported")
        extension_flag, _ = _read_bits(data, offset, 1)
        if extension_flag:
            raise ValueError("extensionFlag=1 is unsupported")
    return audio_object_type, sample_rate, channels


def _aac_error(parameters, sample_rate, channels):
    if parameters.get("mode", "").lower() != "aac-hbr":
        return "unsupported AAC fmtp: mode must be AAC-hbr"
    expected_lengths = {
        "sizelength": 13,
        "indexlength": 3,
        "indexdeltalength": 3,
    }
    for name, expected in expected_lengths.items():
        raw_value = parameters.get(name, "")
        if not raw_value.isdecimal() or int(raw_value) != expected:
            return f"unsupported AAC fmtp: {name} must be {expected}"
    if parameters.get("streamtype") != "5":
        return "unsupported AAC fmtp: streamtype must be 5"
    if (
        "constantduration" in parameters
        and parameters["constantduration"] != str(AAC_SAMPLES_PER_FRAME)
    ):
        return "unsupported AAC fmtp: constantduration must be 1024"
    try:
        object_type, config_rate, config_channels = _aac_config(
            parameters.get("config", "")
        )
    except ValueError as error:
        return f"unsupported AAC config: {error}"
    if object_type != 2:
        return "unsupported AAC config: only AAC-LC is supported"
    if config_rate != sample_rate or config_channels != channels:
        return "unsupported AAC config: rate or channel count does not match rtpmap"
    if channels != 1:
        return "unsupported AAC config: only mono is supported"
    return None


def _codec_offers(send_track):
    payload_types = _media_payload_types(send_track)
    mappings = _rtp_mappings(send_track)
    fmtp = _fmtp_mappings(send_track)
    offers = {}
    aac_errors = []
    latm_offered = False

    for payload_type in payload_types:
        mapping = mappings.get(payload_type)
        if payload_type == 8 and mapping is None:
            offers.setdefault("pcma", _CodecSelection("pcma", 8, 8000))
            continue
        if payload_type == 0 and mapping is None:
            offers.setdefault("pcmu", _CodecSelection("pcmu", 0, 8000))
            continue
        if mapping is None:
            continue
        encoding, sample_rate, channels = mapping
        if encoding in {"PCMA", "PCMU"} and sample_rate == 8000 and channels == 1:
            codec = encoding.lower()
            offers.setdefault(
                codec,
                _CodecSelection(codec, payload_type, sample_rate),
            )
            continue
        g726 = re.fullmatch(r"G726-(16|24|32|40)", encoding)
        if g726 is not None and sample_rate == 8000 and channels == 1:
            bitrate = int(g726.group(1))
            codec = f"g726-{bitrate}"
            offers.setdefault(
                codec,
                _CodecSelection(codec, payload_type, sample_rate, bitrate // 8),
            )
            continue
        if encoding == "MP4A-LATM":
            latm_offered = True
            continue
        if encoding == "MPEG4-GENERIC":
            error = _aac_error(fmtp.get(payload_type, {}), sample_rate, channels)
            if error is None:
                offers.setdefault(
                    "aac",
                    _CodecSelection("aac", payload_type, sample_rate),
                )
            else:
                aac_errors.append(error)
    return offers, aac_errors, latm_offered


def _select_codec(send_track, preference="auto"):
    if preference not in ("auto", *CODEC_PREFERENCES):
        raise ValueError(f"unsupported codec preference: {preference}")
    offers, aac_errors, latm_offered = _codec_offers(send_track)
    if preference == "auto":
        for codec in CODEC_PREFERENCES:
            if codec in offers:
                return offers[codec]
        if latm_offered:
            raise RuntimeError("MP4A-LATM is recognized but not supported")
        raise RuntimeError("camera SDP offers no supported backchannel codec")
    if preference in offers:
        return offers[preference]
    if preference == "aac":
        if aac_errors:
            raise RuntimeError(aac_errors[0])
        if latm_offered:
            raise RuntimeError("MP4A-LATM is recognized but not supported")
    raise RuntimeError(f"requested codec {preference} is not offered by camera SDP")


def _pcma_payload_type(send_track):
    return _select_codec(send_track, "pcma").payload_type


def _g726_packets(payload, bits_per_sample):
    if len(payload) * 8 % bits_per_sample:
        raise RuntimeError("G.726 payload does not end on a complete codeword")
    packet_size = SAMPLES_PER_PACKET * bits_per_sample // 8
    for offset in range(0, len(payload), packet_size):
        packet = memoryview(payload)[offset : offset + packet_size]
        if len(packet) * 8 % bits_per_sample:
            raise RuntimeError("G.726 RTP packet does not end on a complete codeword")
        yield packet, len(packet) * 8 // bits_per_sample, False


def play_file(
    *,
    host,
    file,
    user="",
    password="",
    volume=0.05,
    codec="auto",
):
    """Play one audio file through a camera speaker, then close the session."""
    if codec not in ("auto", *CODEC_PREFERENCES):
        raise ValueError(f"unsupported codec preference: {codec}")
    sent = 0
    encoded_bytes = 0
    total_samples = 0
    with open_backchannel_transport(
        host,
        user,
        password,
        transport="tcp",
    ) as session:
        selected = _select_codec(session.send_track, codec)
        packetizer = RtpPacketizer(selected.payload_type)
        pacer = RtpPacer(selected.sample_rate, mode="rebase")

        if selected.codec == "aac":
            frames = file_aac(file, volume, selected.sample_rate, 0)
            encoded_bytes = sum(len(frame) for frame in frames)
            packet_source = (
                (aac_rfc3640_payload(frame), AAC_SAMPLES_PER_FRAME, True)
                for frame in frames
            )
        elif selected.codec.startswith("g726-"):
            payload = file_g726(
                file,
                volume,
                selected.sample_rate,
                selected.bits_per_sample,
            )
            encoded_bytes = len(payload)
            packet_source = _g726_packets(payload, selected.bits_per_sample)
        else:
            if selected.codec == "pcma":
                payload = file_audio(
                    file,
                    "pcma",
                    volume,
                    selected.sample_rate,
                    encoder="python-alaw",
                )
            else:
                payload = file_audio(
                    file,
                    "pcmu",
                    volume,
                    selected.sample_rate,
                )
            encoded_bytes = len(payload)
            boundaries = RtpBoundaryPlan.fixed(
                payload,
                SAMPLES_PER_PACKET,
                sample_rate=selected.sample_rate,
                bytes_per_sample=1,
            )
            packet_source = (
                (boundary.payload, boundary.samples, False)
                for boundary in boundaries.packets
            )

        for payload, samples, marker in packet_source:
            session.check_keepalive()
            pacer.wait(samples)
            packet = packetizer.build(
                payload,
                samples,
                marker=marker or sent == 0,
            )
            session.send_rtp(packet)
            total_samples += samples
            sent += 1
        session.check_keepalive()
        pacer.finish()
        session.check_keepalive()
        rtp_channel = session.rtp_channel

    return PlaybackResult(
        codec=selected.codec.upper(),
        sample_rate=selected.sample_rate,
        payload_type=selected.payload_type,
        rtp_channel=rtp_channel,
        encoded_bytes=encoded_bytes,
        packets_sent=sent,
        duration_seconds=total_samples / selected.sample_rate,
    )
