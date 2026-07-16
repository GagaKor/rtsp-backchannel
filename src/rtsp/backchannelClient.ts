/**
 * Minimal RTSP client over a raw TCP socket (Node built-ins only).
 *
 * M1 scope: connect, OPTIONS, DESCRIBE (incl. ONVIF backchannel Require header),
 * with HTTP Digest / Basic auth. SETUP/PLAY + interleaved RTP are added in M2/M3
 * on top of the same persistent socket.
 */
import net from 'node:net';
import crypto from 'node:crypto';

export const BACKCHANNEL_REQUIRE = 'www.onvif.org/ver20/backchannel';

export interface RtspResponse {
  status: number;
  statusLine: string;
  headers: Record<string, string>;
  body: string;
}

interface DigestChallenge {
  realm: string;
  nonce: string;
  qop?: string;
  opaque?: string;
  algorithm?: string;
}

const md5 = (s: string) => crypto.createHash('md5').update(s).digest('hex');

export class RtspClient {
  private socket?: net.Socket;
  private cseq = 0;
  private challenge?: DigestChallenge;
  private basic = false;
  /** Buffer of bytes received but not yet consumed by a response read. */
  private rxBuf = Buffer.alloc(0);
  private rxWaiter?: () => void;

  constructor(
    private readonly host: string,
    private readonly port: number,
    private readonly user: string,
    private readonly pass: string,
    private readonly timeoutMs = 6000,
  ) {}

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const sock = net.createConnection({ host: this.host, port: this.port });
      sock.setTimeout(this.timeoutMs);
      sock.once('connect', () => {
        sock.setTimeout(0);
        this.socket = sock;
        sock.on('data', (chunk) => {
          this.rxBuf = Buffer.concat([this.rxBuf, chunk]);
          if (this.rxWaiter) this.rxWaiter();
          else this.discardInterleavedFrames();
        });
        resolve();
      });
      sock.once('error', reject);
      sock.once('timeout', () => {
        sock.destroy();
        reject(new Error('RTSP connect timeout'));
      });
    });
  }

  close(): void {
    this.socket?.destroy();
    this.socket = undefined;
  }

  /** The live socket — used by M2/M3 to send interleaved RTP frames. */
  get rawSocket(): net.Socket {
    if (!this.socket) throw new Error('not connected');
    return this.socket;
  }

  private authHeader(method: string, uri: string): string | undefined {
    if (this.basic) {
      return 'Basic ' + Buffer.from(`${this.user}:${this.pass}`).toString('base64');
    }
    const c = this.challenge;
    if (!c) return undefined;
    const ha1 = md5(`${this.user}:${c.realm}:${this.pass}`);
    const ha2 = md5(`${method}:${uri}`);
    if (c.qop) {
      const cnonce = crypto.randomBytes(8).toString('hex');
      const nc = '00000001';
      const resp = md5(`${ha1}:${c.nonce}:${nc}:${cnonce}:${c.qop}:${ha2}`);
      return (
        `Digest username="${this.user}", realm="${c.realm}", nonce="${c.nonce}", uri="${uri}", ` +
        `qop=${c.qop}, nc=${nc}, cnonce="${cnonce}", response="${resp}"` +
        (c.opaque ? `, opaque="${c.opaque}"` : '')
      );
    }
    const resp = md5(`${ha1}:${c.nonce}:${ha2}`);
    return (
      `Digest username="${this.user}", realm="${c.realm}", nonce="${c.nonce}", ` +
      `uri="${uri}", response="${resp}"` + (c.opaque ? `, opaque="${c.opaque}"` : '')
    );
  }

  private parseChallenge(headerValue: string): void {
    if (/^\s*Basic/i.test(headerValue) && !/Digest/i.test(headerValue)) {
      this.basic = true;
      return;
    }
    const get = (k: string) => new RegExp(`${k}="?([^",]+)"?`, 'i').exec(headerValue)?.[1];
    const realm = get('realm');
    const nonce = get('nonce');
    if (realm && nonce) {
      this.challenge = {
        realm,
        nonce,
        qop: get('qop'),
        opaque: get('opaque'),
        algorithm: get('algorithm'),
      };
    }
  }

  /** Send a request and read exactly one RTSP response. */
  private async send(
    method: string,
    uri: string,
    extra: Record<string, string> = {},
    body = '',
  ): Promise<RtspResponse> {
    if (!this.socket) throw new Error('not connected');
    this.cseq += 1;
    const headers: Record<string, string> = { CSeq: String(this.cseq), 'User-Agent': 'macs-poc', ...extra };
    const auth = this.authHeader(method, uri);
    if (auth) headers['Authorization'] = auth;
    if (body) headers['Content-Length'] = String(Buffer.byteLength(body));
    const head =
      `${method} ${uri} RTSP/1.0\r\n` +
      Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join('\r\n') +
      '\r\n\r\n';
    this.socket.write(head + body);
    return this.readResponse();
  }

  /** Public request with one automatic re-try after a 401 challenge. */
  async request(
    method: string,
    uri: string,
    extra: Record<string, string> = {},
    body = '',
  ): Promise<RtspResponse> {
    let res = await this.send(method, uri, extra, body);
    if (res.status === 401) {
      const wa = res.headers['www-authenticate'];
      if (wa) {
        this.parseChallenge(wa);
        res = await this.send(method, uri, extra, body);
      }
    }
    return res;
  }

  options(uri: string): Promise<RtspResponse> {
    return this.request('OPTIONS', uri);
  }

  describe(uri: string, opts: { backchannel?: boolean } = {}): Promise<RtspResponse> {
    const extra: Record<string, string> = { Accept: 'application/sdp' };
    if (opts.backchannel) extra['Require'] = BACKCHANNEL_REQUIRE;
    return this.request('DESCRIBE', uri, extra);
  }

  private session?: string;
  sessionTimeoutSeconds = 60;

  /** SETUP one interleaved TCP track, optionally as the backchannel track. */
  async setup(
    trackUri: string,
    opts: { rtpChannel?: number; backchannel?: boolean } = {},
  ): Promise<{ session: string; rtpChannel: number }> {
    const rtp = opts.rtpChannel ?? 0;
    const headers: Record<string, string> = {
      Transport: `RTP/AVP/TCP;unicast;interleaved=${rtp}-${rtp + 1}`,
    };
    if (this.session) headers['Session'] = this.session;
    if (opts.backchannel) headers['Require'] = BACKCHANNEL_REQUIRE;
    const res = await this.request('SETUP', trackUri, headers);
    if (res.status !== 200) {
      throw new Error(`SETUP failed: ${res.statusLine}`);
    }
    const sessionHeader = res.headers['session'] ?? '';
    this.session = sessionHeader.split(';')[0].trim();
    const timeout = /(?:^|;)\s*timeout=(\d+)/i.exec(sessionHeader);
    if (timeout && Number(timeout[1]) > 0) {
      this.sessionTimeoutSeconds = Number(timeout[1]);
    }
    const il = /interleaved=(\d+)-(\d+)/.exec(res.headers['transport'] ?? '');
    return { session: this.session, rtpChannel: il ? Number(il[1]) : rtp };
  }

  async play(uri: string): Promise<RtspResponse> {
    if (!this.session) throw new Error('SETUP must precede PLAY');
    return this.request('PLAY', uri, {
      Session: this.session,
      Range: 'npt=now-',
      Require: BACKCHANNEL_REQUIRE,
    });
  }

  async keepAlive(uri: string): Promise<RtspResponse> {
    if (!this.session) throw new Error('SETUP must precede keepalive');
    return this.request('OPTIONS', uri, { Session: this.session });
  }

  async teardown(uri: string): Promise<void> {
    if (!this.session) return;
    try {
      await this.request('TEARDOWN', uri, { Session: this.session });
    } catch {
      /* best effort */
    }
  }

  /** Send an already-framed buffer (interleaved RTP) on the live socket. */
  sendInterleaved(frame: Buffer): void {
    if (!this.socket) throw new Error('not connected');
    this.socket.write(frame);
  }

  /** Drop complete RTP/RTCP frames arriving on an interleaved RTSP socket. */
  private discardInterleavedFrames(): void {
    while (this.rxBuf.length > 0 && this.rxBuf[0] === 0x24) {
      if (this.rxBuf.length < 4) return;
      const frameLength = this.rxBuf.readUInt16BE(2);
      if (this.rxBuf.length < frameLength + 4) return;
      this.rxBuf = this.rxBuf.subarray(frameLength + 4);
    }
  }

  private readResponse(): Promise<RtspResponse> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.rxWaiter = undefined;
        reject(new Error('RTSP response timeout'));
      }, this.timeoutMs);

      const tryParse = () => {
        this.discardInterleavedFrames();
        const sep = this.rxBuf.indexOf('\r\n\r\n');
        if (sep < 0) return false;
        const headerText = this.rxBuf.subarray(0, sep).toString('utf8');
        const lines = headerText.split('\r\n');
        const statusLine = lines[0];
        const status = Number(/RTSP\/1\.0 (\d+)/.exec(statusLine)?.[1] ?? 0);
        const headers: Record<string, string> = {};
        for (const l of lines.slice(1)) {
          const idx = l.indexOf(':');
          if (idx > 0) headers[l.slice(0, idx).trim().toLowerCase()] = l.slice(idx + 1).trim();
        }
        const contentLength = Number(headers['content-length'] ?? 0);
        const bodyStart = sep + 4;
        if (this.rxBuf.length < bodyStart + contentLength) return false; // wait for full body
        const body = this.rxBuf.subarray(bodyStart, bodyStart + contentLength).toString('utf8');
        this.rxBuf = this.rxBuf.subarray(bodyStart + contentLength);
        clearTimeout(timer);
        this.rxWaiter = undefined;
        resolve({ status, statusLine, headers, body });
        return true;
      };

      this.rxWaiter = () => tryParse();
      if (!tryParse()) {
        /* wait for more data via rxWaiter */
      }
    });
  }
}
