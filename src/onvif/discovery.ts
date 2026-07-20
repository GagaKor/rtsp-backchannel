import crypto from 'node:crypto';
import dgram from 'node:dgram';
import os from 'node:os';

const MULTICAST_ADDRESS = '239.255.255.250';
const MULTICAST_PORT = 3702;

export interface DiscoveredDevice {
  ip: string;
  xaddrs: string[];
  scopes: string[];
  name?: string;
  hardware?: string;
  endpointReference?: string;
}

export interface DiscoveryOptions {
  timeoutMs?: number;
  interfaces?: string[];
}

export interface DiscoveryDependencies {
  now(): number;
  randomUUID(): string;
  localIpv4(): string[];
  probeInterface(
    source: string,
    probe: Buffer,
    deadlineMs: number,
    onMessage: (xml: string, sourceIp: string) => void,
  ): Promise<void>;
}

function decodeXml(value: string): string {
  return value
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'");
}

function firstTag(xml: string, name: string): string | undefined {
  const match = new RegExp(
    `<(?:[A-Za-z_][\\w.-]*:)?${name}\\b[^>]*>([\\s\\S]*?)</(?:[A-Za-z_][\\w.-]*:)?${name}>`,
    'i',
  ).exec(xml);
  return match ? decodeXml(match[1].trim()) : undefined;
}

function scopeValue(scopes: string[], key: string): string | undefined {
  const prefix = `onvif://www.onvif.org/${key}/`;
  const value = scopes.find((candidate) => candidate.toLowerCase().startsWith(prefix));
  if (!value) return undefined;
  try {
    return decodeURIComponent(value.slice(prefix.length));
  } catch {
    return value.slice(prefix.length);
  }
}

export function parseProbeMatch(xml: string, sourceIp: string): DiscoveredDevice | undefined {
  const types = firstTag(xml, 'Types') ?? '';
  const xaddrs = (firstTag(xml, 'XAddrs') ?? '').split(/\s+/).filter(Boolean);
  const scopes = (firstTag(xml, 'Scopes') ?? '').split(/\s+/).filter(Boolean);
  const isOnvif =
    types.includes('NetworkVideoTransmitter') ||
    scopes.some((value) => value.toLowerCase().startsWith('onvif://')) ||
    xaddrs.some((value) => /\/onvif\//i.test(value));
  if (!isOnvif || xaddrs.some((value) => value.includes(':5357/'))) return undefined;

  const name = scopeValue(scopes, 'name');
  const hardware = scopeValue(scopes, 'hardware');
  const endpointReference = firstTag(xml, 'Address');
  return {
    ip: sourceIp,
    xaddrs,
    scopes,
    ...(name ? { name } : {}),
    ...(hardware ? { hardware } : {}),
    ...(endpointReference ? { endpointReference } : {}),
  };
}

function probeMessage(id: string): Buffer {
  return Buffer.from(
    '<?xml version="1.0" encoding="UTF-8"?>' +
      '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"' +
      ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"' +
      ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"' +
      ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">' +
      `<e:Header><w:MessageID>uuid:${id}</w:MessageID>` +
      '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>' +
      '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>' +
      '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>' +
      '</d:Probe></e:Body></e:Envelope>',
  );
}

function localIpv4(): string[] {
  const addresses = new Set<string>();
  for (const entries of Object.values(os.networkInterfaces())) {
    for (const address of entries ?? []) {
      if (address.family === 'IPv4' && !address.internal) addresses.add(address.address);
    }
  }
  return [...addresses];
}

function probeInterface(
  source: string,
  probe: Buffer,
  deadlineMs: number,
  onMessage: (xml: string, sourceIp: string) => void,
): Promise<void> {
  return new Promise((resolve) => {
    const socket = dgram.createSocket({ type: 'udp4', reuseAddr: true });
    let finished = false;
    let timer: NodeJS.Timeout | undefined;
    const finish = () => {
      if (finished) return;
      finished = true;
      if (timer) clearTimeout(timer);
      try {
        socket.close();
      } catch {
        // The socket may fail before bind completes.
      }
      resolve();
    };

    socket.on('message', (message, remote) => {
      onMessage(message.toString('utf8'), remote.address);
    });
    socket.on('error', finish);
    socket.bind(0, source, () => {
      try {
        socket.setMulticastInterface(source);
      } catch {
        // Binding the source address is sufficient on platforms without this option.
      }
      for (let attempt = 0; attempt < 3; attempt++) {
        socket.send(probe, MULTICAST_PORT, MULTICAST_ADDRESS);
      }
      timer = setTimeout(finish, Math.max(0, deadlineMs - Date.now()));
    });
  });
}

const defaultDependencies: DiscoveryDependencies = {
  now: Date.now,
  randomUUID: crypto.randomUUID,
  localIpv4,
  probeInterface,
};

function mergeDevice(target: DiscoveredDevice, incoming: DiscoveredDevice): void {
  for (const xaddr of incoming.xaddrs) {
    if (!target.xaddrs.includes(xaddr)) target.xaddrs.push(xaddr);
  }
  for (const scope of incoming.scopes) {
    if (!target.scopes.includes(scope)) target.scopes.push(scope);
  }
  target.name ??= incoming.name;
  target.hardware ??= incoming.hardware;
  target.endpointReference ??= incoming.endpointReference;
}

export async function discoverDevices(
  options: DiscoveryOptions = {},
  dependencies: DiscoveryDependencies = defaultDependencies,
): Promise<DiscoveredDevice[]> {
  const timeoutMs = options.timeoutMs ?? 3_000;
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) {
    throw new RangeError('timeoutMs must be finite and 0 or greater');
  }
  const interfaces = [...new Set(options.interfaces ?? dependencies.localIpv4())];
  const deadline = dependencies.now() + timeoutMs;
  const probe = probeMessage(dependencies.randomUUID());
  const found = new Map<string, DiscoveredDevice>();
  const onMessage = (xml: string, sourceIp: string) => {
    const incoming = parseProbeMatch(xml, sourceIp);
    if (!incoming) return;
    const current = found.get(incoming.ip);
    if (current) mergeDevice(current, incoming);
    else found.set(incoming.ip, incoming);
  };

  await Promise.all(
    interfaces.map((source) =>
      dependencies.probeInterface(source, probe, deadline, onMessage).catch(() => {}),
    ),
  );
  return [...found.values()].sort((left, right) =>
    left.ip.localeCompare(right.ip, undefined, { numeric: true }),
  );
}
