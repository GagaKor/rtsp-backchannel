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
const MAX_RTSP_HEADER_BYTES = 64 * 1024;
const MAX_RTSP_BODY_BYTES = 8 * 1024 * 1024;
const MAX_RTSP_BUFFER_BYTES = MAX_RTSP_HEADER_BYTES + 4 + MAX_RTSP_BODY_BYTES;

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

interface RtspResponseWaiter {
  expectedCSeq: number;
  wake(): void;
  fail(error: Error): void;
}

const md5 = (s: string) => crypto.createHash('md5').update(s).digest('hex');

function parseDigestParameters(headerValue: string): Record<string, string> | undefined {
  const digest = /\bDigest\s+/i.exec(headerValue);
  if (!digest) return undefined;
  const parameters: Record<string, string> = {};
  let index = digest.index + digest[0].length;
  while (index < headerValue.length) {
    while (index < headerValue.length && /[\s,]/.test(headerValue[index])) index++;
    const keyMatch = /^[a-z][a-z\d_-]*/i.exec(headerValue.slice(index));
    if (!keyMatch) break;
    const key = keyMatch[0].toLowerCase();
    index += keyMatch[0].length;
    while (headerValue[index] === ' ' || headerValue[index] === '\t') index++;
    if (headerValue[index] !== '=') break;
    index++;
    while (headerValue[index] === ' ' || headerValue[index] === '\t') index++;

    let value = '';
    if (headerValue[index] === '"') {
      index++;
      while (index < headerValue.length) {
        const character = headerValue[index++];
        if (character === '"') break;
        if (character === '\\' && index < headerValue.length) {
          value += headerValue[index++];
        } else {
          value += character;
        }
      }
    } else {
      const end = headerValue.indexOf(',', index);
      value = headerValue.slice(index, end < 0 ? headerValue.length : end).trim();
      index = end < 0 ? headerValue.length : end;
    }
    parameters[key] = value;
  }
  return parameters;
}

