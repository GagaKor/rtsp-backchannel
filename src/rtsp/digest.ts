/**
 * RTSP/HTTP Digest authentication, shared by the ONVIF backchannel client and
 * the VIGI talk client.
 *
 * Two parameter styles exist because one camera family requires a
 * non-conformant one. See DigestParameterStyle.
 */
import crypto from 'node:crypto';

export type DigestAlgorithm = 'MD5' | 'SHA-256';

export interface DigestChallenge {
  realm: string;
  nonce: string;
  qop?: string;
  opaque?: string;
  algorithm: DigestAlgorithm;
}

/**
 * `rfc7616` renders `qop` and `nc` as bare tokens, which is what RFC 7616
 * specifies and what every ONVIF camera this library has met accepts.
 *
 * `vigi` quotes both and places `cnonce` before `nc`, matching the example in
 * TP-Link's VIGI IPC Open API document. Measured on a VIGI C540V (firmware
 * 2.3.3): the RFC form gets 401, this form gets 200. Do not make it the
 * default — quoting `qop` is not conformant.
 */
export type DigestParameterStyle = 'rfc7616' | 'vigi';

export interface DigestAuthorizationInput {
  user: string;
  pass: string;
  method: string;
  uri: string;
  challenge: DigestChallenge;
  /** 1-based, owned by the caller so one connection's counter never repeats. */
  nonceCount: number;
  cnonce: string;
  style?: DigestParameterStyle;
}

function hasher(algorithm: DigestAlgorithm): (value: string) => string {
  const nodeName = algorithm === 'SHA-256' ? 'sha256' : 'md5';
  return (value) => crypto.createHash(nodeName).update(value).digest('hex');
}

export function parseDigestParameters(
  headerValue: string,
): Record<string, string> | undefined {
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

function escapeQuoted(name: string, value: string): string {
  if (/[\x00-\x1f\x7f]/.test(value)) {
    throw new Error(`RTSP Digest ${name} contains control characters`);
  }
  return value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/**
 * Returns the parsed challenge, the string `'basic'` when the header offers
 * Basic instead of Digest, or `undefined` when it is neither.
 */
export function parseDigestChallenge(
  headerValue: string,
): DigestChallenge | 'basic' | undefined {
  const parameters = parseDigestParameters(headerValue);
  if (!parameters) {
    return /^\s*Basic\b/i.test(headerValue) ? 'basic' : undefined;
  }
  const { realm, nonce } = parameters;
  if (!realm || !nonce) {
    throw new Error('invalid RTSP Digest challenge: missing realm or nonce');
  }
  const declared = parameters.algorithm || 'MD5';
  const normalised = declared.toUpperCase();
  if (normalised !== 'MD5' && normalised !== 'SHA-256') {
    throw new Error(`unsupported RTSP Digest algorithm: ${declared}`);
  }
  const challenge: DigestChallenge = {
    realm,
    nonce,
    algorithm: normalised as DigestAlgorithm,
  };
  if (parameters.qop !== undefined) {
    const auth = parameters.qop
      .split(',')
      .map((value) => value.trim().toLowerCase())
      .find((value) => value === 'auth');
    if (auth) challenge.qop = auth;
  }
  if (parameters.opaque !== undefined) challenge.opaque = parameters.opaque;
  return challenge;
}

export function digestAuthorization(input: DigestAuthorizationInput): string {
  const { user, pass, method, uri, challenge, nonceCount, cnonce } = input;
  const style = input.style ?? 'rfc7616';
  const hash = hasher(challenge.algorithm);
  const ha1 = hash(`${user}:${challenge.realm}:${pass}`);
  const ha2 = hash(`${method}:${uri}`);

  const username = escapeQuoted('username', user);
  const realm = escapeQuoted('realm', challenge.realm);
  const nonce = escapeQuoted('nonce', challenge.nonce);
  const digestUri = escapeQuoted('uri', uri);
  const opaque = challenge.opaque === undefined
    ? ''
    : `, opaque="${escapeQuoted('opaque', challenge.opaque)}"`;
  const head =
    `Digest username="${username}", realm="${realm}", nonce="${nonce}", ` +
    `uri="${digestUri}"`;

  if (!challenge.qop) {
    return `${head}, response="${hash(`${ha1}:${challenge.nonce}:${ha2}`)}"${opaque}`;
  }

  const nc = nonceCount.toString(16).padStart(8, '0');
  const response = hash(
    `${ha1}:${challenge.nonce}:${nc}:${cnonce}:${challenge.qop}:${ha2}`,
  );
  if (style === 'vigi') {
    return (
      `${head}, qop="${challenge.qop}", cnonce="${cnonce}", nc="${nc}", ` +
      `response="${response}"${opaque}`
    );
  }
  return (
    `${head}, qop=${challenge.qop}, nc=${nc}, cnonce="${cnonce}", ` +
    `response="${response}"${opaque}`
  );
}

/** Eight bytes of hex, the cnonce length this library has always used. */
export function generateCnonce(): string {
  return crypto.randomBytes(8).toString('hex');
}
