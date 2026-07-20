import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  discoverDevices,
  parseProbeMatch,
  type DiscoveryDependencies,
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
  const dependencies: DiscoveryDependencies = {
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

  const devices = await discoverDevices(
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
