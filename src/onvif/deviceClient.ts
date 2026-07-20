/**
 * Minimal ONVIF device/media client (Node built-ins only).
 *
 * Scope for the PoC: time sync, WS-UsernameToken (PasswordDigest) auth,
 * GetDeviceInformation, GetProfiles (audio config detection), GetStreamUri.
 * Handles http/https device service with self-signed TLS.
 */
import http from 'node:http';
import https from 'node:https';
import crypto from 'node:crypto';

const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const MED_NS = 'http://www.onvif.org/ver10/media/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';
const WSSE_NS =
  'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd';
const WSU_NS =
  'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd';
const PWD_DIGEST =
  'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest';

function decodeXml(value: string): string {
  return value
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'");
}

function encodeXml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

export interface DeviceInfo {
  manufacturer?: string;
  model?: string;
  firmware?: string;
  serial?: string;
}

export interface OnvifProfile {
  token: string;
  name?: string;
  hasAudioEncoder: boolean;
  hasAudioOutput: boolean;
  hasAudioSource: boolean;
}

export function parseProfiles(xml: string): OnvifProfile[] {
  const profiles: OnvifProfile[] = [];
  const pattern = /<[^>]*:?Profiles\b[^>]*token="([^"]+)"[^>]*>([\s\S]*?)<\/[^>]*:?Profiles>/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(xml))) {
    const [, encodedToken, block] = match;
    const token = decodeXml(encodedToken);
    const encodedName = firstTag(block, 'Name');
    const name = encodedName ? decodeXml(encodedName) : undefined;
    profiles.push({
      token,
      ...(name ? { name } : {}),
      hasAudioEncoder: block.includes('AudioEncoderConfiguration'),
      hasAudioOutput: block.includes('AudioOutputConfiguration'),
      hasAudioSource: block.includes('AudioSourceConfiguration'),
    });
  }
  return profiles;
}

export interface OnvifOptions {
  /** Candidate device service URLs to try, in order. Auto-built if omitted. */
  deviceUrls?: string[];
  timeoutMs?: number;
}

function firstTag(xml: string, name: string): string | undefined {
  const m =
    new RegExp(`<[^>]*:${name}>([^<]*)</[^>]*:${name}>`).exec(xml) ??
    new RegExp(`<${name}>([^<]*)</${name}>`).exec(xml);
  return m ? m[1] : undefined;
}

export class OnvifDevice {
  private deviceUrl?: string;
  private mediaUrl?: string;
  /** deviceTimeMs - localTimeMs at connect, applied to every security header. */
  private clockOffsetMs = 0;

  constructor(
    private readonly host: string,
    private readonly user: string,
    private readonly pass: string,
    private readonly opts: OnvifOptions = {},
  ) {}

  private candidates(): string[] {
    if (this.opts.deviceUrls?.length) return this.opts.deviceUrls;
    // Try plain HTTP on 80 first — it is the common ONVIF port and avoids
    // wasting time on closed 443/8000 (which also surfaces confusing
    // EHOSTUNREACH errors on multi-homed hosts).
    return [
      `http://${this.host}/onvif/device_service`,
      `https://${this.host}/onvif/device_service`,
      `http://${this.host}:8000/onvif/device_service`,
    ];
  }

  /** Locate a working device service URL and sync the clock for auth. */
  async connect(): Promise<DeviceInfo> {
    let lastErr: unknown;
    for (const url of this.candidates()) {
      try {
        const t = await this.getSystemDateAndTime(url);
        this.clockOffsetMs = t.getTime() - Date.now();
        const info = await this.getDeviceInformation(url);
        this.deviceUrl = url;
        this.mediaUrl = await this.discoverMediaUrl(url);
        return info;
      } catch (err) {
        lastErr = err;
      }
    }
    throw new Error(
      `ONVIF connect failed for ${this.host}: ${(lastErr as Error)?.message ?? lastErr}`,
    );
  }

  private now(): Date {
    return new Date(Date.now() + this.clockOffsetMs);
  }

