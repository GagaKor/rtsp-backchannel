import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import { test } from 'node:test';

import {
  discoverDevices,
  discoverDevicesWithDependencies,
  discoveryTargetsForTest,
  parseProbeMatch,
} from './discovery.ts';

const FIRST_RESPONSE = `<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference><a:Address>urn:uuid:camera-1</a:Address></a:EndpointReference>
  <d:Types>dn:NetworkVideoTransmitter</d:Types>
  <d:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/SM-DM-4M2W</d:Scopes>
  <d:XAddrs>http://10.128.10.141/onvif/device_service http://camera.local/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>`;

const SECOND_RESPONSE = `<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <e:Body><wsd:ProbeMatches><wsd:ProbeMatch>
  <w:EndpointReference><w:Address>urn:uuid:camera-1</w:Address></w:EndpointReference>
  <wsd:Types>tds:NetworkVideoTransmitter</wsd:Types>
  <wsd:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/location/Entrance</wsd:Scopes>
  <wsd:XAddrs>http://10.128.10.141:8000/onvif/device_service</wsd:XAddrs>
 </wsd:ProbeMatch></wsd:ProbeMatches></e:Body>
</e:Envelope>`;

test('parses namespace-independent ONVIF ProbeMatch metadata', () => {
  assert.deepEqual(parseProbeMatch(FIRST_RESPONSE, '10.128.10.141'), {
    ip: '10.128.10.141',
    xaddrs: [
      'http://10.128.10.141/onvif/device_service',
      'http://camera.local/onvif/device_service',
    ],
    scopes: [
      'onvif://www.onvif.org/name/Front%20Door',
      'onvif://www.onvif.org/hardware/SM-DM-4M2W',
    ],
    name: 'Front Door',
    hardware: 'SM-DM-4M2W',
    endpointReference: 'urn:uuid:camera-1',
  });
});

test('probes selected interfaces against one deadline and merges duplicate devices', async () => {
  const deadlines: number[] = [];
  const sources: string[] = [];
  const dependencies: Parameters<typeof discoverDevicesWithDependencies>[1] = {
    now: () => 1_000,
    randomUUID: () => 'probe-id',
    localIpv4: () => {
      throw new Error('explicit interfaces should be used');
    },
    probeInterface: async (source, _probe, deadline, onMessage) => {
      sources.push(source);
      deadlines.push(deadline);
      onMessage(source.endsWith('.10') ? FIRST_RESPONSE : SECOND_RESPONSE, '10.128.10.141');
    },
  };

  const devices = await discoverDevicesWithDependencies(
    { timeoutMs: 3_000, interfaces: ['10.0.0.10', '192.168.0.20'] },
    dependencies,
  );

  assert.deepEqual(sources.sort(), ['10.0.0.10', '192.168.0.20']);
  assert.deepEqual(deadlines, [4_000, 4_000]);
  assert.equal(devices.length, 1);
  assert.deepEqual(devices[0].xaddrs, [
    'http://10.128.10.141/onvif/device_service',
    'http://camera.local/onvif/device_service',
    'http://10.128.10.141:8000/onvif/device_service',
  ]);
  assert.deepEqual(devices[0].scopes, [
    'onvif://www.onvif.org/name/Front%20Door',
    'onvif://www.onvif.org/hardware/SM-DM-4M2W',
    'onvif://www.onvif.org/location/Entrance',
  ]);
});

test('expands every selected IP and CIDR and removes overlapping hosts', () => {
  assert.deepEqual(
    discoveryTargetsForTest([
      '10.0.0.0/30',
      '10.128.0.10',
      '10.0.0.1',
    ]),
    ['10.0.0.1', '10.0.0.2', '10.128.0.10'],
  );
});

