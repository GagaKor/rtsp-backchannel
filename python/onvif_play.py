#!/usr/bin/env python3
"""
Send audio to a camera speaker via ONVIF + RTSP backchannel.
Python equivalent of the TS PoC (m3/cli) for cross-verification.

  python3 python/onvif_play.py --host 172.168.46.56 --user admin --pass CHANGEME
  python3 python/onvif_play.py --file test.mp3 --host 172.168.46.56
"""
import argparse, base64, datetime, hashlib, math, os, pathlib, queue, re, socket, ssl, stat, struct, subprocess, threading, time, urllib.parse, urllib.request

import backchannel_audio
from backchannel_rtp import (
    RtpBoundaryPlan,
    RtpPacer,
    RtpPacketizer,
    TIMING_LOG_MAX_BYTES,
    TIMING_LOG_MAX_LINE_BYTES,
    TIMING_LOG_MAX_ROWS,
    atomic_write_jsonl,
    load_packet_pattern,
    paths_refer_to_same_file,
    remove_output,
)

DEV = "http://www.onvif.org/ver10/device/wsdl"
MED = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PDT = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BACKCHANNEL = "www.onvif.org/ver20/backchannel"
CTX = ssl._create_unverified_context()
RTSP_IO_TIMEOUT_SECONDS = 8.0
RTSP_MAX_HEADER_BYTES = 64 * 1024
RTSP_MAX_BODY_BYTES = 4 * 1024 * 1024
RTSP_MAX_BUFFER_BYTES = 8 * 1024 * 1024
RTSP_MAX_QUEUED_RESPONSES = 64
DEFAULT_RTSP_SESSION_TIMEOUT_SECONDS = 60
MAX_RTSP_SESSION_TIMEOUT_SECONDS = int(threading.TIMEOUT_MAX) * 2
RTSP_KEEPALIVE_METHODS = ("SET_PARAMETER", "GET_PARAMETER", "OPTIONS")
RTSP_KEEPALIVE_MAX_EXCHANGES = len(RTSP_KEEPALIVE_METHODS) * 2
# An exchange may consume one socket-send timeout plus one response timeout.
RTSP_REQUEST_EXCHANGE_BUDGET_SECONDS = RTSP_IO_TIMEOUT_SECONDS * 2
RTSP_KEEPALIVE_JOIN_TIMEOUT_SECONDS = (
    RTSP_KEEPALIVE_MAX_EXCHANGES * RTSP_REQUEST_EXCHANGE_BUDGET_SECONDS + 1
)
RTSP_KEEPALIVE_CANCEL_JOIN_TIMEOUT_SECONDS = RTSP_IO_TIMEOUT_SECONDS + 1
MAX_PCMA_INPUT_BYTES = 128 * 1024 * 1024
MAX_AUDIO_SOURCE_BYTES = 128 * 1024 * 1024
MAX_AUDIO_SOURCE_FRAMES = 100_000
MAX_REPEATED_MEDIA_BYTES = 128 * 1024 * 1024
MAX_REPEATED_AUDIO_FRAMES = 1_000_000
MAX_PREROLL_MILLISECONDS = MAX_REPEATED_MEDIA_BYTES * 1000 // 8000
MAX_TIMING_ROWS = TIMING_LOG_MAX_ROWS
MAX_TIMING_LINE_BYTES = TIMING_LOG_MAX_LINE_BYTES
MAX_TIMING_BYTES = TIMING_LOG_MAX_BYTES
MAX_SESSION_TIMEOUT_CYCLES = 100


