/**
 * Discover ONVIF cameras on the local network via WS-Discovery (UDP multicast).
 *
 *   npm run discover
 */
import dgram from 'node:dgram';
import os from 'node:os';
import crypto from 'node:crypto';

const MCAST_ADDR = '239.255.255.250';
const MCAST_PORT = 3702;

function probeMessage(): Buffer {
  const id = `uuid:${crypto.randomUUID()}`;
  return Buffer.from(
    '<?xml version="1.0" encoding="UTF-8"?>' +
      '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"' +
      ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"' +
      ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"' +
      ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">' +
      `<e:Header><w:MessageID>${id}</w:MessageID>` +
      '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>' +
      '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>' +
      '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>',
  );
}

function localIpv4(): string[] {
  const out: string[] = [];
  for (const addrs of Object.values(os.networkInterfaces())) {
    for (const a of addrs ?? []) {
      if (a.family === 'IPv4' && !a.internal) out.push(a.address);
    }
  }
  return out;
}

function scope(scopes: string, key: string): string {
  const m = new RegExp(`onvif://www\\.onvif\\.org/${key}/([^\\s<]+)`).exec(scopes);
  return m ? decodeURIComponent(m[1]) : '';
}

interface Found {
  ip: string;
  name: string;
  hardware: string;
  xaddr: string;
}

async function discover(waitMs = 3000): Promise<Found[]> {
  const found = new Map<string, Found>();
  await Promise.all(
    localIpv4().map(
      (src) =>
        new Promise<void>((resolve) => {
          const sock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
          sock.on('message', (msg, rinfo) => {
            const text = msg.toString('utf8');
            if (text.includes(':5357/')) return; // Windows WSD, not a camera
            const xaddr = /XAddrs>([^<]+)</.exec(text)?.[1]?.split(/\s+/)[0] ?? '';
            const scopes = /Scopes>([^<]+)</.exec(text)?.[1] ?? '';
            found.set(rinfo.address, {
              ip: rinfo.address,
              name: scope(scopes, 'name'),
              hardware: scope(scopes, 'hardware'),
              xaddr,
            });
          });
          sock.on('error', () => resolve());
          sock.bind(0, src, () => {
            try {
              sock.setMulticastInterface(src);
            } catch {
              /* ignore */
            }
            const msg = probeMessage();
            for (let i = 0; i < 3; i++) sock.send(msg, MCAST_PORT, MCAST_ADDR);
            setTimeout(() => {
              sock.close();
              resolve();
            }, waitMs);
          });
        }),
    ),
  );
  return [...found.values()].sort((a, b) => a.ip.localeCompare(b.ip, undefined, { numeric: true }));
}

const cams = await discover();
if (cams.length === 0) {
  console.log('ONVIF 카메라를 찾지 못했습니다. (같은 네트워크/스위치에 연결됐는지 확인)');
} else {
  console.log(`발견된 ONVIF 장치 ${cams.length}대:`);
  for (const c of cams) {
    console.log(`  ${c.ip}\t${c.name || '?'}\t${c.hardware || ''}`);
  }
  console.log('\n예) 테스트:  npm run m3 -- --host <IP> --user admin --pass CHANGEME --freq 1000 --ms 5000 --amp 0.9');
}
