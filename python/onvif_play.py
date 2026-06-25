#!/usr/bin/env python3
"""
Send audio to a camera speaker via ONVIF + RTSP backchannel (G.711 PCMU).
Python equivalent of the TS PoC (m3/cli) for cross-verification.

  python3 python/onvif_play.py --host 172.168.46.56 --user admin --pass CHANGEME
  python3 python/onvif_play.py --file test.mp3 --host 172.168.46.56
"""
import argparse, base64, datetime, hashlib, math, os, re, socket, ssl, struct, subprocess, time, urllib.request

DEV = "http://www.onvif.org/ver10/device/wsdl"
MED = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PDT = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BACKCHANNEL = "www.onvif.org/ver20/backchannel"
CTX = ssl._create_unverified_context()


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


class Rtsp:
    def __init__(self, host, port, user, pw):
        self.user, self.pw = user, pw
        self.s = socket.create_connection((host, port), timeout=6)
        self.cseq = 0
        self.challenge = None
        self.session = None
        self.buf = b""

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
        self.cseq += 1
        hdr = {"CSeq": str(self.cseq), "User-Agent": "py-poc"}
        hdr.update(extra)
        a = self._auth(method, uri)
        if a:
            hdr["Authorization"] = a
        msg = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n"
        self.s.sendall(msg.encode())
        return self._read()

    def _read(self):
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.s.recv(4096)
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        status = int(re.search(r"RTSP/1.0 (\d+)", lines[0]).group(1))
        headers = {}
        for l in lines[1:]:
            if ":" in l:
                k, v = l.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        cl = int(headers.get("content-length", 0))
        data = rest
        while len(data) < cl:
            data += self.s.recv(4096)
        body = data[:cl].decode("utf-8", "replace")
        self.buf = data[cl:]
        return status, headers, body

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
        self.s.sendall(b"\x24" + bytes([channel]) + struct.pack(">H", len(rtp)) + rtp)

    def close(self):
        self.s.close()


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


# codec -> (rtp payload type, ffmpeg format, linear encoder)
CODECS = {"pcmu": (0, "mulaw", lin2ulaw), "pcma": (8, "alaw", lin2alaw)}