function quoteDigestValue(name: string, value: string): string {
  if (/[\x00-\x1f\x7f]/.test(value)) {
    throw new Error(`RTSP Digest ${name} contains control characters`);
  }
  return value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

export class RtspClient {
  private socket?: net.Socket;
  private cseq = 0;
  private challenge?: DigestChallenge;
  private digestNonceCount = 0;
  private basic = false;
  /** Buffer of bytes received but not yet consumed by a response read. */
  private rxBuf = Buffer.alloc(0);
  private rxWaiter?: RtspResponseWaiter;
  private requestQueue: Promise<void> = Promise.resolve();

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
          if (chunk.length > MAX_RTSP_BUFFER_BYTES - this.rxBuf.length) {
            const error = new Error(
              `RTSP receive buffer exceeds ${MAX_RTSP_BUFFER_BYTES} bytes`,
            );
            if (this.rxWaiter) this.rxWaiter.fail(error);
            else {
              this.rxBuf = Buffer.alloc(0);
              this.close();
            }
            return;
          }
          this.rxBuf = Buffer.concat([this.rxBuf, chunk]);
          if (this.rxWaiter) this.rxWaiter.wake();
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
    const username = quoteDigestValue('username', this.user);
    const realm = quoteDigestValue('realm', c.realm);
    const nonce = quoteDigestValue('nonce', c.nonce);
    const digestUri = quoteDigestValue('uri', uri);
    const opaque = c.opaque === undefined
      ? ''
      : `, opaque="${quoteDigestValue('opaque', c.opaque)}"`;
    if (c.qop) {
      const cnonce = crypto.randomBytes(8).toString('hex');
      this.digestNonceCount += 1;
      const nc = this.digestNonceCount.toString(16).padStart(8, '0');
      const resp = md5(`${ha1}:${c.nonce}:${nc}:${cnonce}:${c.qop}:${ha2}`);
      return (
        `Digest username="${username}", realm="${realm}", nonce="${nonce}", ` +
        `uri="${digestUri}", qop=${c.qop}, nc=${nc}, cnonce="${cnonce}", ` +
        `response="${resp}"${opaque}`
      );
    }
    const resp = md5(`${ha1}:${c.nonce}:${ha2}`);
    return (
      `Digest username="${username}", realm="${realm}", nonce="${nonce}", ` +
      `uri="${digestUri}", response="${resp}"${opaque}`
    );
  }

  private parseChallenge(headerValue: string): void {
    const parameters = parseDigestParameters(headerValue);
    if (!parameters && /^\s*Basic\b/i.test(headerValue)) {
      this.basic = true;
      this.challenge = undefined;
      this.digestNonceCount = 0;
      return;
    }
    if (!parameters) return;
    const realm = parameters.realm;
    const nonce = parameters.nonce;
    if (!realm || !nonce) {
      throw new Error('invalid RTSP Digest challenge: missing realm or nonce');
    }
    const algorithm = parameters.algorithm || 'MD5';
    if (algorithm.toLowerCase() !== 'md5') {
      throw new Error(`unsupported RTSP Digest algorithm: ${algorithm}`);
    }
    let qop: string | undefined;
    if (parameters.qop !== undefined) {
      const options = parameters.qop.split(',').map((value) => value.trim().toLowerCase());
      if (!options.includes('auth')) {
        throw new Error(`unsupported RTSP Digest qop: ${parameters.qop}`);
      }
      qop = 'auth';
    }
    if (this.challenge?.nonce !== nonce) this.digestNonceCount = 0;
    this.basic = false;
    this.challenge = {
      realm,
      nonce,
      ...(qop ? { qop } : {}),
      ...(parameters.opaque !== undefined ? { opaque: parameters.opaque } : {}),
      algorithm: 'MD5',
    };
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
    return this.readResponse(this.cseq);
  }

  /** Public request with one automatic re-try after a 401 challenge. */
  request(
    method: string,
    uri: string,
    extra: Record<string, string> = {},
    body = '',
  ): Promise<RtspResponse> {
    const operation = this.requestQueue.then(() =>
      this.requestWithAuthentication(method, uri, extra, body),
    );
    this.requestQueue = operation.then(
      () => undefined,
      () => undefined,
    );
    return operation;
  }

  private async requestWithAuthentication(
    method: string,
    uri: string,
    extra: Record<string, string>,
    body: string,
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
  sendInterleaved(frame: Buffer): Promise<void> {
    const socket = this.rawSocket;
    return new Promise((resolve, reject) => {
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const cleanup = () => {
        if (timer !== undefined) clearTimeout(timer);
        socket.off('drain', onDrain);
        socket.off('error', onError);
        socket.off('close', onClose);
      };
      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        cleanup();
        if (error) reject(error);
        else resolve();
      };
      const onDrain = () => finish();
      const onError = (error: Error) => {
        finish(new Error(`RTSP interleaved write failed: ${error.message}`, { cause: error }));
      };
      const onClose = () => finish(new Error('RTSP socket closed during interleaved write'));

      socket.once('drain', onDrain);
      socket.once('error', onError);
      socket.once('close', onClose);
      try {
        if (socket.write(frame)) {
          finish();
          return;
        }
      } catch (error) {
        onError(error instanceof Error ? error : new Error(String(error)));
        return;
      }
      if (settled) return;
      timer = setTimeout(() => {
        finish(new Error(`RTSP interleaved write timeout after ${this.timeoutMs} ms`));
        this.close();
      }, this.timeoutMs);
    });
  }

  /** Drop complete RTP/RTCP frames arriving on an interleaved RTSP socket. */
  private discardInterleavedFrames(): boolean {
    while (this.rxBuf.length > 0 && this.rxBuf[0] === 0x24) {
      if (this.rxBuf.length < 4) return false;
      const frameLength = this.rxBuf.readUInt16BE(2);
      if (this.rxBuf.length < frameLength + 4) return false;
      this.rxBuf = this.rxBuf.subarray(frameLength + 4);
    }
    return true;
  }

  private readResponse(expectedCSeq: number): Promise<RtspResponse> {
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      let settled = false;
      let waiter: RtspResponseWaiter;
      const clearWaiter = () => {
        if (timer !== undefined) clearTimeout(timer);
        if (this.rxWaiter === waiter) this.rxWaiter = undefined;
      };
      const fail = (error: Error) => {
        if (settled) return;
        settled = true;
        clearWaiter();
        this.rxBuf = Buffer.alloc(0);
        this.close();
        reject(error);
      };

      const tryParse = () => {
        if (settled) return true;
        if (!this.discardInterleavedFrames()) return false;
        const sep = this.rxBuf.indexOf('\r\n\r\n');
        if (sep < 0) {
          if (this.rxBuf.length > MAX_RTSP_HEADER_BYTES) {
            fail(new Error(`RTSP response header exceeds ${MAX_RTSP_HEADER_BYTES} bytes`));
            return true;
          }
          return false;
        }
        if (sep > MAX_RTSP_HEADER_BYTES) {
          fail(new Error(`RTSP response header exceeds ${MAX_RTSP_HEADER_BYTES} bytes`));
          return true;
        }
        const headerText = this.rxBuf.subarray(0, sep).toString('utf8');
        const lines = headerText.split('\r\n');
        const statusLine = lines[0];
        const statusMatch = /^RTSP\/1\.0 (\d{3})(?:\s.*)?$/.exec(statusLine);
        if (!statusMatch) {
          fail(new Error('invalid RTSP response status line'));
          return true;
        }
        const status = Number(statusMatch[1]);
        const headers: Record<string, string> = {};
        for (const l of lines.slice(1)) {
          const idx = l.indexOf(':');
          const name = idx < 0 ? '' : l.slice(0, idx).trim();
          if (idx < 1 || !/^[!#$%&'*+.^_`|~\dA-Za-z-]+$/.test(name)) {
            fail(new Error('malformed RTSP response header'));
            return true;
          }
          headers[name.toLowerCase()] = l.slice(idx + 1).trim();
        }
        const responseCSeqText = headers['cseq'];
        if (!responseCSeqText || !/^\d+$/.test(responseCSeqText)) {
          fail(new Error('RTSP response has invalid or missing CSeq'));
          return true;
        }
        const responseCSeq = Number(responseCSeqText);
        if (responseCSeq !== expectedCSeq) {
          fail(
            new Error(
              `RTSP response CSeq ${responseCSeq} does not match request CSeq ${expectedCSeq}`,
            ),
          );
          return true;
        }
        const contentLengthText = headers['content-length'] ?? '0';
        if (!/^\d+$/.test(contentLengthText)) {
          fail(new Error('invalid RTSP Content-Length'));
          return true;
        }
        const contentLength = Number(contentLengthText);
        if (!Number.isSafeInteger(contentLength)) {
          fail(new Error('invalid RTSP Content-Length'));
          return true;
        }
        if (contentLength > MAX_RTSP_BODY_BYTES) {
          fail(new Error(`RTSP response body exceeds ${MAX_RTSP_BODY_BYTES} bytes`));
          return true;
        }
        const bodyStart = sep + 4;
        if (this.rxBuf.length < bodyStart + contentLength) return false; // wait for full body
        const body = this.rxBuf.subarray(bodyStart, bodyStart + contentLength).toString('utf8');
        this.rxBuf = this.rxBuf.subarray(bodyStart + contentLength);
        settled = true;
        clearWaiter();
        resolve({ status, statusLine, headers, body });
        return true;
      };

      waiter = { expectedCSeq, wake: () => tryParse(), fail };
      timer = setTimeout(() => fail(new Error('RTSP response timeout')), this.timeoutMs);
      this.rxWaiter = waiter;
      if (!tryParse()) {
        /* wait for more data via rxWaiter */
      }
    });
  }
}
