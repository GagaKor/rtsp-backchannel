#!/usr/bin/env python3
"""
Send audio to a camera speaker via ONVIF + RTSP backchannel.
Python equivalent of the TS PoC (m3/cli) for cross-verification.

  python3 python/onvif_play.py --host 172.168.46.56 --user admin --pass CHANGEME
  python3 python/onvif_play.py --file test.mp3 --host 172.168.46.56
"""
import argparse, base64, datetime, hashlib, math, os, queue, re, socket, ssl, struct, subprocess, threading, time, urllib.parse, urllib.request

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
        self.responses = queue.Queue(maxsize=RTSP_MAX_QUEUED_RESPONSES)
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

    def _send(self, method, uri, extra):
        self._raise_reader_failure()
        self.cseq += 1
        hdr = {"CSeq": str(self.cseq), "User-Agent": "py-poc"}
        hdr.update(extra)
        a = self._auth(method, uri)
        if a:
            hdr["Authorization"] = a
        msg = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n"
        self.s.sendall(msg.encode())
        return self._read()

    def _raise_reader_failure(self):
        if self.reader_failure is not None:
            raise self.reader_failure

    def _close_socket_from_reader(self, error):
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        except BaseException as cleanup_error:
            if hasattr(error, "add_note"):
                error.add_note(f"RTSP reader shutdown failure: {cleanup_error}")
        try:
            self.s.close()
        except BaseException as cleanup_error:
            if hasattr(error, "add_note"):
                error.add_note(f"RTSP reader socket close failure: {cleanup_error}")

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

    def _read(self):
        self._raise_reader_failure()
        try:
            result = self.responses.get(timeout=RTSP_IO_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise TimeoutError("RTSP response timeout") from exc
        if isinstance(result, Exception):
            raise result
        return result

    def request(self, method, uri, extra=None):
        st, h, b = self._send(method, uri, extra or {})
        if st == 401 and "www-authenticate" in h:
            wa = h["www-authenticate"]
            realm = re.search(r'realm="([^"]*)"', wa)
            nonce = re.search(r'nonce="([^"]*)"', wa)
            if realm and nonce:
                self.challenge = {"realm": realm.group(1), "nonce": nonce.group(1)}
                st, h, b = self._send(method, uri, extra or {})
        return st, h, b

    def send_interleaved(self, channel, rtp):
        self._raise_reader_failure()
        self.s.sendall(b"\x24" + bytes([channel]) + struct.pack(">H", len(rtp)) + rtp)

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
            if hasattr(primary, "add_note"):
                for error in errors[1:]:
                    primary.add_note(f"additional RTSP close failure: {error}")
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


class BackchannelTransport:
    """An established ONVIF RTSP backchannel and its RTP destination."""

    def __init__(self, stream_uri, rtsp, camera_host, transport):
        self.stream_uri = stream_uri
        self.rtsp = rtsp
        self.camera_host = camera_host
        self.transport = transport
        self.model = None
        self.sdp = None
        self.describe_headers = {}
        self.send_track = None
        self.send_control = None
        self.setup_headers = {}
        self.play_headers = {}
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

    def send_rtp(self, packet):
        if self.closed:
            raise RuntimeError("backchannel transport is closed")
        if self.transport == "tcp":
            self.rtsp.send_interleaved(self.rtp_channel, packet)
        else:
            self.udp_socket.sendto(packet, self.udp_target)

    def send_rtcp(self, packet):
        if self.closed:
            raise RuntimeError("backchannel transport is closed")
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
        try:
            if self.rtsp.session:
                try:
                    status, _, _ = self.rtsp.request("TEARDOWN", self.stream_uri, {
                        "Session": self.rtsp.session,
                        "Require": BACKCHANNEL,
                    })
                    _require_rtsp_success("TEARDOWN", status)
                except BaseException as error:
                    errors.append(error)
            for udp_socket in (self.udp_socket, self.udp_rtcp_socket):
                if udp_socket is not None:
                    try:
                        udp_socket.close()
                    except BaseException as error:
                        errors.append(error)
            try:
                self.rtsp.close()
            except BaseException as error:
                errors.append(error)
        finally:
            self.closed = True
        if errors:
            primary = errors[0]
            if hasattr(primary, "add_note"):
                for error in errors[1:]:
                    primary.add_note(f"additional backchannel cleanup failure: {error}")
            raise primary

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"backchannel cleanup failure: {cleanup_error}")
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
    result = BackchannelTransport(stream_uri, rtsp, camera_host, transport)
    result.model = model

    try:
        status, _, _ = rtsp.request("OPTIONS", stream_uri)
        _require_rtsp_success("OPTIONS", status)
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
                rtsp.session = setup_headers.get("session", "").split(";")[0].strip()
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
            rtsp.session = setup_headers.get("session", "").split(";")[0].strip()
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
            rtsp.session = setup_headers.get("session", "").split(";")[0].strip()
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
        return result
    except BaseException as error:
        try:
            result.close()
        except BaseException as cleanup_error:
            if hasattr(error, "add_note"):
                error.add_note(f"backchannel cleanup failure: {cleanup_error}")
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
    n = int(rate * ms / 1000)
    if codec == "l16":
        return b"".join(struct.pack(">h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)))
                        for i in range(n))
    return bytes(enc(int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def file_audio(path, codec, volume, sample_rate):
    fmt = CODECS[codec][1]
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                        "-af", f"volume={volume}", "-ar", str(sample_rate), "-ac", "1",
                        "-f", fmt, "-"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.decode().strip())
    return p.stdout


def parse_adts_frames(data):
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
        frames.append(data[offset + header_length:offset + frame_length])
        offset += frame_length
    return frames


def file_aac(path, volume, sample_rate, preroll_ms, bitrate_kbps=64):
    filters = [f"volume={volume}"]
    if preroll_ms:
        filters.append(f"adelay={preroll_ms}:all=1")
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
        "-af", ",".join(filters), "-ar", str(sample_rate), "-ac", "1",
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", f"{bitrate_kbps}k",
        "-f", "adts", "-",
    ], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg AAC: " + p.stderr.decode().strip())
    return parse_adts_frames(p.stdout)


def aac_rfc3640_payload(frame):
    if len(frame) > 0x1FFF:
        raise RuntimeError(f"AAC access unit too large: {len(frame)} bytes")
    # AU-headers-length=16, then AU-sizeLength=13 and AU-indexLength=3.
    return b"\x00\x10" + struct.pack(">H", len(frame) << 3) + frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="172.168.46.56")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--pass", dest="pw", default="CHANGEME")
    ap.add_argument("--file")
    ap.add_argument("--freq", type=int, default=1000)
    ap.add_argument("--ms", type=int, default=3000)
    ap.add_argument("--volume", type=float, default=0.25,
                    help="linear output gain, 0.0-1.0 (default: 0.25 / about -12 dB)")
    ap.add_argument("--sample-rate", type=int, choices=[8000, 16000, 32000, 48000, 64000],
                    default=8000, help="PCMA/PCMU RTP clock rate offered by the camera")
    ap.add_argument("--rtcp-interval", type=float, default=0.5,
                    help="seconds between RTCP sender reports; 0 disables RTCP")
    ap.add_argument("--preroll-ms", type=int, default=3000,
                    help="silent RTP warm-up before audio, for camera clock convergence")
    ap.add_argument("--transport", choices=["tcp", "udp"], default="tcp",
                    help="backchannel RTP transport: tcp(interleaved) or udp")
    ap.add_argument("--codec", choices=["pcmu", "pcma", "l16", "aac"], default="pcma",
                    help="RTP audio codec: pcma, pcmu, l16, or AAC-LC MPEG4-GENERIC")
    a = ap.parse_args()
    if not 0.0 < a.volume <= 1.0:
        ap.error("--volume must be greater than 0.0 and at most 1.0")
    if a.rtcp_interval < 0:
        ap.error("--rtcp-interval must be 0 or greater")
    if a.preroll_ms < 0:
        ap.error("--preroll-ms must be 0 or greater")

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
    try:
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
        print(f"    RTP-Info: {backchannel.play_headers.get('rtp-info', '-')}")
        backchannel_info = rtp_info_for_track(
            backchannel.play_headers.get("rtp-info", ""), ctrl
        )

        aac_frames = None
        if a.codec == "aac":
            if not a.file:
                raise RuntimeError("AAC 송출에는 --file이 필요합니다")
            aac_frames = file_aac(a.file, a.volume, a.sample_rate, a.preroll_ms)
            payload_bytes = sum(len(frame) for frame in aac_frames)
            duration = len(aac_frames) * 1024 / a.sample_rate
            print(f"  AAC-LC 파일 {a.file} → {len(aac_frames)} frames, {payload_bytes} bytes "
                  f"(~{duration:.1f}s, preroll 포함)")
        elif a.file:
            payload = file_audio(a.file, a.codec, a.volume, a.sample_rate)
            bytes_per_sample = 2 if a.codec == "l16" else 1
            print(f"  파일 {a.file} → {len(payload)} bytes "
                  f"(~{len(payload)/(a.sample_rate * bytes_per_sample):.1f}s {a.codec.upper()})")
        else:
            payload = tone_audio(a.freq, a.ms, a.codec, amp=a.volume, rate=a.sample_rate)
            print(f"  톤 {a.freq}Hz {a.ms}ms → {len(payload)} bytes ({a.codec.upper()})")

        if a.codec != "aac":
            bytes_per_sample = 2 if a.codec == "l16" else 1
            silence_sample = {"pcma": b"\xD5", "pcmu": b"\xFF", "l16": b"\x00\x00"}[a.codec]
            preroll_samples = int(a.sample_rate * a.preroll_ms / 1000)
            preroll = silence_sample * preroll_samples
            audio_start_offset = len(preroll)
            stream_payload = preroll + payload
            if preroll:
                print(f"  무음 preroll: {a.preroll_ms}ms ({len(preroll)} bytes)")

        seq = backchannel_info.get("seq")
        if seq is None:
            seq = int.from_bytes(os.urandom(2), "big")
        ts = backchannel_info.get("rtptime")
        if ts is None:
            ts = int.from_bytes(os.urandom(4), "big")
        ts_start = ts
        ssrc = negotiated_ssrc if negotiated_ssrc is not None else int.from_bytes(os.urandom(4), "big")
        print(f"  RTP 시작값: seq={seq} rtptime={ts} ssrc={ssrc:08X}")
        sent = 0
        octets_sent = 0
        nxt = time.monotonic()
        mono_start = nxt
        wall_start = time.time()
        last_rtcp = float("-inf")
        cname = f"py-poc@{socket.gethostname()}"

        def send_rtcp(now):
            elapsed = now - mono_start
            current_rtp_ts = (ts_start + int(elapsed * a.sample_rate)) & 0xFFFFFFFF
            report = rtcp_sender_report(ssrc, current_rtp_ts, sent, octets_sent,
                                        wall_start + elapsed, cname)
            backchannel.send_rtcp(report)

        if a.codec == "aac":
            packet_source = ((aac_rfc3640_payload(frame), 1024, True)
                             for frame in aac_frames)
            frame_seconds = 1024 / a.sample_rate
        else:
            samples_per_frame = a.sample_rate // 50  # 20 ms
            bytes_per_frame = samples_per_frame * bytes_per_sample
            packet_source = ((stream_payload[off:off + bytes_per_frame],
                              len(stream_payload[off:off + bytes_per_frame]) // bytes_per_sample,
                              off == audio_start_offset)
                             for off in range(0, len(stream_payload), bytes_per_frame))
            frame_seconds = 0.02

        for chunk, samples_in_packet, audio_marker in packet_source:
            hdr = bytearray(12)
            hdr[0] = 0x80
            marker = sent == 0 or audio_marker
            hdr[1] = (0x80 if marker else 0) | pt
            hdr[2:4] = struct.pack(">H", seq & 0xFFFF)
            hdr[4:8] = struct.pack(">I", ts & 0xFFFFFFFF)
            hdr[8:12] = struct.pack(">I", ssrc & 0xFFFFFFFF)
            rtp = bytes(hdr) + chunk
            backchannel.send_rtp(rtp)
            seq += 1
            ts += samples_in_packet
            sent += 1
            octets_sent += len(chunk)
            now = time.monotonic()
            if a.rtcp_interval > 0 and (sent == 1 or now - last_rtcp >= a.rtcp_interval):
                send_rtcp(now)
                last_rtcp = now
            nxt += frame_seconds
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
        if a.rtcp_interval > 0:
            send_rtcp(time.monotonic())
        print(f"  ✓ {sent} RTP 프레임 송신 완료 ({a.transport}) — 카메라 스피커에서 재생됐다면 성공")
    finally:
        backchannel.close()


if __name__ == "__main__":
    main()
