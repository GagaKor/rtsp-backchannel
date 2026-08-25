/**
 * VIGI OpenAPI control channel.
 *
 * Authenticates over HTTPS and answers the two questions the talk transport
 * needs: which port carries the stream, and whether the camera reports a
 * speaker. Authentication is attempted exactly once — the device keeps a retry
 * counter and locks the account when it is exceeded.
 */
import https from 'node:https';
import type http from 'node:http';
import crypto from 'node:crypto';

export const DEFAULT_VIGI_CONTROL_PORT = 20443;
const DEFAULT_TIMEOUT_MS = 8000;
const MAX_RESPONSE_BYTES = 1024 * 1024;

/** Device-reported failure. `code` is the device's own errCode. */
export class VigiControlError extends Error {
  constructor(readonly code: number, message: string) {
    super(message);
    this.name = 'VigiControlError';
  }
}

export interface VigiAudioCapability {
  speaker: boolean;
  microphone: boolean;
}

export interface VigiControlSession {
  readonly stok: string;
  getStreamPort(): Promise<number>;
  getAudioCapability(): Promise<VigiAudioCapability>;
}

export interface VigiControlOptions {
  host: string;
  /** The device documents admin as the only OpenAPI account. */
  user?: string;
  pass: string;
  port?: number;
  timeoutMs?: number;
}

/** @internal Exported for tests; the HTTP transport is injected. */
export interface VigiControlDependencies {
  postJson(url: string, body: unknown, timeoutMs: number): Promise<unknown>;
}

const sha256 = (value: string) =>
  crypto.createHash('sha256').update(value).digest('hex');

function describeErrorCode(code: number, operation: string): string {
  if (code === -10020) return 'VIGI OpenAPI authentication failed';
  if (code === -10022) return 'VIGI OpenAPI account is locked by retry limit';
  if (code === -10002) return 'VIGI OpenAPI request was unauthorized';
  if (code === -10030) {
    return `VIGI OpenAPI rejected ${operation} as unsupported`;
  }
  return `VIGI OpenAPI ${operation} failed`;
}

/**
 * @internal Reduces `host` to a bare authority, dropping any port it already
 * carries. Shared with src/vigi/talk.ts, which connects a socket and builds an
 * RTSP URI from the same string.
 *
 * `VigiControlOptions.host` is documented as a hostname and the OpenAPI
 * control port has its own option, but every route into this function accepts
 * a `host:port` string for a *different* service: `openBackchannel('cam:2020')`
 * and `getCameraCapabilities({ host: 'cam:2020' })` both forward it verbatim,
 * and an ONVIF service on a non-default port is exactly how VIGI hardware
 * ships. Concatenating produced `https://cam:2020:20443` and threw
 * `Invalid URL`, which the audioSend probe swallowed as "no VIGI speaker" --
 * observed on a VIGI C540V whose OpenAPI reports speaker=true.
 *
 * `URL.hostname` keeps IPv6 literals bracketed, so `[2001:db8::1]:2020`
 * survives as `[2001:db8::1]`.
 */
export function vigiAuthority(host: string): string {
  let hostname: string;
  try {
    ({ hostname } = new URL(`http://${host}`));
  } catch {
    throw new Error(`invalid VIGI host: ${host}`);
  }
  if (hostname === '') throw new Error(`invalid VIGI host: ${host}`);
  return hostname;
}

function requireSuccess(reply: unknown, operation: string): Record<string, unknown> {
  if (typeof reply !== 'object' || reply === null) {
    throw new Error(`invalid VIGI ${operation} response`);
  }
  const record = reply as Record<string, unknown>;
  const code = typeof record.errCode === 'number' ? record.errCode : 0;
  if (code !== 0) throw new VigiControlError(code, describeErrorCode(code, operation));
  return record;
}