def tone_g711(freq, ms, codec, amp=0.7, rate=8000):
    enc = CODECS[codec][2]
    n = int(rate * ms / 1000)
    return bytes(enc(int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def file_g711(path, codec):
    fmt = CODECS[codec][1]
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                        "-ar", "8000", "-ac", "1", "-f", fmt, "-"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.decode().strip())
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="172.168.46.56")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--pass", dest="pw", default="CHANGEME")
    ap.add_argument("--file")
    ap.add_argument("--freq", type=int, default=1000)
    ap.add_argument("--ms", type=int, default=3000)
    ap.add_argument("--transport", choices=["tcp", "udp"], default="tcp",
                    help="backchannel RTP transport: tcp(interleaved) or udp")
    ap.add_argument("--codec", choices=["pcmu", "pcma"], default="pcma",
                    help="G.711 variant: pcma=G.711A(A-law), pcmu=G.711Mu(µ-law). "
                         "Must match the camera's audio codec setting.")
    a = ap.parse_args()

    print(f"# Python ONVIF 백채널 송출 @ {a.host}")
    uri, model = onvif_stream_uri(a.host, a.user, a.pw)
    print(f"  ✓ ONVIF OK ({model})  stream={uri}")

    m = re.match(r"rtsp://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", uri)
    cam_host = m.group(1)
    r = Rtsp(cam_host, int(m.group(2) or 554), a.user, a.pw)
    try:
        st, h, sdp = r.request("DESCRIBE", uri, {"Accept": "application/sdp", "Require": BACKCHANNEL})
        print(f"  DESCRIBE(backchannel) -> {st}")
        if st != 200:
            raise RuntimeError("backchannel DESCRIBE 실패")
        track = next((b for b in re.split(r"\r?\n(?=m=)", sdp)
                      if b.startswith("m=audio") and "a=sendonly" in b), None)
        if not track:
            raise RuntimeError("sendonly 오디오 백채널 트랙 없음")
        ctrl = re.search(r"a=control:(\S+)", track).group(1)
        pt = CODECS[a.codec][0]  # pcma=8, pcmu=0
        print(f"  코덱: {a.codec.upper()} (pt={pt})")
        turi = ctrl if ctrl.startswith("rtsp://") else uri.rstrip("/") + "/" + ctrl

        udp_sock = None
        ch = 0
        server_rtp_port = None
        if a.transport == "udp":
            client_rtp = 50000
            transport = f"RTP/AVP;unicast;client_port={client_rtp}-{client_rtp + 1};mode=\"PLAY\""
            st, h, _ = r.request("SETUP", turi, {"Require": BACKCHANNEL, "Transport": transport})
            if st != 200:
                raise RuntimeError(f"SETUP(udp) {st} — Transport={h.get('transport')}")
            r.session = h.get("session", "").split(";")[0].strip()
            tr = h.get("transport", "")
            sp = re.search(r"server_port=(\d+)-(\d+)", tr)
            server_rtp_port = int(sp.group(1)) if sp else None
            print(f"  SETUP(udp) -> 200  session={r.session}  transport={tr}")
            if not server_rtp_port:
                raise RuntimeError("server_port 없음 — 이 카메라는 UDP 백채널 미지원일 수 있음")
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.bind(("", client_rtp))
        else:
            st, h, _ = r.request("SETUP", turi,
                                 {"Require": BACKCHANNEL, "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
            if st != 200:
                raise RuntimeError(f"SETUP {st}")
            r.session = h.get("session", "").split(";")[0].strip()
            il = re.search(r"interleaved=(\d+)-(\d+)", h.get("transport", ""))
            ch = int(il.group(1)) if il else 0
            print(f"  SETUP(tcp) -> 200  session={r.session}  channel={ch}")

        st, h, _ = r.request("RECORD", uri, {"Session": r.session, "Range": "npt=0.000-"})
        if st != 200:
            raise RuntimeError(f"RECORD {st}")
        print(f"  RECORD -> 200  (transport={a.transport})")

        if a.file:
            payload = file_g711(a.file, a.codec)
            print(f"  파일 {a.file} → {len(payload)} bytes (~{len(payload)/8000:.1f}s {a.codec.upper()})")
        else:
            payload = tone_g711(a.freq, a.ms, a.codec)
            print(f"  톤 {a.freq}Hz {a.ms}ms → {len(payload)} bytes ({a.codec.upper()})")

        seq = int.from_bytes(os.urandom(2), "big")
        ts = 0
        ssrc = int.from_bytes(os.urandom(4), "big")
        sent = 0
        nxt = time.monotonic()
        for off in range(0, len(payload), 160):
            chunk = payload[off:off + 160]
            hdr = bytearray(12)
            hdr[0] = 0x80
            hdr[1] = (0x80 if sent == 0 else 0) | pt
            hdr[2:4] = struct.pack(">H", seq & 0xFFFF)
            hdr[4:8] = struct.pack(">I", ts & 0xFFFFFFFF)
            hdr[8:12] = struct.pack(">I", ssrc & 0xFFFFFFFF)
            rtp = bytes(hdr) + chunk
            if udp_sock is not None:
                udp_sock.sendto(rtp, (cam_host, server_rtp_port))
            else:
                r.send_interleaved(ch, rtp)
            seq += 1
            ts += len(chunk)
            sent += 1
            nxt += 0.02
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
        print(f"  ✓ {sent} RTP 프레임 송신 완료 ({a.transport}) — 카메라 스피커에서 재생됐다면 성공")
        r.request("TEARDOWN", uri, {"Session": r.session})
        if udp_sock:
            udp_sock.close()
    finally:
        r.close()


if __name__ == "__main__":
    main()
