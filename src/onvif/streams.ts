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
      uri: await device.getStreamUri(profile.token),
    })),
  );
}