# ---------- ONVIF ----------
def soap(url, body, header=""):
    env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>')
    req = urllib.request.Request(url, data=env.encode(),
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        return urllib.request.urlopen(req, timeout=8, context=CTX).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")


def dev_time(url):
    t = soap(url, f'<GetSystemDateAndTime xmlns="{DEV}"/>')
    seg = t.split("UTCDateTime", 1)[1]
    g = lambda n: int(re.search(rf"<[^>]*{n}>(\d+)<", seg).group(1))
    return datetime.datetime(g("Year"), g("Month"), g("Day"), g("Hour"), g("Minute"), g("Second"))


def wsse(user, pw, when):
    n = base64.b64encode(os.urandom(16))
    c = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    d = base64.b64encode(hashlib.sha1(base64.b64decode(n) + c.encode() + pw.encode()).digest())
    return (f'<wsse:Security xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><wsse:UsernameToken>'
            f'<wsse:Username>{user}</wsse:Username>'
            f'<wsse:Password Type="{PDT}">{d.decode()}</wsse:Password>'
            f'<wsse:Nonce>{n.decode()}</wsse:Nonce><wsu:Created>{c}</wsu:Created>'
            f'</wsse:UsernameToken></wsse:Security>')


def onvif_stream_uri(host, user, pw):
    durl = f"http://{host}/onvif/device_service"
    when = dev_time(durl)
    info = soap(durl, f'<GetDeviceInformation xmlns="{DEV}"/>', wsse(user, pw, when))
    if "GetDeviceInformationResponse" not in info:
        raise RuntimeError("ONVIF 인증 실패: " + (re.search(r"ter:\w+", info) or ["?"])[0])
    model = re.search(r"Model>([^<]+)<", info)
    cap = soap(durl, f'<GetCapabilities xmlns="{DEV}"><Category>Media</Category></GetCapabilities>', wsse(user, pw, when))
    m = re.search(r'XAddr>(https?://[^<]*media[^<]*)<', cap)
    murl = m.group(1) if m else f"http://{host}/onvif/media_service"
    prof = soap(murl, f'<GetProfiles xmlns="{MED}"/>', wsse(user, pw, when))
    tok = re.search(r'token="([^"]+)"', prof).group(1)
    body = (f'<GetStreamUri xmlns="{MED}"><StreamSetup><Stream xmlns="{SCHEMA}">RTP-Unicast</Stream>'
            f'<Transport xmlns="{SCHEMA}"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
            f'<ProfileToken>{tok}</ProfileToken></GetStreamUri>')
    su = soap(murl, body, wsse(user, pw, when))
    uri = re.search(r"<[^>]*Uri>([^<]+)<", su).group(1).replace("&amp;", "&")
    return uri, (model.group(1) if model else "?")


# ---------- RTSP ----------
def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


class RtspStreamParser:
    """Incrementally parse bounded RTSP responses and skip interleaved frames."""

    def __init__(
        self,
        *,
        max_header_bytes=RTSP_MAX_HEADER_BYTES,
        max_body_bytes=RTSP_MAX_BODY_BYTES,
        max_buffer_bytes=RTSP_MAX_BUFFER_BYTES,
    ):
        self.max_header_bytes = max_header_bytes
        self.max_body_bytes = max_body_bytes
        self.max_buffer_bytes = max_buffer_bytes
        self.buffer = bytearray()

    def feed(self, data):
        if len(self.buffer) + len(data) > self.max_buffer_bytes:
            raise RuntimeError(
                f"RTSP buffer exceeds {self.max_buffer_bytes} bytes"
            )
        self.buffer.extend(data)
        responses = []
        while self.buffer:
            if self.buffer[0] == 0x24:  # RTSP interleaved RTP/RTCP frame
                if len(self.buffer) < 4:
                    break
                frame_len = struct.unpack_from(">H", self.buffer, 2)[0]
                frame_end = 4 + frame_len
                if frame_end > self.max_buffer_bytes:
                    raise RuntimeError(
                        f"RTSP interleaved frame exceeds {self.max_buffer_bytes} bytes"
                    )
                if len(self.buffer) < frame_end:
                    break
                del self.buffer[:frame_end]
                continue

            separator = self.buffer.find(b"\r\n\r\n")
            if separator < 0:
                if len(self.buffer) > self.max_header_bytes:
                    raise RuntimeError(
                        f"RTSP header exceeds {self.max_header_bytes} bytes"
                    )
                break
            header_end = separator + 4
            if header_end > self.max_header_bytes:
                raise RuntimeError(
                    f"RTSP header exceeds {self.max_header_bytes} bytes"
                )

            head = bytes(self.buffer[:separator])
            lines = head.decode("latin1").split("\r\n")
            match = re.match(r"RTSP/1\.0 (\d{3})(?:\s|$)", lines[0])
            if not match:
                raise RuntimeError(f"invalid RTSP response: {lines[0]!r}")
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            try:
                content_len = int(headers.get("content-length", "0"))
            except ValueError as error:
                raise RuntimeError("invalid RTSP Content-Length") from error
            if content_len < 0:
                raise RuntimeError("invalid negative RTSP Content-Length")
            if content_len > self.max_body_bytes:
                raise RuntimeError(
                    f"RTSP body exceeds {self.max_body_bytes} bytes"
                )
            response_end = header_end + content_len
            if response_end > self.max_buffer_bytes:
                raise RuntimeError(
                    f"RTSP response exceeds {self.max_buffer_bytes} buffered bytes"
                )
            if len(self.buffer) < response_end:
                break
            body = bytes(self.buffer[header_end:response_end]).decode(
                "utf-8", "replace"
            )
            del self.buffer[:response_end]
            responses.append((int(match.group(1)), headers, body))
        return responses


class Rtsp:
    def __init__(self, host, port, user, pw):
        self.user, self.pw = user, pw
        self.s = socket.create_connection(
            (host, port), timeout=RTSP_IO_TIMEOUT_SECONDS
        )
        self.s.settimeout(RTSP_IO_TIMEOUT_SECONDS)
        self.cseq = 0
        self.challenge = None
        self.session = None
        self.write_lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.responses = queue.Queue(maxsize=RTSP_MAX_QUEUED_RESPONSES)
        self.response_timeout_seconds = RTSP_IO_TIMEOUT_SECONDS
        self.monotonic = time.monotonic
        self.reader_failure = None
        self.closed = False
        self.reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader.start()

    def _auth(self, method, uri):
        c = self.challenge
        if not c:
            return None
        ha1 = md5(f"{self.user}:{c['realm']}:{self.pw}")
        ha2 = md5(f"{method}:{uri}")
        resp = md5(f"{ha1}:{c['nonce']}:{ha2}")
        return (f'Digest username="{self.user}", realm="{c["realm"]}", '
                f'nonce="{c["nonce"]}", uri="{uri}", response="{resp}"')

    def _send(self, method, uri, extra, deadline):
        self._raise_reader_failure()
        self.cseq += 1
        expected_cseq = self.cseq
        hdr = {"User-Agent": "py-poc"}
        hdr.update(
            (key, value)
            for key, value in extra.items()
            if key.lower() != "cseq"
        )
        hdr["CSeq"] = str(expected_cseq)
        a = self._auth(method, uri)
        if a:
            hdr["Authorization"] = a
        msg = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n"
        with self.write_lock:
            self._raise_reader_failure()
            self.s.sendall(msg.encode())
        return self._read(expected_cseq, deadline=deadline)

    def _raise_reader_failure(self):
        if self.reader_failure is not None:
            raise self.reader_failure

    def _close_socket_from_reader(self, error):
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        except BaseException as cleanup_error:
            add_cleanup_failure_notes(
                error,
                cleanup_error,
                prefix="RTSP reader shutdown failure",
            )
        try:
            self.s.close()
        except BaseException as cleanup_error:
            add_cleanup_failure_notes(
                error,
                cleanup_error,
                prefix="RTSP reader socket close failure",
            )

    def _fail_reader(self, error):
        if self.reader_failure is None:
            self.reader_failure = error
        else:
            error = self.reader_failure
        while True:
            try:
                self.responses.get_nowait()
            except queue.Empty:
                break
        try:
            self.responses.put_nowait(error)
        except queue.Full:
            pass
        self._close_socket_from_reader(error)

    def _enqueue_response(self, response):
        try:
            self.responses.put_nowait(response)
            return True
        except queue.Full:
            self._fail_reader(RuntimeError(
                "RTSP response queue exceeded "
                f"{RTSP_MAX_QUEUED_RESPONSES} entries"
            ))
            return False

    def _reader_loop(self):
        parser = RtspStreamParser()
        try:
            while not self.closed:
                try:
                    chunk = self.s.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                for response in parser.feed(chunk):
                    if not self._enqueue_response(response):
                        return
        except OSError as exc:
            if not self.closed:
                self._fail_reader(exc)
        except Exception as exc:
            self._fail_reader(exc)

    def _read(self, expected_cseq, *, deadline=None):
        if deadline is None:
            deadline = self.monotonic() + self.response_timeout_seconds
        expected_text = str(expected_cseq)
        while True:
            self._raise_reader_failure()
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TimeoutError("RTSP response timeout")
            try:
                result = self.responses.get(timeout=remaining)
            except queue.Empty as exc:
                self._raise_reader_failure()
                raise TimeoutError("RTSP response timeout") from exc
            if isinstance(result, Exception):
                raise result
            _, headers, _ = result
            raw_cseq = headers.get("cseq")
            if raw_cseq is None:
                raise RuntimeError("missing RTSP response CSeq")
            if not re.fullmatch(r"[0-9]+", raw_cseq):
                raise RuntimeError(
                    f"invalid RTSP response CSeq: {raw_cseq!r}"
                )
            normalized_cseq = raw_cseq.lstrip("0") or "0"
            if (
                len(normalized_cseq) < len(expected_text)
                or (
                    len(normalized_cseq) == len(expected_text)
                    and normalized_cseq < expected_text
                )
            ):
                continue
            if normalized_cseq != expected_text:
                display_cseq = normalized_cseq[:32]
                if len(normalized_cseq) > len(display_cseq):
                    display_cseq += "..."
                raise RuntimeError(
                    "future RTSP response CSeq "
                    f"{display_cseq} while waiting for {expected_cseq}"
                )
            return result

    def request(self, method, uri, extra=None):
        deadline = self.monotonic() + self.response_timeout_seconds
        with self.request_lock:
            st, h, b = self._send(method, uri, extra or {}, deadline)
            if st == 401 and "www-authenticate" in h:
                wa = h["www-authenticate"]
                realm = re.search(r'realm="([^"]*)"', wa)
                nonce = re.search(r'nonce="([^"]*)"', wa)
                if realm and nonce:
                    self.challenge = {
                        "realm": realm.group(1),
                        "nonce": nonce.group(1),
                    }
                    st, h, b = self._send(
                        method, uri, extra or {}, deadline
                    )
            return st, h, b

    def send_interleaved(self, channel, rtp):
        frame = b"\x24" + bytes([channel]) + struct.pack(">H", len(rtp)) + rtp
        with self.write_lock:
            self._raise_reader_failure()
            self.s.sendall(frame)

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        except BaseException as error:
            errors.append(error)
        try:
            self.s.close()
        except BaseException as error:
            errors.append(error)
        try:
            self.reader.join(timeout=RTSP_IO_TIMEOUT_SECONDS)
        except BaseException as error:
            errors.append(error)
        if hasattr(self.reader, "is_alive") and self.reader.is_alive():
            errors.append(RuntimeError("RTSP reader thread did not stop"))
        if errors:
            primary = errors[0]
            for error in errors[1:]:
                add_cleanup_failure_notes(
                    primary,
                    error,
                    prefix="additional RTSP close failure",
                )
            raise primary


def sdp_tracks(sdp):
    return [block for block in re.split(r"\r?\n(?=m=)", sdp) if block.startswith("m=")]


def track_control(track):
    match = re.search(r"^a=control:(\S+)", track, re.MULTILINE)
    return match.group(1) if match else None


def resolve_track_uri(base_uri, content_base, control):
    if control.startswith("rtsp://"):
        return control
    return (content_base or base_uri).rstrip("/") + "/" + control


def rtp_info_for_track(header, control):
    for entry in header.split(","):
        fields = {}
        for part in entry.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                fields[key.strip().lower()] = value.strip()
        if control in fields.get("url", ""):
            return {
                "seq": int(fields["seq"]) if "seq" in fields else None,
                "rtptime": int(fields["rtptime"]) if "rtptime" in fields else None,
            }
    return {}


def parse_public_methods(header):
    return frozenset(
        method.strip().upper()
        for method in header.split(",")
        if method.strip()
    )


def parse_session_header(
    header, *, default_timeout=DEFAULT_RTSP_SESSION_TIMEOUT_SECONDS
):
    parts = [part.strip() for part in header.split(";")]
    session_id = parts[0] if parts else ""
    if not session_id:
        raise RuntimeError("RTSP SETUP returned an empty Session ID")
    timeout = default_timeout
    for parameter in parts[1:]:
        key, separator, value = parameter.partition("=")
        if not separator or key.strip().lower() != "timeout":
            continue
        value = value.strip().strip('"')
        normalized = value.lstrip("0") or "0"
        maximum = str(MAX_RTSP_SESSION_TIMEOUT_SECONDS)
        if (
            not re.fullmatch(r"[0-9]+", value)
            or normalized == "0"
            or len(normalized) > len(maximum)
            or (
                len(normalized) == len(maximum)
                and normalized > maximum
            )
        ):
            display_value = value[:64]
            if len(value) > len(display_value):
                display_value += "..."
            raise RuntimeError(
                f"invalid RTSP Session timeout: {display_value!r}"
            )
        timeout = int(normalized)
    return session_id, timeout


def select_keepalive_method(public_methods):
    if "SET_PARAMETER" in public_methods:
        return "SET_PARAMETER"
    if "GET_PARAMETER" in public_methods:
        return "GET_PARAMETER"
    return "OPTIONS"


def add_cleanup_failure_notes(primary_error, cleanup_error, *, prefix):
    if not hasattr(primary_error, "add_note") or primary_error is cleanup_error:
        return
    nested_notes = tuple(getattr(cleanup_error, "__notes__", ()) or ())
    primary_error.add_note(f"{prefix}: {cleanup_error}")
    for note in nested_notes:
        primary_error.add_note(note)


class BackchannelTransport:
    """An established ONVIF RTSP backchannel and its RTP destination."""

    def __init__(
        self,
        stream_uri,
        rtsp,
        camera_host,
        transport,
        *,
        keepalive_event_factory=threading.Event,
        keepalive_join_timeout_seconds=RTSP_KEEPALIVE_JOIN_TIMEOUT_SECONDS,
        keepalive_cancel_join_timeout_seconds=(
            RTSP_KEEPALIVE_CANCEL_JOIN_TIMEOUT_SECONDS
        ),
    ):
        self.stream_uri = stream_uri
        self.rtsp = rtsp
        self.camera_host = camera_host
        self.transport = transport
        self.model = None
        self.sdp = None
        self.describe_headers = {}
        self.public_methods = frozenset()
        self.send_track = None
        self.send_control = None
        self.setup_headers = {}
        self.play_headers = {}
        self.session_timeout_seconds = DEFAULT_RTSP_SESSION_TIMEOUT_SECONDS
        self.keepalive_method = "OPTIONS"
        self.keepalive_event_factory = keepalive_event_factory
        self.keepalive_join_timeout_seconds = keepalive_join_timeout_seconds
        self.keepalive_cancel_join_timeout_seconds = (
            keepalive_cancel_join_timeout_seconds
        )
        self.keepalive_stop = None
        self.keepalive_thread = None
        self.keepalive_failure = None
        self.rtp_channel = None
        self.rtcp_channel = None
        self.udp_target = None
        self.udp_rtcp_target = None
        self.udp_socket = None
        self.udp_rtcp_socket = None
        self.closed = False

    @property
    def session(self):
        return self.rtsp.session

    @property
    def keepalive_interval_seconds(self):
        return self.session_timeout_seconds / 2

    def update_session(self, session_header):
        session_id, timeout = parse_session_header(session_header)
        self.rtsp.session = session_id
        self.session_timeout_seconds = timeout

    def _send_keepalive(self):
        start_index = RTSP_KEEPALIVE_METHODS.index(self.keepalive_method)
        for method in RTSP_KEEPALIVE_METHODS[start_index:]:
            headers = {"Session": self.session}
            if method in {"SET_PARAMETER", "GET_PARAMETER"}:
                headers["Content-Length"] = "0"
            status, _, _ = self.rtsp.request(method, self.stream_uri, headers)
            if status == 200:
                self.keepalive_method = method
                return
            if status not in {405, 501}:
                _require_rtsp_success(method, status)
        raise RuntimeError("camera does not support an RTSP keepalive method")

    def _keepalive_loop(self):
        try:
            while not self.keepalive_stop.wait(
                self.keepalive_interval_seconds
            ):
                self._send_keepalive()
        except BaseException as error:
            self.keepalive_failure = error

    def start_keepalive(self):
        if self.keepalive_thread is not None:
            return
        self.keepalive_stop = self.keepalive_event_factory()
        self.keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="rtsp-backchannel-keepalive",
            daemon=True,
        )
        self.keepalive_thread.start()

    def check_keepalive(self):
        if self.keepalive_failure is not None:
            raise self.keepalive_failure

    def send_rtp(self, packet):
        if self.closed:
            raise RuntimeError("backchannel transport is closed")
        self.check_keepalive()
        if self.transport == "tcp":
            self.rtsp.send_interleaved(self.rtp_channel, packet)
        else:
            self.udp_socket.sendto(packet, self.udp_target)

    def send_rtcp(self, packet):
        if self.closed:
            raise RuntimeError("backchannel transport is closed")
        self.check_keepalive()
        if self.transport == "tcp":
            self.rtsp.send_interleaved(self.rtcp_channel, packet)
        else:
            if self.udp_rtcp_target is None:
                raise RuntimeError("RTSP SETUP did not return an RTCP server port")
            self.udp_rtcp_socket.sendto(packet, self.udp_rtcp_target)

    def close(self):
        if self.closed:
            return
        errors = []
        rtsp_closed = False
        forced_rtsp_cancellation = False

        def record_error(error):
            if not any(error is existing for existing in errors):
                errors.append(error)

        try:
            if self.keepalive_thread is not None:
                self.keepalive_stop.set()
                try:
                    self.keepalive_thread.join(
                        timeout=self.keepalive_join_timeout_seconds
                    )
                except BaseException as error:
                    record_error(error)
                if self.keepalive_thread.is_alive():
                    forced_rtsp_cancellation = True
                    record_error(RuntimeError(
                        "RTSP keepalive exceeded its shutdown budget; "
                        "TEARDOWN could not be sent because the RTSP "
                        "connection had to be closed"
                    ))
                    try:
                        self.rtsp.close()
                    except BaseException as error:
                        record_error(error)
                    finally:
                        rtsp_closed = True
                    try:
                        self.keepalive_thread.join(
                            timeout=self.keepalive_cancel_join_timeout_seconds
                        )
                    except BaseException as error:
                        record_error(error)
                    if self.keepalive_thread.is_alive():
                        record_error(RuntimeError(
                            "RTSP keepalive thread exceeded the forced "
                            "cancellation join budget"
                        ))
                        self.keepalive_thread.join()
                if self.keepalive_failure is not None:
                    record_error(self.keepalive_failure)
            if self.rtsp.session and not forced_rtsp_cancellation:
                try:
                    status, _, _ = self.rtsp.request("TEARDOWN", self.stream_uri, {
                        "Session": self.rtsp.session,
                        "Require": BACKCHANNEL,
                    })
                    _require_rtsp_success("TEARDOWN", status)
                except BaseException as error:
                    record_error(error)
            for udp_socket in (self.udp_socket, self.udp_rtcp_socket):
                if udp_socket is not None:
                    try:
                        udp_socket.close()
                    except BaseException as error:
                        record_error(error)
            if not rtsp_closed:
                try:
                    self.rtsp.close()
                except BaseException as error:
                    record_error(error)
        finally:
            self.closed = True
        if errors:
            primary = errors[0]
            for error in errors[1:]:
                add_cleanup_failure_notes(
                    primary,
                    error,
                    prefix="additional backchannel cleanup failure",
                )
            raise primary

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc_value is None:
                raise
            add_cleanup_failure_notes(
                exc_value,
                cleanup_error,
                prefix="backchannel cleanup failure",
            )
        return False


