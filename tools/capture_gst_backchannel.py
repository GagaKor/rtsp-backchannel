#!/usr/bin/env python3
"""Send MP3 audio over ONVIF backchannel while capturing the clean RTP output."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import struct
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

try:
    from tools.rtp_reference import (
        MAX_RTP_PACKET_SIZE,
        RtpPacketMeta,
        parse_rtp_packet,
    )
except ModuleNotFoundError:  # Direct execution adds tools/, not the repository root.
    from rtp_reference import MAX_RTP_PACKET_SIZE, RtpPacketMeta, parse_rtp_packet


class BackchannelPushError(RuntimeError):
    """Raised when rtspsrc rejects a backchannel sample or buffer."""


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=pathlib.Path, help="source MP3")
    parser.add_argument("--volume", type=float, default=0.05)
    parser.add_argument(
        "--output", required=True, type=pathlib.Path, help="capture output directory"
    )
    parser.add_argument(
        "--url",
        help="RTSP endpoint (or set ONVIF_RTSP_URL)",
    )
    return parser


def resolve_endpoint(url: str | None, environ=None) -> str:
    environment = os.environ if environ is None else environ
    endpoint = url or environment.get("ONVIF_RTSP_URL")
    if endpoint is None or not endpoint.strip():
        raise ValueError("provide --url or set ONVIF_RTSP_URL for capture")
    return endpoint


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_uri(uri: str) -> str:
    """Remove URI userinfo and redact common secret-bearing query parameters."""
    parsed = urllib.parse.urlsplit(uri)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parsed.port is not None:
            netloc += f":{parsed.port}"
    except ValueError:
        netloc = hostname

    query = [
        (key, "<redacted>")
        for key, _value in urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True
        )
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, urllib.parse.urlencode(query), "")
    )


def _redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(
        r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+",
        lambda match: redact_uri(match.group(0)),
        value,
    )


def build_session_metadata(
    arguments: argparse.Namespace,
    *,
    source_path: pathlib.Path,
    output_path: pathlib.Path,
    source_sha256: str,
    endpoint: str,
) -> dict:
    redacted_endpoint = redact_uri(endpoint)
    return {
        "schema_version": 2,
        "started_at": _utc_now(),
        "ended_at": None,
        "status": "starting",
        "source": {"path": str(source_path), "sha256": source_sha256},
        "volume": arguments.volume,
        "gstreamer_version": None,
        "command": {
            "program": pathlib.Path(sys.argv[0]).name,
            "arguments": {
                "file": str(source_path),
                "volume": arguments.volume,
                "output": str(output_path),
                "url": redacted_endpoint,
            },
        },
        "endpoint": redacted_endpoint,
        "negotiated_streams": [],
        "backchannel": {"stream_index": None, "caps": None},
        "packet_count": 0,
    }


def _library_candidates(platform: str) -> list[str]:
    if platform == "darwin":
        return ["libgstreamer-1.0.0.dylib", "libgstreamer-1.0.dylib"]
    if platform.startswith("win"):
        return ["libgstreamer-1.0-0.dll", "gstreamer-1.0-0.dll"]
    return ["libgstreamer-1.0.so.0", "libgstreamer-1.0.so"]


def load_gstreamer_library(
    *,
    find_library=ctypes.util.find_library,
    cdll=ctypes.CDLL,
    platform: str = sys.platform,
):
    candidates = []
    discovered = find_library("gstreamer-1.0")
    if discovered:
        candidates.append(discovered)
    candidates.extend(_library_candidates(platform))

    attempted = []
    for candidate in candidates:
        if candidate in attempted:
            continue
        attempted.append(candidate)
        try:
            return cdll(candidate)
        except OSError:
            continue
    raise RuntimeError(
        "unable to load the GStreamer runtime library; tried: "
        + ", ".join(attempted)
    )


def configure_legacy_push(has_sample_signal: bool, loader=load_gstreamer_library):
    if has_sample_signal:
        return None
    library = loader()
    library.gst_mini_object_ref.argtypes = [ctypes.c_void_p]
    library.gst_mini_object_ref.restype = ctypes.c_void_p
    return library


def ensure_push_succeeded(result, ok_result, operation: str):
    if result != ok_result:
        raise BackchannelPushError(
            f"{operation} returned {result}; expected {ok_result}"
        )
    return result


def _flush_fsync_close(file_object) -> None:
    primary_error = None
    try:
        file_object.flush()
        os.fsync(file_object.fileno())
    except BaseException as error:
        primary_error = error
    try:
        file_object.close()
    except BaseException as error:
        if primary_error is None:
            primary_error = error
        elif hasattr(primary_error, "add_note"):
            primary_error.add_note(f"additional close failure: {error}")
    if primary_error is not None:
        raise primary_error


def _fsync_directory(path: pathlib.Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EINVAL, errno.ENOTSUP}:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise
    finally:
        os.close(descriptor)


def _write_session_file(path: pathlib.Path, session: dict) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(session, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _artifact_metadata(path: pathlib.Path) -> dict:
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


class CaptureArtifacts:
    """Publish one internally consistent capture directory generation."""

    def __init__(self, output: pathlib.Path):
        self.output = pathlib.Path(output)
        if self.output.exists():
            raise FileExistsError(f"capture output already exists: {self.output}")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.staging = pathlib.Path(
            tempfile.mkdtemp(
                prefix=f".{self.output.name}.{self.run_id}.",
                suffix=".tmp",
                dir=self.output.parent,
            )
        )
        self.packet_path = self.staging / "packets.bin"
        self.manifest_path = self.staging / "manifest.jsonl"
        self.packet_count = 0
        self._active = True
        self._packet_file = None
        self._manifest_file = None
        try:
            self._packet_file = self.packet_path.open("xb")
            self._manifest_file = self.manifest_path.open("x", encoding="utf-8")
        except BaseException as error:
            self._abort_preserving(error)
            raise

    def record_packet(
        self,
        packet: bytes,
        *,
        relative_monotonic_ns: int,
        buffer_pts_ns: int | None,
        buffer_duration_ns: int | None,
        meta: RtpPacketMeta | None = None,
    ) -> None:
        if not self._active:
            raise RuntimeError("capture artifacts are no longer active")
        try:
            if not packet:
                raise ValueError("invalid RTP packet length 0")
            if len(packet) > MAX_RTP_PACKET_SIZE:
                raise ValueError(
                    f"RTP packet length {len(packet)} exceeds maximum "
                    f"RTP packet size {MAX_RTP_PACKET_SIZE}"
                )
            meta = meta or parse_rtp_packet(packet)
            row = {
                "packet_index": self.packet_count,
                "relative_monotonic_ns": relative_monotonic_ns,
                "buffer_pts_ns": buffer_pts_ns,
                "buffer_duration_ns": buffer_duration_ns,
                "packet_size": len(packet),
                "payload_type": meta.payload_type,
                "marker": meta.marker,
                "sequence": meta.sequence,
                "timestamp": meta.timestamp,
                "ssrc": meta.ssrc,
                "payload_size": meta.payload_size,
                "payload_sha256": meta.payload_sha256,
            }
            framed_packet = struct.pack("!I", len(packet)) + packet
            rendered_row = json.dumps(row, sort_keys=True) + "\n"
            if self._packet_file.write(framed_packet) != len(framed_packet):
                raise OSError("short write while recording packets.bin")
            if self._manifest_file.write(rendered_row) != len(rendered_row):
                raise OSError("short write while recording manifest.jsonl")
            self.packet_count += 1
        except BaseException as error:
            self._abort_preserving(error)
            raise

    def _close_capture_files(self) -> list[BaseException]:
        errors = []
        for file_object in (self._packet_file, self._manifest_file):
            if file_object is None or file_object.closed:
                continue
            try:
                _flush_fsync_close(file_object)
            except BaseException as error:
                errors.append(error)
        return errors

    def _abort_preserving(self, primary_error: BaseException) -> None:
        cleanup_errors = []
        for file_object in (self._packet_file, self._manifest_file):
            if file_object is None or file_object.closed:
                continue
            try:
                file_object.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if self.staging.exists():
            try:
                shutil.rmtree(self.staging)
            except BaseException as error:
                cleanup_errors.append(error)
        self._active = False
        if hasattr(primary_error, "add_note"):
            for cleanup_error in cleanup_errors:
                primary_error.add_note(f"capture cleanup failure: {cleanup_error}")

    def abort(self) -> None:
        if not self._active and not self.staging.exists():
            return
        marker = RuntimeError("capture aborted")
        self._abort_preserving(marker)
        notes = getattr(marker, "__notes__", [])
        if notes:
            raise RuntimeError("; ".join(notes))

    def finalize(self, session: dict) -> dict:
        if not self._active:
            raise RuntimeError("capture artifacts are no longer active")
        try:
            close_errors = self._close_capture_files()
            if close_errors:
                primary_error = close_errors[0]
                if hasattr(primary_error, "add_note"):
                    for close_error in close_errors[1:]:
                        primary_error.add_note(
                            f"additional capture close failure: {close_error}"
                        )
                raise primary_error

            published_session = dict(session)
            published_session["run_id"] = self.run_id
            published_session["artifacts"] = {
                "packets.bin": _artifact_metadata(self.packet_path),
                "manifest.jsonl": _artifact_metadata(self.manifest_path),
            }
            _write_session_file(self.staging / "session.json", published_session)
            _fsync_directory(self.staging)
            if self.output.exists():
                raise FileExistsError(f"capture output already exists: {self.output}")
            os.rename(self.staging, self.output)
            self._active = False
            return published_session
        except BaseException as error:
            self._abort_preserving(error)
            raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gst_clock_value(value: int, Gst) -> int | None:
    return None if value == Gst.CLOCK_TIME_NONE else int(value)


def _record_session_error(session: dict, error: BaseException) -> None:
    session["status"] = "error"
    session["error"] = {
        "type": type(error).__name__,
        "message": _redact_text(str(error)),
    }


def run_capture(arguments: argparse.Namespace, endpoint: str | None = None) -> int:
    endpoint = resolve_endpoint(endpoint or arguments.url)
    source_path = arguments.file.expanduser().resolve()
    output_path = arguments.output.expanduser().resolve()
    started_monotonic_ns = time.monotonic_ns()
    session = {"status": "starting"}
    artifacts = None
    pipe = None
    sender = None
    gst_module = None
    primary_error = None
    interrupted = False

    try:
        if not source_path.is_file():
            raise FileNotFoundError(f"source file does not exist: {source_path}")
        source_sha256 = sha256_file(source_path)
        artifacts = CaptureArtifacts(output_path)
        session = build_session_metadata(
            arguments,
            source_path=source_path,
            output_path=output_path,
            source_sha256=source_sha256,
            endpoint=endpoint,
        )

        # Keep GStreamer entirely inside runtime capture so imports and --help stay portable.
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, GObject, Gst

        gst_module = Gst
        Gst.init(None)
        session["gstreamer_version"] = Gst.version_string()
        loop = GLib.MainLoop()

        pipe = Gst.Pipeline.new("m")
        src = Gst.ElementFactory.make("rtspsrc", "src")
        src.set_property("location", endpoint)
        src.set_property("backchannel", 1)
        src.set_property("latency", 200)
        pipe.add(src)

        has_sample_signal = (
            GObject.signal_lookup("push-backchannel-sample", type(src)) != 0
        )
        session["push_backchannel_sample"] = has_sample_signal
        print(
            f"GStreamer {Gst.version_string()}, "
            f"push-backchannel-sample={has_sample_signal}, volume={arguments.volume}"
        )

        audio_streams = []
        backchannel_stream = [None]

        def select_stream(_source, stream_index, caps):
            structure = caps.get_structure(0)
            media = structure.get_string("media")
            send_only = structure.has_field("a-sendonly")
            session["negotiated_streams"].append(
                {
                    "stream_index": int(stream_index),
                    "media": media,
                    "send_only": send_only,
                    "caps": caps.to_string(),
                }
            )
            if media == "audio":
                audio_streams.append(stream_index)
                if send_only:
                    backchannel_stream[0] = stream_index
                    session["backchannel"] = {
                        "stream_index": int(stream_index),
                        "caps": caps.to_string(),
                    }
            return True

        def pad_added(_source, pad):
            fake_sink = Gst.ElementFactory.make("fakesink", None)
            fake_sink.set_property("sync", False)
            pipe.add(fake_sink)
            fake_sink.sync_state_with_parent()
            pad.link(fake_sink.get_static_pad("sink"))

        src.connect("select-stream", select_stream)
        src.connect("pad-added", pad_added)

        sender = Gst.parse_launch(
            f"filesrc location={json.dumps(str(source_path))} "
            "! decodebin ! audioconvert ! audioresample "
            f"! volume volume={arguments.volume} "
            "! audio/x-raw,rate=8000,channels=1,format=S16LE "
            "! alawenc ! rtppcmapay pt=8 "
            "! appsink name=out emit-signals=true sync=true"
        )
        sink = sender.get_by_name("out")
        gst_library = configure_legacy_push(has_sample_signal)
        callback_error = [None]

        def on_sample(app_sink):
            try:
                sample = app_sink.emit("pull-sample")
                if sample is None or backchannel_stream[0] is None:
                    return Gst.FlowReturn.OK
                buffer = sample.get_buffer()
                packet = bytes(buffer.extract_dup(0, buffer.get_size()))
                meta = parse_rtp_packet(packet)
                output_sample = Gst.Sample.new(
                    buffer.copy(), sample.get_caps(), None, None
                )
                if has_sample_signal:
                    operation = "push-backchannel-sample"
                    relative_monotonic_ns = (
                        time.monotonic_ns() - started_monotonic_ns
                    )
                    push_result = src.emit(
                        operation,
                        backchannel_stream[0],
                        output_sample,
                    )
                else:
                    operation = "push-backchannel-buffer"
                    gst_library.gst_mini_object_ref(hash(output_sample))
                    relative_monotonic_ns = (
                        time.monotonic_ns() - started_monotonic_ns
                    )
                    push_result = src.emit(
                        operation,
                        backchannel_stream[0],
                        output_sample,
                    )
                ensure_push_succeeded(push_result, Gst.FlowReturn.OK, operation)
                artifacts.record_packet(
                    packet,
                    relative_monotonic_ns=relative_monotonic_ns,
                    buffer_pts_ns=_gst_clock_value(buffer.pts, Gst),
                    buffer_duration_ns=_gst_clock_value(buffer.duration, Gst),
                    meta=meta,
                )
                if artifacts.packet_count == 1 or artifacts.packet_count % 100 == 0:
                    print(f"pushed {artifacts.packet_count} pkts")
                return Gst.FlowReturn.OK
            except Exception as error:
                callback_error[0] = error
                _record_session_error(session, error)
                loop.quit()
                return Gst.FlowReturn.ERROR

        sink.connect("new-sample", on_sample)
        termination_reason = [None]

        def on_bus(_bus, message):
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                session["error"] = {
                    "type": type(error).__name__,
                    "message": _redact_text(error.message),
                    "debug": _redact_text(debug),
                }
                session["status"] = "error"
                termination_reason[0] = "error"
                loop.quit()
            elif message.type == Gst.MessageType.EOS and message.src is sender:
                termination_reason[0] = "eos"
                loop.quit()
            return True

        for pipeline in (pipe, sender):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", on_bus)

        def start_sender():
            if backchannel_stream[0] is None:
                backchannel_stream[0] = (
                    audio_streams[-1] if len(audio_streams) >= 2 else None
                )
            if backchannel_stream[0] is None:
                error = RuntimeError("no backchannel stream")
                callback_error[0] = error
                _record_session_error(session, error)
                loop.quit()
                return False
            if session["backchannel"]["stream_index"] is None:
                session["backchannel"]["stream_index"] = int(backchannel_stream[0])
                for stream in session["negotiated_streams"]:
                    if stream["stream_index"] == int(backchannel_stream[0]):
                        session["backchannel"]["caps"] = stream["caps"]
                        break
            print(
                f"sending to backchannel stream {backchannel_stream[0]}: "
                f"{source_path} (vol={arguments.volume})"
            )
            sender.set_state(Gst.State.PLAYING)
            session["status"] = "capturing"
            return False

        signal_interrupted = [False]

        def stop_for_signal(_signal_number, _frame):
            signal_interrupted[0] = True
            loop.quit()

        previous_sigint = signal.signal(signal.SIGINT, stop_for_signal)
        previous_sigterm = signal.signal(signal.SIGTERM, stop_for_signal)
        try:
            pipe.set_state(Gst.State.PLAYING)
            session["status"] = "negotiating"
            GLib.timeout_add_seconds(4, start_sender)
            loop.run()
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)

        if callback_error[0] is not None:
            raise callback_error[0]
        if signal_interrupted[0]:
            interrupted = True
            raise RuntimeError("capture interrupted")
        if termination_reason[0] != "eos":
            message = session.get("error", {}).get("message", "capture terminated")
            raise RuntimeError(message)
        session["status"] = "complete"
    except Exception as error:
        primary_error = error
        _record_session_error(session, error)
    finally:
        cleanup_errors = []
        if sender is not None:
            try:
                sender.set_state(gst_module.State.NULL)
            except Exception as error:
                cleanup_errors.append(error)
        if pipe is not None:
            try:
                pipe.set_state(gst_module.State.NULL)
            except Exception as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is None:
                primary_error = cleanup_errors[0]
                _record_session_error(session, primary_error)
            if hasattr(primary_error, "add_note"):
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(f"pipeline cleanup failure: {cleanup_error}")

        session["packet_count"] = artifacts.packet_count if artifacts else 0
        session["ended_at"] = _utc_now()
        session["elapsed_monotonic_ns"] = time.monotonic_ns() - started_monotonic_ns
        if artifacts is not None:
            if primary_error is None:
                try:
                    artifacts.finalize(session)
                except Exception as error:
                    primary_error = error
                    _record_session_error(session, error)
            else:
                artifacts._abort_preserving(primary_error)

    if primary_error is not None:
        print(f"ERROR: {_redact_text(str(primary_error))}", file=sys.stderr)
        return 130 if interrupted else 1
    print(f"complete; captured packets: {artifacts.packet_count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        endpoint = resolve_endpoint(arguments.url)
    except ValueError as error:
        parser.error(str(error))
    return run_capture(arguments, endpoint)


if __name__ == "__main__":
    raise SystemExit(main())
