"""Command-line wrapper for the public one-shot playback API."""

import argparse
import math
import os

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
        prog="onvif-backchannel",
        description="Play one audio file through an ONVIF RTSP backchannel",
    )
    parser.add_argument("--host", default="172.168.46.56")
    parser.add_argument("--user", default="admin")
    parser.add_argument(
        "--pass",
        dest="password",
        default=os.environ.get("ONVIF_PASSWORD", "CHANGEME"),
    )
    parser.add_argument("--file", required=True)
    parser.add_argument("--volume", type=_volume, default=0.05)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    result = play_file(
        host=args.host,
        user=args.user,
        password=args.password,
        file=args.file,
        volume=args.volume,
    )
    print(f"sent {result.packets_sent} RTP packets")


if __name__ == "__main__":
    main()