  private securityHeader(): string {
    const nonce = crypto.randomBytes(16);
    const created = this.now().toISOString().replace(/\.\d+Z$/, 'Z');
    const digest = crypto
      .createHash('sha1')
      .update(Buffer.concat([nonce, Buffer.from(created, 'utf8'), Buffer.from(this.pass, 'utf8')]))
      .digest('base64');
    return (
      `<wsse:Security xmlns:wsse="${WSSE_NS}" xmlns:wsu="${WSU_NS}">` +
      `<wsse:UsernameToken><wsse:Username>${encodeXml(this.user)}</wsse:Username>` +
      `<wsse:Password Type="${PWD_DIGEST}">${digest}</wsse:Password>` +
      `<wsse:Nonce>${nonce.toString('base64')}</wsse:Nonce>` +
      `<wsu:Created>${created}</wsu:Created></wsse:UsernameToken></wsse:Security>`
    );
  }

  private soap(url: string, body: string, withAuth: boolean): Promise<string> {
    const header = withAuth ? this.securityHeader() : '';
    const envelope =
      `<?xml version="1.0" encoding="UTF-8"?>` +
      `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">` +
      `<s:Header>${header}</s:Header><s:Body>${body}</s:Body></s:Envelope>`;
    const u = new URL(url);
    const lib = u.protocol === 'https:' ? https : http;
    const options: https.RequestOptions = {
      method: 'POST',
      hostname: u.hostname,
      port: u.port || (u.protocol === 'https:' ? 443 : 80),
      path: u.pathname + u.search,
      headers: {
        'Content-Type': 'application/soap+xml; charset=utf-8',
        'Content-Length': Buffer.byteLength(envelope),
      },
      timeout: this.opts.timeoutMs ?? 8000,
      ...(u.protocol === 'https:' ? { rejectUnauthorized: false } : {}),
    };
    return new Promise((resolve, reject) => {
      const req = lib.request(options, (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          // ONVIF returns 200 on success, 4xx (with SOAP Fault) on auth errors.
          if ((res.statusCode ?? 0) >= 500 && !text.includes('Envelope')) {
            reject(new Error(`HTTP ${res.statusCode} from ${url}`));
          } else {
            resolve(text);
          }
        });
      });
      req.on('error', reject);
      req.on('timeout', () => req.destroy(new Error('request timeout')));
      req.end(envelope);
    });
  }

  async getSystemDateAndTime(url: string): Promise<Date> {
    const xml = await this.soap(url, `<GetSystemDateAndTime xmlns="${DEV_NS}"/>`, false);
    const utc = /<[^>]*UTCDateTime>([\s\S]*?)<\/[^>]*UTCDateTime>/.exec(xml);
    if (!utc) throw new Error('no UTCDateTime in response');
    const seg = utc[1];
    const num = (n: string) => {
      const m = new RegExp(`<[^>]*${n}>(\\d+)<`).exec(seg);
      return m ? Number(m[1]) : undefined;
    };
    const y = num('Year'), mo = num('Month'), d = num('Day');
    const h = num('Hour') ?? 0, mi = num('Minute') ?? 0, s = num('Second') ?? 0;
    if (y === undefined || mo === undefined || d === undefined) {
      throw new Error('incomplete UTCDateTime');
    }
    return new Date(Date.UTC(y, mo - 1, d, h, mi, s));
  }

  async getDeviceInformation(url = this.requireDeviceUrl()): Promise<DeviceInfo> {
    const xml = await this.soap(url, `<GetDeviceInformation xmlns="${DEV_NS}"/>`, true);
    if (!xml.includes('GetDeviceInformationResponse')) {
      const fault = /<[^>]*:?(?:Subcode|Value)>\s*([^<]*ter:[^<]+)</.exec(xml)?.[1] ??
        /ter:(\w+)/.exec(xml)?.[0] ?? 'auth failed';
      throw new Error(`GetDeviceInformation rejected: ${fault.trim()}`);
    }
    return {
      manufacturer: firstTag(xml, 'Manufacturer'),
      model: firstTag(xml, 'Model'),
      firmware: firstTag(xml, 'FirmwareVersion'),
      serial: firstTag(xml, 'SerialNumber'),
    };
  }

  private async discoverMediaUrl(deviceUrl: string): Promise<string> {
    try {
      const cap = await this.soap(
        deviceUrl,
        `<GetCapabilities xmlns="${DEV_NS}"><Category>Media</Category></GetCapabilities>`,
        true,
      );
      const xaddr = /<[^>]*:?XAddr>(https?:\/\/[^<]*[Mm]edia[^<]*)<\/[^>]*:?XAddr>/.exec(cap)?.[1];
      if (xaddr) return xaddr;
    } catch {
      /* fall through to derived URL */
    }
    return deviceUrl.replace('device_service', 'media_service');
  }

  private requireDeviceUrl(): string {
    if (!this.deviceUrl) throw new Error('call connect() first');
    return this.deviceUrl;
  }

  private requireMediaUrl(): string {
    if (!this.mediaUrl) throw new Error('call connect() first');
    return this.mediaUrl;
  }

  /** Returns profiles plus their audio configuration presence. */
  async getProfiles(): Promise<OnvifProfile[]> {
    const xml = await this.soap(this.requireMediaUrl(), `<GetProfiles xmlns="${MED_NS}"/>`, true);
    return parseProfiles(xml);
  }

  private mediaCall(action: string): Promise<string> {
    return this.soap(this.requireMediaUrl(), `<${action} xmlns="${MED_NS}"/>`, true);
  }

  private static tokensOf(xml: string, element: string): string[] {
    return [...xml.matchAll(new RegExp(`<[^>]*:?${element}\\b[^>]*token="([^"]+)"`, 'g'))].map((m) => m[1]);
  }

  private static valuesOf(xml: string, element: string): string[] {
    return [...xml.matchAll(new RegExp(`<[^>]*:?${element}>([^<]+)<`, 'g'))].map((m) => m[1]);
  }

  /** Physical audio inputs (microphones) the device exposes. */
  async getAudioSources(): Promise<string[]> {
    return OnvifDevice.tokensOf(await this.mediaCall('GetAudioSources'), 'AudioSources');
  }

  /** Physical audio outputs (speaker / line-out) the device exposes. */
  async getAudioOutputs(): Promise<string[]> {
    return OnvifDevice.tokensOf(await this.mediaCall('GetAudioOutputs'), 'AudioOutputs');
  }

  /** Audio output configurations, incl. current volume (OutputLevel 0-100). */
  async getAudioOutputConfigurations(): Promise<{
    configTokens: string[];
    outputTokens: string[];
    outputLevels: number[];
    sendPrimaryAudio: string[];
  }> {
    const xml = await this.mediaCall('GetAudioOutputConfigurations');
    return {
      configTokens: OnvifDevice.tokensOf(xml, 'Configurations'),
      outputTokens: OnvifDevice.valuesOf(xml, 'OutputToken'),
      outputLevels: OnvifDevice.valuesOf(xml, 'OutputLevel').map(Number),
      sendPrimaryAudio: OnvifDevice.valuesOf(xml, 'SendPrimaryAudio'),
    };
  }

  /** Resolve the RTSP stream URI for a profile (RTP-Unicast over RTSP). */
  async getStreamUri(profileToken: string): Promise<string> {
    const body =
      `<GetStreamUri xmlns="${MED_NS}">` +
      `<StreamSetup><Stream xmlns="${SCHEMA_NS}">RTP-Unicast</Stream>` +
      `<Transport xmlns="${SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport></StreamSetup>` +
      `<ProfileToken>${encodeXml(profileToken)}</ProfileToken></GetStreamUri>`;
    const xml = await this.soap(this.requireMediaUrl(), body, true);
    const uri = firstTag(xml, 'Uri');
    if (!uri) throw new Error('no Uri in GetStreamUri response');
    return decodeXml(uri);
  }
}
