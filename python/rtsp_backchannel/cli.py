"""Command-line wrapper for the public one-shot playback API."""

import argparse
import json
import math
import sys

from .onvif import discover_devices, get_stream_uris
from .playback import play_file


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
            "rtsp-backchannel streams"
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