def _require_rtsp_success(method, status):
    if status != 200:
        raise RuntimeError(f"{method} failed with RTSP status {status}")


def redact_rtsp_uri(uri):
    """Remove userinfo and query data before displaying an RTSP endpoint."""
    parsed = urllib.parse.urlsplit(uri)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def open_backchannel_transport(
    host,
    user,
    password,
    *,
    transport="tcp",
    stream_uri=None,
    rtsp_factory=None,
    onvif_uri_resolver=None,
    client_rtp_port=50000,
    keepalive_event_factory=threading.Event,
    keepalive_join_timeout_seconds=RTSP_KEEPALIVE_JOIN_TIMEOUT_SECONDS,
    keepalive_cancel_join_timeout_seconds=(
        RTSP_KEEPALIVE_CANCEL_JOIN_TIMEOUT_SECONDS
    ),
):
    """Open and PLAY an ONVIF backchannel, returning a closable RTP transport."""
    if transport not in {"tcp", "udp"}:
        raise ValueError(f"unsupported backchannel transport: {transport}")
    if stream_uri is None:
        resolver = onvif_uri_resolver or onvif_stream_uri
        stream_uri, model = resolver(host, user, password)
    else:
        model = None

    parsed_uri = urllib.parse.urlsplit(stream_uri)
    if parsed_uri.scheme.lower() != "rtsp" or not parsed_uri.hostname:
        raise ValueError("ONVIF returned an invalid RTSP stream URI")
    camera_host = parsed_uri.hostname
    camera_port = parsed_uri.port or 554
    rtsp = (rtsp_factory or Rtsp)(camera_host, camera_port, user, password)
    result = BackchannelTransport(
        stream_uri,
        rtsp,
        camera_host,
        transport,
        keepalive_event_factory=keepalive_event_factory,
        keepalive_join_timeout_seconds=keepalive_join_timeout_seconds,
        keepalive_cancel_join_timeout_seconds=(
            keepalive_cancel_join_timeout_seconds
        ),
    )
    result.model = model

    try:
        status, options_headers, _ = rtsp.request("OPTIONS", stream_uri)
        _require_rtsp_success("OPTIONS", status)
        result.public_methods = parse_public_methods(
            options_headers.get("public", "")
        )
        result.keepalive_method = select_keepalive_method(
            result.public_methods
        )
        status, describe_headers, sdp = rtsp.request("DESCRIBE", stream_uri, {
            "Accept": "application/sdp",
            "Require": BACKCHANNEL,
        })
        _require_rtsp_success("DESCRIBE", status)
        result.describe_headers = describe_headers
        result.sdp = sdp

        tracks = sdp_tracks(sdp)
        send_track = next((
            track for track in tracks
            if track.startswith("m=audio") and "a=sendonly" in track
        ), None)
        if send_track is None:
            raise RuntimeError("sendonly audio backchannel track not found")
        send_control = track_control(send_track)
        if send_control is None:
            raise RuntimeError("backchannel track has no control URI")
        result.send_track = send_track
        result.send_control = send_control
        content_base = describe_headers.get("content-base")

        if transport == "tcp":
            requested_channel = 0
            for receive_track in tracks:
                if receive_track == send_track or "a=recvonly" not in receive_track:
                    continue
                receive_control = track_control(receive_track)
                if receive_control is None:
                    continue
                receive_uri = resolve_track_uri(
                    stream_uri, content_base, receive_control
                )
                headers = {
                    "Transport": (
                        "RTP/AVP/TCP;unicast;"
                        f"interleaved={requested_channel}-{requested_channel + 1}"
                    )
                }
                if rtsp.session:
                    headers["Session"] = rtsp.session
                status, setup_headers, _ = rtsp.request(
                    "SETUP", receive_uri, headers
                )
                _require_rtsp_success(f"SETUP({receive_control})", status)
                result.update_session(setup_headers.get("session", ""))
                requested_channel += 2

            send_uri = resolve_track_uri(stream_uri, content_base, send_control)
            headers = {
                "Require": BACKCHANNEL,
                "Transport": (
                    "RTP/AVP/TCP;unicast;"
                    f"interleaved={requested_channel}-{requested_channel + 1}"
                ),
            }
            if rtsp.session:
                headers["Session"] = rtsp.session
            status, setup_headers, _ = rtsp.request("SETUP", send_uri, headers)
            _require_rtsp_success("SETUP(backchannel)", status)
            result.update_session(setup_headers.get("session", ""))
            interleaved = re.search(
                r"(?:^|;)interleaved=(\d+)-(\d+)(?:;|$)",
                setup_headers.get("transport", ""),
                re.IGNORECASE,
            )
            if interleaved:
                result.rtp_channel = int(interleaved.group(1))
                result.rtcp_channel = int(interleaved.group(2))
            else:
                result.rtp_channel = requested_channel
                result.rtcp_channel = requested_channel + 1
        else:
            send_uri = resolve_track_uri(stream_uri, content_base, send_control)
            headers = {
                "Require": BACKCHANNEL,
                "Transport": (
                    "RTP/AVP;unicast;"
                    f"client_port={client_rtp_port}-{client_rtp_port + 1};"
                    'mode="PLAY"'
                ),
            }
            status, setup_headers, _ = rtsp.request("SETUP", send_uri, headers)
            _require_rtsp_success("SETUP(backchannel)", status)
            result.update_session(setup_headers.get("session", ""))
            server_ports = re.search(
                r"(?:^|;)server_port=(\d+)-(\d+)(?:;|$)",
                setup_headers.get("transport", ""),
                re.IGNORECASE,
            )
            if server_ports is None:
                raise RuntimeError("RTSP SETUP did not return UDP server ports")
            result.udp_target = (camera_host, int(server_ports.group(1)))
            result.udp_rtcp_target = (camera_host, int(server_ports.group(2)))
            result.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            result.udp_socket.bind(("", client_rtp_port))
            result.udp_rtcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            result.udp_rtcp_socket.bind(("", client_rtp_port + 1))

        result.setup_headers = setup_headers
        status, play_headers, _ = rtsp.request("PLAY", stream_uri, {
            "Session": rtsp.session,
            "Range": "npt=now-",
            "Require": BACKCHANNEL,
        })
        _require_rtsp_success("PLAY", status)
        result.play_headers = play_headers
        result.start_keepalive()
        return result
    except BaseException as error:
        try:
            result.close()
        except BaseException as cleanup_error:
            add_cleanup_failure_notes(
                error,
                cleanup_error,
                prefix="backchannel cleanup failure",
            )
        raise


