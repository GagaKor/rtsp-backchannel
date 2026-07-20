import { OnvifDevice, type OnvifOptions, type OnvifProfile } from './deviceClient.ts';

export interface StreamUriOptions {
  host: string;
  user: string;
  pass: string;
  deviceUrls?: string[];
  timeoutMs?: number;
}

export interface StreamUri {
  profileToken: string;
  profileName?: string;
  uri: string;
}

interface StreamLookupDevice {
  connect(): Promise<unknown>;
  getProfiles(): Promise<OnvifProfile[]>;
  getStreamUri(profileToken: string): Promise<string>;
}

export interface StreamLookupDependencies {
  createDevice(
    host: string,
    user: string,
    pass: string,
    options: OnvifOptions,
  ): StreamLookupDevice;
}

const defaultDependencies: StreamLookupDependencies = {
  createDevice: (host, user, pass, options) => new OnvifDevice(host, user, pass, options),
};

function stripRtspUserInfo(uri: string): string {
  const scheme = /^rtsps?:\/\//i.exec(uri);
  if (!scheme) return uri;

  const authorityStart = scheme[0].length;
  const suffixOffset = uri.slice(authorityStart).search(/[/?#]/);
  const authorityEnd = suffixOffset < 0 ? uri.length : authorityStart + suffixOffset;
  const authority = uri.slice(authorityStart, authorityEnd);
  const userInfoEnd = authority.lastIndexOf('@');
  if (userInfoEnd < 0) return uri;

  const endpoint = authority.slice(userInfoEnd + 1);
  if (!endpoint) return uri;
  const sanitized =
    `${uri.slice(0, authorityStart)}${endpoint}${uri.slice(authorityEnd)}`;

  try {
    const parsed = new URL(sanitized);
    const protocol = parsed.protocol.toLowerCase();
    if ((protocol !== 'rtsp:' && protocol !== 'rtsps:') || !parsed.hostname) return uri;
  } catch {
    return uri;
  }

  return sanitized;
}

export async function getStreamUris(
  options: StreamUriOptions,
  dependencies: StreamLookupDependencies = defaultDependencies,
): Promise<StreamUri[]> {
  const device = dependencies.createDevice(options.host, options.user, options.pass, {
    ...(options.deviceUrls ? { deviceUrls: options.deviceUrls } : {}),
    ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
  });
  await device.connect();
  const profiles = await device.getProfiles();
  return Promise.all(
    profiles.map(async (profile) => ({
      profileToken: profile.token,
      ...(profile.name ? { profileName: profile.name } : {}),
      uri: stripRtspUserInfo(await device.getStreamUri(profile.token)),
    })),
  );
}
