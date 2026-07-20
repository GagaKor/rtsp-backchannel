import crypto from 'node:crypto';
import dgram from 'node:dgram';
import { isIPv4 } from 'node:net';
import os from 'node:os';

import { OnvifDevice } from './deviceClient.ts';

const MULTICAST_ADDRESS = '239.255.255.250';
const MULTICAST_PORT = 3702;
const DEFAULT_CIDR_PORTS = [80, 8000, 443];
const DEFAULT_CIDR_CONCURRENCY = 64;
const DEFAULT_CIDR_TIMEOUT_MS = 1_000;
const MAX_CIDR_HOSTS = 4_096;

export interface DiscoveredDevice {
  ip: string;
  xaddrs: string[];
  scopes: string[];
  name?: string;
  hardware?: string;
  endpointReference?: string;
}

export interface DiscoveryOptions {
  /** Local multicast deadline, or per-request timeout during active scanning. */
  timeoutMs?: number;
  /** Local computer IPv4 addresses used for multicast discovery. */
  interfaces?: string[];
  /** IPv4 CIDRs or individual IPv4 addresses to scan instead of multicast. */
  cidrs?: string[];
  /** ONVIF Device Service ports used in CIDR mode. Defaults to 80, 8000, 443. */
  ports?: number[];
  /** Number of hosts scanned concurrently in CIDR mode. Defaults to 64. */
  concurrency?: number;
}

interface DiscoveryDependencies {
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

function ipv4ToInteger(ip: string): number {
  const octets = ip.split('.').map(Number);
  return (
    octets[0] * 0x1000000 + octets[1] * 0x10000 + octets[2] * 0x100 + octets[3]
  ) >>> 0;
}

function integerToIpv4(value: number): string {
  return [
    Math.floor(value / 0x1000000),
    Math.floor(value / 0x10000) & 0xff,
    Math.floor(value / 0x100) & 0xff,
    value & 0xff,
  ].join('.');
}

function cidrHosts(cidrs: string[]): string[] {
  const addresses = new Set<number>();
  for (const rawCidr of cidrs) {
    const target = rawCidr.trim();
    const cidr = target.includes('/') ? target : `${target}/32`;
    const [ip, prefixText, extra] = cidr.split('/');
    const prefix = Number(prefixText);
    if (
      extra !== undefined ||
      !isIPv4(ip) ||
      !/^\d+$/.test(prefixText ?? '') ||
      prefix < 0 ||
      prefix > 32
    ) {
      throw new RangeError(`invalid IPv4 CIDR: ${rawCidr}`);
    }

    const size = 2 ** (32 - prefix);
    const network = Math.floor(ipv4ToInteger(ip) / size) * size;
    const first = prefix <= 30 ? network + 1 : network;
    const last = prefix <= 30 ? network + size - 2 : network + size - 1;
    const hostCount = Math.max(0, last - first + 1);
    if (hostCount > MAX_CIDR_HOSTS) {
      throw new RangeError(`CIDR discovery is limited to ${MAX_CIDR_HOSTS} IPv4 hosts`);
    }
    for (let address = first; address <= last; address++) {
      addresses.add(address);
      if (addresses.size > MAX_CIDR_HOSTS) {
        throw new RangeError(`CIDR discovery is limited to ${MAX_CIDR_HOSTS} IPv4 hosts`);
      }
    }
  }
  return [...addresses].sort((left, right) => left - right).map(integerToIpv4);
}

/** @internal Address expansion coverage without performing network I/O. */
export function discoveryTargetsForTest(targets: string[]): string[] {
  return cidrHosts(targets);
}

function cidrPortUrls(ip: string, ports: number[]): string[] {
  return ports.map((port) => {
    const secure = port === 443;
    const defaultPort = secure ? 443 : 80;
    return `${secure ? 'https' : 'http'}://${ip}${port === defaultPort ? '' : `:${port}`}` +
      '/onvif/device_service';
  });
}

function validatedPorts(ports: number[] | undefined): number[] {
  const values = [...new Set(ports ?? DEFAULT_CIDR_PORTS)];
  if (
    values.length === 0 ||
    values.some((port) => !Number.isInteger(port) || port < 1 || port > 65_535)
  ) {
    throw new RangeError('ports must contain integers between 1 and 65535');
  }
  return values;
}

async function probeCidrHost(
  ip: string,
  ports: number[],
  timeoutMs: number,
): Promise<DiscoveredDevice | undefined> {
  const xaddrs: string[] = [];
  for (const url of cidrPortUrls(ip, ports)) {
    try {
      const device = new OnvifDevice(ip, '', '', { deviceUrls: [url], timeoutMs });
      await device.getSystemDateAndTime(url);
      xaddrs.push(url);
    } catch {
      // Continue probing the remaining service ports for this host.
    }
  }
  return xaddrs.length > 0 ? { ip, xaddrs, scopes: [] } : undefined;
}

async function discoverCidrs(options: DiscoveryOptions): Promise<DiscoveredDevice[]> {
  if (options.interfaces?.length) {
    throw new RangeError('interfaces cannot be combined with cidrs');
  }
  const timeoutMs = options.timeoutMs ?? DEFAULT_CIDR_TIMEOUT_MS;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('timeoutMs must be finite and greater than 0 for CIDR discovery');
  }
  const concurrency = options.concurrency ?? DEFAULT_CIDR_CONCURRENCY;
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 256) {
    throw new RangeError('concurrency must be an integer between 1 and 256');
  }
  const hosts = cidrHosts(options.cidrs ?? []);
  const ports = validatedPorts(options.ports);
  const found: DiscoveredDevice[] = [];
  let nextIndex = 0;

  const worker = async () => {
    while (nextIndex < hosts.length) {
      const host = hosts[nextIndex++];
      const device = await probeCidrHost(host, ports, timeoutMs);
      if (device) found.push(device);
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(concurrency, hosts.length) }, () => worker()),
  );
  return found.sort((left, right) =>
    left.ip.localeCompare(right.ip, undefined, { numeric: true }),
  );
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

async function discoverLocalDevices(
  options: DiscoveryOptions,
  dependencies: DiscoveryDependencies,
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

/** @internal Test seam for deterministic multicast discovery coverage. */
export function discoverDevicesWithDependencies(
  options: DiscoveryOptions,
  dependencies: DiscoveryDependencies,
): Promise<DiscoveredDevice[]> {
  return discoverLocalDevices(options, dependencies);
}

export function discoverDevices(
  options: DiscoveryOptions = {},
): Promise<DiscoveredDevice[]> {
  if (options.cidrs?.length) return discoverCidrs(options);
  return discoverLocalDevices(options, defaultDependencies);
}