test('actively discovers a selected ONVIF device service', async () => {
  let requestBody = '';
  let requestCount = 0;
  const server = http.createServer((request, response) => {
    requestCount++;
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      requestBody += chunk;
    });
    request.on('end', () => {
      response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
      response.end(
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">' +
          '<s:Body><GetSystemDateAndTimeResponse>' +
          '<SystemDateAndTime><UTCDateTime>' +
          '<Time><Hour>6</Hour><Minute>30</Minute><Second>0</Second></Time>' +
          '<Date><Year>2026</Year><Month>7</Month><Day>20</Day></Date>' +
          '</UTCDateTime></SystemDateAndTime>' +
          '</GetSystemDateAndTimeResponse></s:Body></s:Envelope>',
      );
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    const devices = await discoverDevices({
      cidrs: ['127.0.0.1', '127.0.0.1/32'],
      ports: [address.port],
      timeoutMs: 250,
      concurrency: 1,
    });

    assert.deepEqual(devices, [
      {
        ip: '127.0.0.1',
        xaddrs: [`http://127.0.0.1:${address.port}/onvif/device_service`],
        scopes: [],
      },
    ]);
    assert.equal(requestCount, 1);
    assert.match(requestBody, /GetSystemDateAndTime/);
    assert.doesNotMatch(requestBody, /wsse:Security/);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('bounds active CIDR requests globally while checking every port', async (t) => {
  const localAddress = Object.values(os.networkInterfaces())
    .flatMap((entries) => entries ?? [])
    .find((entry) => entry.family === 'IPv4' && !entry.internal)?.address;
  if (!localAddress) {
    t.skip('requires a non-loopback local IPv4 address');
    return;
  }

  let inFlight = 0;
  let peakInFlight = 0;
  let requestCount = 0;
  const responseBody =
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">' +
    '<s:Body><GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>' +
    '<Time><Hour>6</Hour><Minute>30</Minute><Second>0</Second></Time>' +
    '<Date><Year>2026</Year><Month>7</Month><Day>20</Day></Date>' +
    '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse></s:Body></s:Envelope>';
  const servers = [
    http.createServer((_request, response) => {
      requestCount++;
      inFlight++;
      peakInFlight = Math.max(peakInFlight, inFlight);
      setTimeout(() => {
        response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
        response.end(responseBody, () => inFlight--);
      }, 40);
    }),
    http.createServer((_request, response) => {
      requestCount++;
      inFlight++;
      peakInFlight = Math.max(peakInFlight, inFlight);
      setTimeout(() => {
        response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
        response.end(responseBody, () => inFlight--);
      }, 40);
    }),
  ];

  await Promise.all(
    servers.map(
      (server) =>
        new Promise<void>((resolve) => server.listen(0, '0.0.0.0', resolve)),
    ),
  );
  const ports = servers.map((server) => {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    return address.port;
  });

  try {
    const devices = await discoverDevices({
      cidrs: ['127.0.0.1/32', `${localAddress}/32`],
      ports,
      timeoutMs: 500,
      concurrency: 2,
    });

    assert.equal(requestCount, 4);
    assert.equal(peakInFlight, 2);
    const devicesByIp = new Map(devices.map((device) => [device.ip, device]));
    assert.equal(devices.length, 2);
    assert.equal(devicesByIp.size, 2);
    assert.deepEqual(devicesByIp.get('127.0.0.1'), {
      ip: '127.0.0.1',
      xaddrs: ports.map((port) => `http://127.0.0.1:${port}/onvif/device_service`),
      scopes: [],
    });
    assert.deepEqual(devicesByIp.get(localAddress), {
      ip: localAddress,
      xaddrs: ports.map((port) => `http://${localAddress}:${port}/onvif/device_service`),
      scopes: [],
    });
  } finally {
    await Promise.all(
      servers.map(
        (server) =>
          new Promise<void>((resolve, reject) =>
            server.close((error) => (error ? reject(error) : resolve())),
          ),
      ),
    );
  }
});

test('rejects invalid explicit IPv4 CIDRs before probing', async () => {
  await assert.rejects(
    discoverDevices({ cidrs: ['10.128.10.0/not-a-prefix'], timeoutMs: 1 }),
    /invalid IPv4 CIDR/,
  );
});