def rtcp_sender_report(ssrc, rtp_timestamp, packet_count, octet_count, unix_time, cname):
    """Build a compound RTCP SR + SDES(CNAME) packet."""
    ntp = unix_time + 2208988800
    ntp_seconds = int(ntp)
    ntp_fraction = int((ntp - ntp_seconds) * (1 << 32))
    sr = struct.pack(">BBHIIIIII", 0x80, 200, 6, ssrc,
                     ntp_seconds & 0xFFFFFFFF, ntp_fraction & 0xFFFFFFFF,
                     rtp_timestamp & 0xFFFFFFFF, packet_count & 0xFFFFFFFF,
                     octet_count & 0xFFFFFFFF)

    cname_bytes = cname.encode("utf-8")[:255]
    sdes_body = struct.pack(">I", ssrc) + bytes([1, len(cname_bytes)]) + cname_bytes + b"\x00"
    sdes_body += b"\x00" * (-len(sdes_body) % 4)
    sdes = struct.pack(">BBH", 0x81, 202, len(sdes_body) // 4) + sdes_body
    return sr + sdes


# ---------- G.711 ----------
def lin2ulaw(sample):
    BIAS, CLIP = 0x84, 32635
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exp, mask = 7, 0x4000
    while (sample & mask) == 0 and exp > 0:
        exp -= 1
        mask >>= 1
    mant = (sample >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant)) & 0xFF


def lin2alaw(sample):
    CLIP = 32635
    sign = 0x80 if sample < 0 else 0
    if sign:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    if sample >= 256:
        exp, mask = 7, 0x4000
        while (sample & mask) == 0 and exp > 0:
            exp -= 1
            mask >>= 1
        mant = (sample >> (exp + 3)) & 0x0F
        comp = (exp << 4) | mant
    else:
        comp = sample >> 4
    # A-law: invert sign bit + even bits (XOR 0x55), sign bit set = positive
    return ((sign | comp) ^ 0xD5) & 0xFF


