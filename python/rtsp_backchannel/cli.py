"""Command-line wrapper for the public one-shot playback API."""

import argparse
import json
import math
import os
import sys

from .capabilities import CameraCapabilityReport, get_camera_capabilities
from .onvif import discover_devices, get_stream_uris
from .playback import play_file


_MAX_CAPABILITY_TIMEOUT_MS = 86_400_000.0
_CAPABILITY_TERMINATOR_ERROR = (
    "capabilities does not accept an argument terminator"
)
_CAPABILITY_OPTION_NAMES = (
    "host",
    "user",
    "pass",
    "device-url",
    "timeout-ms",
)


def _volume(value):
    try:
        volume = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("volume must be a number") from error
    if not math.isfinite(volume) or not 0.0 <= volume <= 1.0:
        raise argparse.ArgumentTypeError(
            "volume must be finite and between 0 and 1"
        )
    return volume


def _parser():
    parser = argparse.ArgumentParser(
        prog="rtsp-backchannel",
        description="Play one audio file through an ONVIF RTSP backchannel",
        epilog=(
            "Other commands: rtsp-backchannel discover; "
            "rtsp-backchannel streams; rtsp-backchannel capabilities"
        ),
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="")
    parser.add_argument(
        "--pass",
        dest="password",
        default="",
    )
    parser.add_argument("--file", required=True)
    parser.add_argument("--volume", type=_volume, default=0.05)
    parser.add_argument(
        "--codec",
        choices=(
            "auto",
            "pcma",
            "pcmu",
            "g726-16",
            "g726-24",
            "g726-32",
            "g726-40",
            "aac",
        ),
        default="auto",
    )
    return parser