/** Node's global fetch cannot accept the camera's self-signed certificate. */
function postJsonOverHttps(
  url: string,
  body: unknown,
  timeoutMs: number,
): Promise<unknown> {
  const target = new URL(url);
  const payload = Buffer.from(JSON.stringify(body), 'utf8');
  const options: https.RequestOptions = {
    method: 'POST',
    hostname: target.hostname,
    port: target.port || 443,
    path: target.pathname + target.search,
    headers: {
      'Content-Type': 'application/json',
      'Content-Length': payload.byteLength,
    },
    rejectUnauthorized: false,
    // This camera closes the TCP connection after answering each request.
    // https.globalAgent pools keep-alive sockets by default in modern
    // Node, so the *next* postJsonOverHttps call (e.g. the doAuth response
    // that follows the doAuth challenge) can get handed a socket the peer
    // has already closed, failing with ECONNRESET ("socket hang up").
    // agent: false opts every call out of pooling so each gets its own
    // fresh socket. Verified against hardware: with the default agent,
    // request 2 of doAuth fails with ECONNRESET; with agent: false it
    // succeeds. Do not remove this to "optimise" the agent back in.
    agent: false,
  };
  return new Promise((resolve, reject) => {
    let settled = false;
    let req: http.ClientRequest | undefined;
    let res: http.IncomingMessage | undefined;
    const settle = (error?: Error, value?: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) {
        // Close the socket before rejecting — a timed-out or oversized
        // exchange must not linger and hold the connection (and the event
        // loop) open after the caller's promise has already settled.
        req?.destroy(error);
        res?.destroy(error);
        reject(error);
      } else {
        resolve(value);
      }
    };
    const timer = setTimeout(
      () => settle(new Error('VIGI OpenAPI request timeout')),
      timeoutMs,
    );
    req = https.request(options, (response) => {
      res = response;
      const chunks: Buffer[] = [];
      let bytes = 0;
      response.on('data', (chunk: Buffer) => {
        bytes += chunk.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) {
          settle(new Error('VIGI OpenAPI response too large'));
          return;
        }
        chunks.push(chunk);
      });
      response.on('end', () => {
        try {
          settle(undefined, JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch {
          settle(new Error('VIGI OpenAPI response was not JSON'));
        }
      });
      response.on('error', () => settle(new Error('VIGI OpenAPI response error')));
    });
    req.on('error', () => settle(new Error('VIGI OpenAPI request failed')));
    req.end(payload);
  });
}

const defaultDependencies: VigiControlDependencies = { postJson: postJsonOverHttps };

export function openVigiControl(
  options: VigiControlOptions,
): Promise<VigiControlSession> {
  return openVigiControlWithDependencies(options, defaultDependencies);
}

/** @internal */
export async function openVigiControlWithDependencies(
  options: VigiControlOptions,
  dependencies: VigiControlDependencies,
): Promise<VigiControlSession> {
  const user = options.user ?? 'admin';
  const port = options.port ?? DEFAULT_VIGI_CONTROL_PORT;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('timeoutMs must be finite and greater than 0');
  }
  const base = `https://${vigiAuthority(options.host)}:${port}`;

  const challengeReply = await dependencies.postJson(
    base,
    { method: 'doAuth', params: null },
    timeoutMs,
  );
  const authenticate = (challengeReply as { authenticate?: unknown } | null)
    ?.authenticate;
  if (typeof authenticate !== 'object' || authenticate === null) {
    throw new Error('invalid VIGI doAuth challenge');
  }
  const challenge = authenticate as Record<string, unknown>;
  const realm = challenge.realm;
  const nonce = challenge.nonce;
  const uri = challenge.uri;
  const method = challenge.method;
  if (
    typeof realm !== 'string' || typeof nonce !== 'string'
    || typeof uri !== 'string' || typeof method !== 'string'
  ) {
    throw new Error('invalid VIGI doAuth challenge');
  }
  const algorithm = typeof challenge.algorithm === 'string'
    ? challenge.algorithm
    : 'unknown';
  if (algorithm.toUpperCase() !== 'SHA-256') {
    throw new Error(`unsupported VIGI doAuth algorithm: ${algorithm}`);
  }

  const a1 = sha256(`${user}:${realm}:${options.pass}`);
  const a2 = sha256(`${method}:${uri}`);
  const response = sha256(`${a1}:${nonce}:${a2}`);

  // One attempt only. A retry here is what locks the account (-10022).
  const authReply = requireSuccess(
    await dependencies.postJson(
      base,
      { method: 'doAuth', params: { nonce, response } },
      timeoutMs,
    ),
    'doAuth',
  );
  const stok = authReply.stok;
  if (typeof stok !== 'string' || stok.length === 0) {
    throw new Error('VIGI doAuth returned no stok');
  }

  const call = async (
    operation: string,
  ): Promise<Record<string, unknown> | undefined> => {
    const reply = requireSuccess(
      await dependencies.postJson(
        `${base}/stok=${stok}`,
        { method: operation, params: null },
        timeoutMs,
      ),
      operation,
    );
    const result = reply.result;
    return typeof result === 'object' && result !== null
      ? (result as Record<string, unknown>)
      : undefined;
  };

  return {
    stok,
    async getStreamPort(): Promise<number> {
      const result = await call('getStreamPort');
      const raw = result?.streamPort;
      const parsed = typeof raw === 'string' ? Number(raw) : raw;
      if (typeof parsed !== 'number' || !Number.isInteger(parsed) || parsed <= 0) {
        throw new Error('invalid VIGI getStreamPort response');
      }
      return parsed;
    },
    async getAudioCapability(): Promise<VigiAudioCapability> {
      const result = await call('getAudioCapability');
      const present = (key: string): boolean => {
        const value = result?.[key];
        return typeof value === 'object' && value !== null;
      };
      return { speaker: present('speaker'), microphone: present('microphone') };
    },
  };
}