# codec -> (static/default RTP payload type, ffmpeg format, linear encoder)
CODECS = {
    "pcmu": (0, "mulaw", lin2ulaw),
    "pcma": (8, "alaw", lin2alaw),
    "l16": (97, "s16be", None),
    "aac": (None, "adts", None),
}


def tone_audio(freq, ms, codec, amp=0.7, rate=8000):
    enc = CODECS[codec][2]
    n = rate * ms // 1000
    if codec == "l16":
        return b"".join(struct.pack(">h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)))
                        for i in range(n))
    return bytes(enc(int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def file_audio(path, codec, volume, sample_rate, encoder="python-alaw"):
    if codec == "pcma":
        decoded = backchannel_audio.decode_source(path, sample_rate)
        if encoder == "ffmpeg":
            return backchannel_audio.encode_pcma_ffmpeg(
                decoded, volume, sample_rate
            )
        if encoder == "python-alaw":
            return backchannel_audio.encode_pcma_gst_compatible(decoded, volume)
        raise ValueError(f"unsupported PCMA encoder: {encoder}")
    fmt = CODECS[codec][1]
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                        "-af", f"volume={volume}", "-ar", str(sample_rate), "-ac", "1",
                        "-fs", str(MAX_AUDIO_SOURCE_BYTES + 1),
                        "-f", fmt, "-"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.decode().strip())
    return p.stdout


def parse_adts_frames(data, max_frames=None):
    if max_frames is None:
        max_frames = MAX_AUDIO_SOURCE_FRAMES
    if (
        isinstance(max_frames, bool)
        or not isinstance(max_frames, int)
        or max_frames < 0
    ):
        raise ValueError("AAC max_frames must be a nonnegative integer")
    frames = []
    offset = 0
    while offset < len(data):
        if offset + 7 > len(data) or data[offset] != 0xFF or data[offset + 1] & 0xF6 != 0xF0:
            raise RuntimeError(f"invalid ADTS frame at byte {offset}")
        protection_absent = data[offset + 1] & 1
        frame_length = ((data[offset + 3] & 0x03) << 11) | (data[offset + 4] << 3) | (data[offset + 5] >> 5)
        header_length = 7 if protection_absent else 9
        if frame_length < header_length or offset + frame_length > len(data):
            raise RuntimeError(f"truncated ADTS frame at byte {offset}")
        if len(frames) >= max_frames:
            raise ValueError(
                f"AAC source frame count exceeds {max_frames} frame limit"
            )
        frames.append(data[offset + header_length:offset + frame_length])
        offset += frame_length
    return frames


def file_aac(
    path,
    volume,
    sample_rate,
    preroll_ms,
    bitrate_kbps=64,
    max_frames=None,
):
    filters = [f"volume={volume}"]
    if preroll_ms:
        filters.append(f"adelay={preroll_ms}:all=1")
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
        "-af", ",".join(filters), "-ar", str(sample_rate), "-ac", "1",
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", f"{bitrate_kbps}k",
        "-fs", str(MAX_AUDIO_SOURCE_BYTES + 1),
        "-f", "adts", "-",
    ], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg AAC: " + p.stderr.decode().strip())
    if len(p.stdout) > MAX_AUDIO_SOURCE_BYTES:
        raise ValueError(
            "AAC encoded output exceeds "
            f"{MAX_AUDIO_SOURCE_BYTES} byte limit"
        )
    return parse_adts_frames(p.stdout, max_frames=max_frames)


def aac_rfc3640_payload(frame):
    if len(frame) > 0x1FFF:
        raise RuntimeError(f"AAC access unit too large: {len(frame)} bytes")
    # AU-headers-length=16, then AU-sizeLength=13 and AU-indexLength=3.
    return b"\x00\x10" + struct.pack(">H", len(frame) << 3) + frame


def read_pcma_input(path):
    path = pathlib.Path(path)
    try:
        metadata = path.stat()
    except FileNotFoundError as error:
        raise ValueError(f"PCMA input does not exist: {path}") from error
    except OSError as error:
        raise ValueError(f"cannot inspect PCMA input {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"PCMA input is not a regular file: {path}")
    if metadata.st_size > MAX_PCMA_INPUT_BYTES:
        raise ValueError(
            f"PCMA input {path} exceeds {MAX_PCMA_INPUT_BYTES} byte limit"
        )
    try:
        with path.open("rb") as source:
            payload = source.read(MAX_PCMA_INPUT_BYTES + 1)
    except OSError as error:
        raise ValueError(f"cannot read PCMA input {path}: {error}") from error
    if len(payload) > MAX_PCMA_INPUT_BYTES:
        raise ValueError(
            f"PCMA input {path} exceeds {MAX_PCMA_INPUT_BYTES} byte limit"
        )
    if not payload:
        raise ValueError(f"PCMA input {path} must not be empty")
    return payload


def prepare_audio(arguments):
    if arguments.codec == "aac":
        if not arguments.file:
            raise ValueError("AAC output requires --file")
        frames = file_aac(
            arguments.file,
            arguments.volume,
            arguments.sample_rate,
            arguments.preroll_ms,
        )
        payload_bytes = sum(len(frame) for frame in frames)
        duration = len(frames) * 1024 / arguments.sample_rate
        message = (
            f"  AAC-LC file {arguments.file} -> {len(frames)} frames, "
            f"{payload_bytes} bytes (~{duration:.1f}s, including preroll)"
        )
        return None, frames, message
    if arguments.pcma_input:
        payload = read_pcma_input(arguments.pcma_input)
        digest = hashlib.sha256(payload).hexdigest()
        message = (
            f"  PCMA input {arguments.pcma_input} -> {len(payload)} bytes "
            f"sha256={digest}"
        )
        return payload, None, message
    if arguments.file:
        payload = file_audio(
            arguments.file,
            arguments.codec,
            arguments.volume,
            arguments.sample_rate,
            encoder=arguments.encoder,
        )
        bytes_per_sample = 2 if arguments.codec == "l16" else 1
        duration = len(payload) / (arguments.sample_rate * bytes_per_sample)
        message = (
            f"  file {arguments.file} -> {len(payload)} bytes "
            f"(~{duration:.1f}s {arguments.codec.upper()})"
        )
        return payload, None, message
    payload = tone_audio(
        arguments.freq,
        arguments.ms,
        arguments.codec,
        amp=arguments.volume,
        rate=arguments.sample_rate,
    )
    message = (
        f"  tone {arguments.freq}Hz {arguments.ms}ms -> "
        f"{len(payload)} bytes ({arguments.codec.upper()})"
    )
    return payload, None, message


def _timing_packet_count(arguments, payload, aac_frames, samples_per_frame):
    if arguments.codec == "aac":
        return len(aac_frames)
    bytes_per_sample = 2 if arguments.codec == "l16" else 1
    preroll_samples = preroll_sample_count(
        arguments.sample_rate, arguments.preroll_ms
    )
    total_bytes = len(payload) + preroll_samples * bytes_per_sample
    bytes_per_frame = samples_per_frame * bytes_per_sample
    return (total_bytes + bytes_per_frame - 1) // bytes_per_frame


def session_cycle_pcm_layout(
    payload,
    preroll,
    *,
    bytes_per_sample,
    sample_rate,
    session_timeout_seconds,
    cycles,
):
    if len(payload) % bytes_per_sample:
        raise ValueError("audio payload is not sample-aligned")
    if len(preroll) % bytes_per_sample:
        raise ValueError("audio preroll is not sample-aligned")
    target_stream_samples = (
        cycles * session_timeout_seconds + 5
    ) * sample_rate
    preroll_samples = len(preroll) // bytes_per_sample
    required_audio_samples = max(0, target_stream_samples - preroll_samples)
    source_samples = len(payload) // bytes_per_sample
    output_audio_samples = max(source_samples, required_audio_samples)
    output_audio_bytes = output_audio_samples * bytes_per_sample
    output_bytes = len(preroll) + output_audio_bytes
    if output_bytes > MAX_REPEATED_MEDIA_BYTES:
        raise ValueError(
            "session timeout diagnostic media requires "
            f"{output_bytes} bytes, exceeding the "
            f"{MAX_REPEATED_MEDIA_BYTES} byte repetition limit"
        )
    if output_audio_bytes and not payload:
        raise ValueError(
            "session timeout diagnostic requires a non-empty audio payload"
        )
    return output_audio_bytes, output_bytes


def repeat_payload_for_session_cycles(
    payload,
    preroll,
    *,
    bytes_per_sample,
    sample_rate,
    session_timeout_seconds,
    cycles,
):
    output_audio_bytes, _ = session_cycle_pcm_layout(
        payload,
        preroll,
        bytes_per_sample=bytes_per_sample,
        sample_rate=sample_rate,
        session_timeout_seconds=session_timeout_seconds,
        cycles=cycles,
    )
    repetitions, tail_bytes = divmod(output_audio_bytes, len(payload) or 1)
    return preroll + payload * repetitions + payload[:tail_bytes]


def session_cycle_aac_layout(
    frames,
    *,
    sample_rate,
    session_timeout_seconds,
    cycles,
):
    frames = tuple(frames)
    if not frames:
        raise ValueError(
            "session timeout diagnostic requires at least one AAC frame"
        )
    target_samples = (
        cycles * session_timeout_seconds + 5
    ) * sample_rate
    target_frame_count = (
        target_samples + 1024 - 1
    ) // 1024
    output_frame_count = max(len(frames), target_frame_count)
    if output_frame_count > MAX_REPEATED_AUDIO_FRAMES:
        raise ValueError(
            "session timeout diagnostic requires "
            f"{output_frame_count} AAC frames, exceeding the "
            f"{MAX_REPEATED_AUDIO_FRAMES} frame limit"
        )
    repetitions, tail_frames = divmod(output_frame_count, len(frames))
    source_bytes = sum(len(frame) for frame in frames)
    represented_bytes = (
        repetitions * source_bytes
        + sum(len(frame) for frame in frames[:tail_frames])
        + output_frame_count * 4
    )
    if represented_bytes > MAX_REPEATED_MEDIA_BYTES:
        raise ValueError(
            "session timeout diagnostic AAC media requires "
            f"{represented_bytes} bytes, exceeding the "
            f"{MAX_REPEATED_MEDIA_BYTES} byte repetition limit"
        )
    return frames, output_frame_count, repetitions, tail_frames


def repeat_aac_frames_for_session_cycles(
    frames,
    *,
    sample_rate,
    session_timeout_seconds,
    cycles,
):
    frames, _, repetitions, tail_frames = session_cycle_aac_layout(
        frames,
        sample_rate=sample_rate,
        session_timeout_seconds=session_timeout_seconds,
        cycles=cycles,
    )
    return frames * repetitions + frames[:tail_frames]


def _bounded_nonnegative_decimal(value, *, maximum, label):
    text = value.strip()
    maximum_text = str(maximum)
    if (
        not re.fullmatch(r"[0-9]+", text)
        or len(text) > len(maximum_text)
        or (len(text) == len(maximum_text) and text > maximum_text)
    ):
        raise argparse.ArgumentTypeError(
            f"{label} must be between 0 and {maximum}"
        )
    result = int(text)
    if str(result) != text:
        raise argparse.ArgumentTypeError(
            f"{label} must be between 0 and {maximum}"
        )
    return result


def session_timeout_cycles(value):
    return _bounded_nonnegative_decimal(
        value,
        maximum=MAX_SESSION_TIMEOUT_CYCLES,
        label="session timeout cycles",
    )


def preroll_milliseconds(value):
    return _bounded_nonnegative_decimal(
        value,
        maximum=MAX_PREROLL_MILLISECONDS,
        label="preroll milliseconds",
    )


def preroll_sample_count(sample_rate, preroll_ms):
    return sample_rate * preroll_ms // 1000


def build_argument_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="172.168.46.56")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--pass", dest="pw", default="CHANGEME")
    input_group = ap.add_mutually_exclusive_group()
    input_group.add_argument("--file")
    input_group.add_argument("--pcma-input", type=pathlib.Path)
    ap.add_argument("--freq", type=int, default=1000)
    ap.add_argument("--ms", type=int, default=3000)
    ap.add_argument("--volume", type=float, default=0.05,
                    help="linear output gain, 0.0-1.0 (default: 0.05 / about -26 dB)")
    ap.add_argument("--sample-rate", type=int, choices=[8000, 16000, 32000, 48000, 64000],
                    default=8000, help="PCMA/PCMU RTP clock rate offered by the camera")
    ap.add_argument("--rtcp-interval", type=float, default=0,
                    help="seconds between RTCP sender reports; 0 disables RTCP (default)")
    ap.add_argument("--preroll-ms", type=preroll_milliseconds, default=0,
                    help="silent RTP warm-up before audio (default: 0)")
    ap.add_argument("--rtp-identity", choices=["legacy", "sender"], default="sender",
                    help="RTP identity source: sender-owned random state or legacy server values")
    ap.add_argument("--marker-mode", choices=["audio-start", "first"], default="first",
                    help="marker policy for G.711/L16 packets")
    packet_group = ap.add_mutually_exclusive_group()
    packet_group.add_argument("--packet-ms", type=float, default=40,
                              help="packet duration in milliseconds for G.711/L16 (default: 40)")
    packet_group.add_argument("--packet-pattern", type=pathlib.Path,
                              help="diagnostic ordered PCMA payload-size manifest")
    ap.add_argument("--pacer", choices=["legacy", "rebase"], default="rebase",
                    help="RTP pacing implementation")
    ap.add_argument("--timing-log", type=pathlib.Path,
                    help="atomically write per-packet pacing JSONL")
    ap.add_argument("--transport", choices=["tcp", "udp"], default="tcp",
                    help="backchannel RTP transport: tcp(interleaved) or udp")
    ap.add_argument("--codec", choices=["pcmu", "pcma", "l16", "aac"], default="pcma",
                    help="RTP audio codec: pcma, pcmu, l16, or AAC-LC MPEG4-GENERIC")
    ap.add_argument("--encoder", choices=["ffmpeg", "python-alaw"], default="python-alaw",
                    help="PCMA terminal encoder; python-alaw uses no GStreamer")
    ap.add_argument(
        "--session-timeout-cycles",
        type=session_timeout_cycles,
        default=0,
        metavar="N",
        help=(
            "extend media through N negotiated RTSP session timeouts plus "
            "5 seconds; 0 disables"
        ),
    )
    return ap


def main(argv=None):
    ap = build_argument_parser()
    a = ap.parse_args(argv)
    for input_option, input_path in (
        ("--file", a.file),
        ("--pcma-input", a.pcma_input),
        ("--packet-pattern", a.packet_pattern),
    ):
        if (
            a.timing_log is not None
            and input_path is not None
            and paths_refer_to_same_file(a.timing_log, input_path)
        ):
            ap.error(
                f"--timing-log must not refer to the same file as {input_option}"
            )
    remove_output(a.timing_log)
    if not math.isfinite(a.volume) or not 0.0 <= a.volume <= 1.0:
        ap.error("--volume must be finite and between 0.0 and 1.0")
    if not math.isfinite(a.rtcp_interval) or a.rtcp_interval < 0:
        ap.error("--rtcp-interval must be finite and 0 or greater")
    if a.preroll_ms < 0:
        ap.error("--preroll-ms must be 0 or greater")
    if a.pcma_input and a.codec != "pcma":
        ap.error("--pcma-input only supports --codec pcma")
    if a.pcma_input and a.sample_rate != 8000:
        ap.error("--pcma-input requires --sample-rate 8000")
    if a.packet_pattern and a.codec != "pcma":
        ap.error("--packet-pattern only supports --codec pcma")
    if a.packet_pattern and a.sample_rate != 8000:
        ap.error("--packet-pattern requires --sample-rate 8000")
    if a.session_timeout_cycles and a.packet_pattern:
        ap.error(
            "--session-timeout-cycles cannot repeat the one-shot "
            "--packet-pattern manifest"
        )
    if a.codec == "aac" and a.session_timeout_cycles and a.preroll_ms:
        ap.error(
            "AAC --session-timeout-cycles cannot use --preroll-ms because "
            "encoder priming and silence must not be repeated"
        )

    packet_limit = 65535 if a.transport == "tcp" else 65507
    pattern_payload_sizes = None
    samples_per_frame = None
    if a.packet_pattern is not None:
        try:
            pattern_payload_sizes = load_packet_pattern(a.packet_pattern)
        except ValueError as error:
            ap.error(str(error))
        for packet_index, payload_size in enumerate(pattern_payload_sizes):
            rtp_packet_bytes = 12 + payload_size
            if rtp_packet_bytes > packet_limit:
                ap.error(
                    f"--packet-pattern RTP packet size {rtp_packet_bytes} at "
                    f"packet {packet_index} exceeds {a.transport.upper()} "
                    f"limit {packet_limit}"
                )
    elif a.codec != "aac":
        if not math.isfinite(a.packet_ms) or a.packet_ms <= 0:
            ap.error("--packet-ms must be positive")
        packet_samples = a.sample_rate * a.packet_ms / 1000
        if not packet_samples.is_integer():
            ap.error("--packet-ms must produce an integral sample count")
        samples_per_frame = int(packet_samples)
        if samples_per_frame > 0xFFFFFFFF:
            ap.error("--packet-ms sample count exceeds uint32")
        packet_payload_bytes = samples_per_frame * (2 if a.codec == "l16" else 1)
        rtp_packet_bytes = 12 + packet_payload_bytes
        if rtp_packet_bytes > packet_limit:
            ap.error(
                f"RTP packet size {rtp_packet_bytes} exceeds "
                f"{a.transport.upper()} limit {packet_limit}"
            )

    if not a.file and not a.pcma_input and a.codec != "aac":
        if a.ms < 0:
            ap.error("--ms must be 0 or greater")
        tone_samples = a.sample_rate * a.ms // 1000
        tone_bytes = tone_samples * (2 if a.codec == "l16" else 1)
        if tone_bytes > MAX_AUDIO_SOURCE_BYTES:
            ap.error(
                f"tone source requires {tone_bytes} bytes, exceeding the "
                f"{MAX_AUDIO_SOURCE_BYTES} byte source limit"
            )

    bytes_per_sample = None
    preroll_samples = None
    if a.codec != "aac":
        bytes_per_sample = 2 if a.codec == "l16" else 1
        preroll_samples = preroll_sample_count(
            a.sample_rate, a.preroll_ms
        )
        preroll_bytes = preroll_samples * bytes_per_sample
        if preroll_bytes > MAX_REPEATED_MEDIA_BYTES:
            ap.error(
                f"audio preroll requires {preroll_bytes} bytes, exceeding "
                f"the {MAX_REPEATED_MEDIA_BYTES} byte media limit"
            )

    payload, aac_frames, audio_message = prepare_audio(a)
    if a.codec == "aac" and len(aac_frames) > MAX_AUDIO_SOURCE_FRAMES:
        ap.error(
            f"AAC source frame count {len(aac_frames)} exceeds the "
            f"{MAX_AUDIO_SOURCE_FRAMES} source frame limit"
        )
    source_audio_bytes = (
        sum(len(frame) for frame in aac_frames)
        if a.codec == "aac"
        else len(payload)
    )
    if source_audio_bytes > MAX_AUDIO_SOURCE_BYTES:
        ap.error(
            f"audio source requires {source_audio_bytes} bytes, exceeding "
            f"the {MAX_AUDIO_SOURCE_BYTES} byte source limit"
        )
    boundary_plan = None
    if a.codec != "aac":
        silence_sample = {
            "pcma": b"\xD5",
            "pcmu": b"\xFF",
            "l16": b"\x00\x00",
        }[a.codec]
        preroll = silence_sample * preroll_samples
        audio_start_offset = len(preroll)
        stream_payload = None if a.session_timeout_cycles else preroll + payload
        if pattern_payload_sizes is not None:
            pattern_samples = sum(pattern_payload_sizes)
            stream_samples = len(stream_payload)
            if pattern_samples < stream_samples:
                ap.error(
                    "--packet-pattern has too few samples: "
                    f"{pattern_samples}; stream requires {stream_samples}"
                )
            if pattern_samples > stream_samples:
                ap.error(
                    "--packet-pattern has too many samples: "
                    f"{pattern_samples}; stream requires {stream_samples}"
                )
            boundary_plan = RtpBoundaryPlan.from_payload_sizes(
                stream_payload,
                pattern_payload_sizes,
                sample_rate=a.sample_rate,
                bytes_per_sample=1,
            )
        elif not a.session_timeout_cycles:
            boundary_plan = RtpBoundaryPlan.fixed(
                stream_payload,
                samples_per_frame,
                sample_rate=a.sample_rate,
                bytes_per_sample=bytes_per_sample,
            )
    if a.timing_log is not None and not a.session_timeout_cycles:
        timing_packet_count = (
            len(aac_frames)
            if a.codec == "aac"
            else boundary_plan.packet_count
        )
        if timing_packet_count > MAX_TIMING_ROWS:
            ap.error(
                f"--timing-log packet count {timing_packet_count} exceeds "
                f"{MAX_TIMING_ROWS:,} row limit"
            )
    print(f"# Python ONVIF 백채널 송출 @ {a.host}")
    uri, model = onvif_stream_uri(a.host, a.user, a.pw)
    print(f"  ✓ ONVIF OK ({model})  stream={redact_rtsp_uri(uri)}")

    backchannel = open_backchannel_transport(
        a.host,
        a.user,
        a.pw,
        transport=a.transport,
        stream_uri=uri,
    )
    r = backchannel.rtsp
    playback_error = None
    try:
        if a.session_timeout_cycles:
            if a.codec == "aac":
                _, diagnostic_packet_count, _, _ = session_cycle_aac_layout(
                    aac_frames,
                    sample_rate=a.sample_rate,
                    session_timeout_seconds=(
                        backchannel.session_timeout_seconds
                    ),
                    cycles=a.session_timeout_cycles,
                )
                if (
                    a.timing_log is not None
                    and diagnostic_packet_count > MAX_TIMING_ROWS
                ):
                    ap.error(
                        f"--timing-log packet count "
                        f"{diagnostic_packet_count} exceeds "
                        f"{MAX_TIMING_ROWS:,} row limit"
                    )
                aac_frames = repeat_aac_frames_for_session_cycles(
                    aac_frames,
                    sample_rate=a.sample_rate,
                    session_timeout_seconds=(
                        backchannel.session_timeout_seconds
                    ),
                    cycles=a.session_timeout_cycles,
                )
                diagnostic_packet_count = len(aac_frames)
            else:
                _, diagnostic_stream_bytes = session_cycle_pcm_layout(
                    payload,
                    preroll,
                    bytes_per_sample=bytes_per_sample,
                    sample_rate=a.sample_rate,
                    session_timeout_seconds=(
                        backchannel.session_timeout_seconds
                    ),
                    cycles=a.session_timeout_cycles,
                )
                packet_payload_bytes = samples_per_frame * bytes_per_sample
                diagnostic_packet_count = (
                    diagnostic_stream_bytes + packet_payload_bytes - 1
                ) // packet_payload_bytes
                if (
                    a.timing_log is not None
                    and diagnostic_packet_count > MAX_TIMING_ROWS
                ):
                    ap.error(
                        f"--timing-log packet count "
                        f"{diagnostic_packet_count} exceeds "
                        f"{MAX_TIMING_ROWS:,} row limit"
                    )
                stream_payload = repeat_payload_for_session_cycles(
                    payload,
                    preroll,
                    bytes_per_sample=bytes_per_sample,
                    sample_rate=a.sample_rate,
                    session_timeout_seconds=(
                        backchannel.session_timeout_seconds
                    ),
                    cycles=a.session_timeout_cycles,
                )
                boundary_plan = RtpBoundaryPlan.fixed(
                    stream_payload,
                    samples_per_frame,
                    sample_rate=a.sample_rate,
                    bytes_per_sample=bytes_per_sample,
                )
                diagnostic_packet_count = boundary_plan.packet_count
            if a.timing_log is not None and diagnostic_packet_count > MAX_TIMING_ROWS:
                ap.error(
                    f"--timing-log packet count "
                    f"{diagnostic_packet_count} exceeds "
                    f"{MAX_TIMING_ROWS:,} row limit"
                )
        print("  OPTIONS -> 200")
        print("  DESCRIBE(backchannel) -> 200")
        track = backchannel.send_track
        ctrl = backchannel.send_control
        encoding = {"pcma": "PCMA", "pcmu": "PCMU", "l16": "L16",
                    "aac": "MPEG4-GENERIC"}[a.codec]
        rtpmap = re.search(rf"^a=rtpmap:(\d+)\s+{encoding}/{a.sample_rate}(?:\D|$)", track,
                           re.IGNORECASE | re.MULTILINE)
        if rtpmap:
            pt = int(rtpmap.group(1))
        elif a.sample_rate == 8000 and a.codec != "aac":
            pt = CODECS[a.codec][0]
        else:
            raise RuntimeError(f"카메라 SDP에 {encoding}/{a.sample_rate} 코덱 없음")
        print(f"  코덱: {a.codec.upper()}/{a.sample_rate}Hz (pt={pt})")
        cam_host = backchannel.camera_host
        ch = backchannel.rtp_channel
        negotiated_ssrc = None
        setup_transport = backchannel.setup_headers.get("transport", "")
        ssrc_match = re.search(
            r"(?:^|;)ssrc=([0-9a-f]+)", setup_transport, re.IGNORECASE
        )
        negotiated_ssrc = int(ssrc_match.group(1), 16) if ssrc_match else None
        if a.transport == "udp":
            print(f"  SETUP(udp) -> 200  session={r.session}  transport={setup_transport}")
        else:
            print(f"  SETUP(tcp) -> 200  session={r.session}  channel={ch}")
            print(f"    Transport: {setup_transport or '-'}")
        print(f"  PLAY -> 200  (transport={a.transport})")
        backchannel_info = rtp_info_for_track(
            backchannel.play_headers.get("rtp-info", ""), ctrl
        )
        advertised_seq = backchannel_info.get("seq")
        advertised_timestamp = backchannel_info.get("rtptime")
        advertised_ssrc = negotiated_ssrc
        advertised_ssrc_text = (
            f"{advertised_ssrc:08X}" if advertised_ssrc is not None else "-"
        )
        print(
            "    Server-advertised RTP: "
            f"seq={advertised_seq if advertised_seq is not None else '-'} "
            f"rtptime={advertised_timestamp if advertised_timestamp is not None else '-'} "
            f"ssrc={advertised_ssrc_text}"
        )

        print(audio_message)

        if a.codec != "aac" and preroll:
            print(f"  무음 preroll: {a.preroll_ms}ms ({len(preroll)} bytes)")

        if a.rtp_identity == "legacy":
            sequence = advertised_seq
            if sequence is None:
                sequence = int.from_bytes(os.urandom(2), "big")
            timestamp = advertised_timestamp
            if timestamp is None:
                timestamp = int.from_bytes(os.urandom(4), "big")
            ssrc = advertised_ssrc
            if ssrc is None:
                ssrc = int.from_bytes(os.urandom(4), "big")
            packetizer = RtpPacketizer(pt, ssrc, sequence, timestamp)
        else:
            packetizer = RtpPacketizer(pt)
        initial_ssrc, initial_sequence, initial_timestamp = packetizer.initial_state
        identity_suffix = " (legacy)" if a.rtp_identity == "legacy" else ""
        print(
            "    Selected sender RTP: "
            f"seq={initial_sequence} rtptime={initial_timestamp} "
            f"ssrc={initial_ssrc:08X}{identity_suffix}"
        )
        sent = 0
        octets_sent = 0
        total_samples_sent = 0
        timing_rows = [] if a.timing_log is not None else None
        pacer = RtpPacer(
            a.sample_rate,
            mode=a.pacer,
            monotonic_ns=time.monotonic_ns,
            sleeper=time.sleep,
        )
        mono_start_ns = time.monotonic_ns()
        mono_start = mono_start_ns / 1_000_000_000
        wall_start = time.time()
        last_rtcp_ns = None
        cname = f"py-poc@{socket.gethostname()}"

        def send_rtcp(now_ns):
            elapsed = max(0, now_ns - mono_start_ns) / 1_000_000_000
            if pacer.rebase_count:
                mapped_samples = pacer.stream_samples_at(now_ns)
            else:
                mapped_samples = min(
                    math.floor(
                        max(0.0, now_ns / 1_000_000_000 - mono_start)
                        * a.sample_rate
                    ),
                    total_samples_sent,
                )
            report_timestamp = (
                initial_timestamp + mapped_samples
            ) & 0xFFFFFFFF
            report = rtcp_sender_report(packetizer.ssrc, report_timestamp,
                                        sent, octets_sent,
                                        wall_start + elapsed, cname)
            backchannel.send_rtcp(report)

        if a.codec == "aac":
            packet_source = ((aac_rfc3640_payload(frame), 1024, True)
                             for frame in aac_frames)
        else:
            packet_source = (
                (
                    boundary.payload,
                    boundary.samples,
                    boundary.payload_offset
                    <= audio_start_offset
                    < boundary.payload_offset + boundary.payload_size,
                )
                for boundary in boundary_plan.packets
            )

        for chunk, samples_in_packet, audio_marker in packet_source:
            backchannel.check_keepalive()
            timing = pacer.wait(samples_in_packet)
            if a.codec == "aac":
                # RFC 3640 packetization is experimental and keeps its prior AU marker policy.
                marker = audio_marker
            elif a.marker_mode == "audio-start":
                marker = sent == 0 or audio_marker
            else:
                marker = sent == 0
            rtp_timestamp = (
                packetizer.timestamp if timing_rows is not None else None
            )
            rtp = packetizer.build(chunk, samples_in_packet, marker)
            backchannel.send_rtp(rtp)
            if timing_rows is not None:
                if len(timing_rows) >= MAX_TIMING_ROWS:
                    raise RuntimeError(
                        f"timing log row count exceeds {MAX_TIMING_ROWS}"
                    )
                timing_rows.append({
                    "packet_index": sent,
                    "rtp_timestamp": rtp_timestamp,
                    "samples": samples_in_packet,
                    "sample_rate": a.sample_rate,
                    "pacer": a.pacer,
                    "packet_duration_ns": (
                        samples_in_packet * 1_000_000_000 // a.sample_rate
                    ),
                    "configured_jitter_bound_ns": 0,
                    "target_monotonic_ns": timing.target_monotonic_ns,
                    "actual_monotonic_ns": timing.actual_monotonic_ns,
                    "lateness_ns": timing.lateness_ns,
                    "interval_ns": timing.interval_ns,
                    "rebased": timing.rebased,
                })
            sent += 1
            octets_sent += len(chunk)
            total_samples_sent += samples_in_packet
            now_ns = time.monotonic_ns()
            if a.rtcp_interval > 0 and (
                last_rtcp_ns is None
                or now_ns - last_rtcp_ns >= a.rtcp_interval * 1_000_000_000
            ):
                send_rtcp(now_ns)
                last_rtcp_ns = now_ns
        backchannel.check_keepalive()
        pacer.finish()
        backchannel.check_keepalive()
        if a.rtcp_interval > 0:
            send_rtcp(time.monotonic_ns())
        print(f"  ✓ {sent} RTP 프레임 송신 완료 ({a.transport}) — 카메라 스피커에서 재생됐다면 성공")
    except BaseException as error:
        playback_error = error
        raise
    finally:
        try:
            backchannel.close()
        except BaseException as cleanup_error:
            if playback_error is None:
                raise
            add_cleanup_failure_notes(
                playback_error,
                cleanup_error,
                prefix="backchannel cleanup failure",
            )
    if a.timing_log is not None:
        atomic_write_jsonl(
            a.timing_log,
            timing_rows,
            max_rows=MAX_TIMING_ROWS,
            max_line_bytes=MAX_TIMING_LINE_BYTES,
            max_bytes=MAX_TIMING_BYTES,
        )


if __name__ == "__main__":
    main()