def _nonnegative_integer(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def _positive_integer(value):
    parsed = _nonnegative_integer(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _port(value):
    parsed = _positive_integer(value)
    if parsed > 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


def _concurrency(value):
    parsed = _positive_integer(value)
    if parsed > 256:
        raise argparse.ArgumentTypeError("must be between 1 and 256")
    return parsed


def _discovery_parser():
    parser = argparse.ArgumentParser(
        prog="rtsp-backchannel discover",
        description="Discover local or explicitly selected ONVIF devices",
    )
    parser.add_argument(
        "--timeout-ms",
        type=_nonnegative_integer,
        default=3000,
        help="discovery timeout in milliseconds (default: 3000)",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="local PC IPv4 address for WS-Discovery (repeatable)",
    )
    parser.add_argument(
        "--cidr",
        action="append",
        dest="cidrs",
        help="target IPv4 address or CIDR (repeatable)",
    )
    parser.add_argument(
        "--port",
        action="append",
        dest="ports",
        type=_port,
        help="ONVIF Device Service port (repeatable)",
    )
    parser.add_argument(
        "--concurrency",
        type=_concurrency,
        help="concurrent CIDR hosts (default: 64)",
    )
    return parser


def _streams_parser():
    parser = argparse.ArgumentParser(
        prog="rtsp-backchannel streams",
        description="Resolve every ONVIF media profile RTSP URI",
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="")
    parser.add_argument(
        "--pass",
        dest="password",
        default="",
    )
    parser.add_argument("--device-url", action="append", dest="device_urls")
    return parser


def _nonempty_text(value):
    if value == "":
        raise argparse.ArgumentTypeError("must not be empty")
    return value


class _NonemptyTextAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if values == "":
            raise argparse.ArgumentError(self, "must not be empty")
        setattr(namespace, self.dest, values)


def _positive_finite_number(value):
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "timeout-ms must be finite and greater than 0"
        ) from error
    if (
        not math.isfinite(parsed)
        or parsed <= 0
        or parsed / 1000.0 <= 0
    ):
        raise argparse.ArgumentTypeError(
            "timeout-ms must be finite and greater than 0"
        )
    if parsed > _MAX_CAPABILITY_TIMEOUT_MS:
        raise argparse.ArgumentTypeError(
            "timeout-ms exceeds the 24-hour maximum"
        )
    return parsed


def _capability_option_name(value):
    for name in _CAPABILITY_OPTION_NAMES:
        if value == f"--{name}":
            return name
    return None


def _is_known_capability_flag(value):
    return value in ("-h", "--help") or any(
        value == f"--{name}" or value.startswith(f"--{name}=")
        for name in _CAPABILITY_OPTION_NAMES
    )


def _normalize_capability_arguments(arguments, parser):
    if "--" in arguments:
        index = arguments.index("--")
        previous = arguments[index - 1] if index else None
        option = _capability_option_name(previous)
        if option is not None:
            parser.error(f"missing value for --{option}")
        parser.error(_CAPABILITY_TERMINATOR_ERROR)

    normalized = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument != "--pass":
            normalized.append(argument)
            index += 1
            continue
        if index + 1 >= len(arguments):
            parser.error("missing value for --pass")
        value = arguments[index + 1]
        if _is_known_capability_flag(value):
            parser.error("missing value for --pass")
        if value.startswith("-"):
            normalized.append(f"--pass={value}")
        else:
            normalized.extend((argument, value))
        index += 2
    return normalized


def _capabilities_parser():
    parser = argparse.ArgumentParser(
        prog="rtsp-backchannel capabilities",
        description="Report read-only ONVIF camera capability evidence",
    )
    parser.add_argument("--host", required=True, type=_nonempty_text)
    parser.add_argument(
        "--user",
        default="",
        action=_NonemptyTextAction,
    )
    parser.add_argument(
        "--pass",
        dest="password",
        default=None,
        help="password; defaults to ONVIF_PASSWORD when omitted",
    )
    parser.add_argument(
        "--device-url",
        action="append",
        dest="device_urls",
        type=_nonempty_text,
        help="ONVIF Device service URL (repeatable; supplied order is kept)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=_positive_finite_number,
        default=None,
        help=(
            "finite positive per-request timeout in milliseconds "
            "(maximum: 24 hours)"
        ),
    )
    return parser


def _device_json(device):
    result = {
        "ip": device.ip,
        "xaddrs": device.xaddrs,
        "scopes": device.scopes,
    }
    if device.name is not None:
        result["name"] = device.name
    if device.hardware is not None:
        result["hardware"] = device.hardware
    if device.endpoint_reference is not None:
        result["endpointReference"] = device.endpoint_reference
    return result


def _stream_json(stream):
    result = {"profileToken": stream.profile_token}
    if stream.profile_name is not None:
        result["profileName"] = stream.profile_name
    result["uri"] = stream.uri
    return result


def _camera_capability_json(report: CameraCapabilityReport):
    device = {}
    if report.device.manufacturer is not None:
        device["manufacturer"] = report.device.manufacturer
    if report.device.model is not None:
        device["model"] = report.device.model
    if report.device.firmware is not None:
        device["firmware"] = report.device.firmware
    if report.device.serial is not None:
        device["serial"] = report.device.serial

    services = []
    for service in report.services:
        item = {
            "namespace": service.namespace,
            "xaddr": service.xaddr,
        }
        if service.version is not None:
            item["version"] = {
                "major": service.version.major,
                "minor": service.version.minor,
            }
        services.append(item)

    profiles = []
    for profile in report.profiles:
        item = {
            "token": profile.token,
            "source": profile.source,
            "hasAudioEncoder": profile.has_audio_encoder,
            "hasAudioOutput": profile.has_audio_output,
            "hasAudioSource": profile.has_audio_source,
        }
        if profile.name is not None:
            item["name"] = profile.name
        if profile.ptz_configuration_token is not None:
            item["ptzConfigurationToken"] = (
                profile.ptz_configuration_token
            )
        if profile.ptz_node_token is not None:
            item["ptzNodeToken"] = profile.ptz_node_token
        profiles.append(item)

    ptz = {
        "detected": report.ptz.detected,
        "panTiltSupported": report.ptz.pan_tilt_supported,
        "zoomSupported": report.ptz.zoom_supported,
        "profileTokens": list(report.ptz.profile_tokens),
    }
    if report.ptz.service_capabilities is not None:
        capabilities = {}
        source = report.ptz.service_capabilities
        if source.e_flip is not None:
            capabilities["eFlip"] = source.e_flip
        if source.reverse is not None:
            capabilities["reverse"] = source.reverse
        if source.get_compatible_configurations is not None:
            capabilities["getCompatibleConfigurations"] = (
                source.get_compatible_configurations
            )
        if source.move_status is not None:
            capabilities["moveStatus"] = source.move_status
        if source.status_position is not None:
            capabilities["statusPosition"] = source.status_position
        ptz["serviceCapabilities"] = capabilities
    nodes = []
    for node in report.ptz.nodes:
        item = {
            "token": node.token,
            "spaces": {
                "absolutePanTilt": node.spaces.absolute_pan_tilt,
                "absoluteZoom": node.spaces.absolute_zoom,
                "relativePanTilt": node.spaces.relative_pan_tilt,
                "relativeZoom": node.spaces.relative_zoom,
                "continuousPanTilt": node.spaces.continuous_pan_tilt,
                "continuousZoom": node.spaces.continuous_zoom,
            },
        }
        if node.name is not None:
            item["name"] = node.name
        if node.maximum_presets is not None:
            item["maximumPresets"] = node.maximum_presets
        if node.home_supported is not None:
            item["homeSupported"] = node.home_supported
        item["auxiliaryCommands"] = list(node.auxiliary_commands)
        nodes.append(item)
    ptz["nodes"] = nodes

    return {
        "device": device,
        "scopes": list(report.scopes),
        "declaredProfiles": list(report.declared_profiles),
        "serviceDiscovery": report.service_discovery,
        "services": services,
        "profiles": profiles,
        "ptz": ptz,
        "media2": {
            "detected": report.media2.detected,
            "encodings": list(report.media2.encodings),
            "h265Supported": report.media2.h265_supported,
        },
        "warnings": [
            {"operation": warning.operation, "message": warning.message}
            for warning in report.warnings
        ],
    }


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["discover"]:
        args = _discovery_parser().parse_args(arguments[1:])
        discovery_options = dict(
            timeout=args.timeout_ms / 1000.0,
            interfaces=args.interfaces,
        )
        if args.cidrs:
            discovery_options["cidrs"] = args.cidrs
        if args.ports:
            discovery_options["ports"] = args.ports
        if args.concurrency is not None:
            discovery_options["concurrency"] = args.concurrency
        devices = discover_devices(**discovery_options)
        for device in devices:
            print(json.dumps(_device_json(device), ensure_ascii=False))
        return
    if arguments[:1] == ["streams"]:
        args = _streams_parser().parse_args(arguments[1:])
        streams = get_stream_uris(
            host=args.host,
            user=args.user,
            password=args.password,
            device_urls=args.device_urls,
        )
        for stream in streams:
            print(json.dumps(_stream_json(stream), ensure_ascii=False))
        return
    if arguments[:1] == ["capabilities"]:
        parser = _capabilities_parser()
        normalized = _normalize_capability_arguments(arguments[1:], parser)
        args = parser.parse_args(normalized)
        capability_options = dict(
            host=args.host,
            user=args.user,
            password=(
                os.environ.get("ONVIF_PASSWORD", "")
                if args.password is None
                else args.password
            ),
        )
        if args.device_urls is not None:
            capability_options["device_urls"] = args.device_urls
        if args.timeout_ms is not None:
            capability_options["timeout"] = args.timeout_ms / 1000.0
        report = get_camera_capabilities(**capability_options)
        print(
            json.dumps(
                _camera_capability_json(report),
                ensure_ascii=False,
            )
        )
        return
    if arguments[:1] == ["play"]:
        arguments = arguments[1:]

    args = _parser().parse_args(arguments)
    result = play_file(
        host=args.host,
        user=args.user,
        password=args.password,
        file=args.file,
        volume=args.volume,
        codec=args.codec,
    )
    print(f"sent {result.packets_sent} RTP packets")


if __name__ == "__main__":
    main()
