import { discoverDevices } from './onvif/discovery.ts';

const cams = await discoverDevices();
if (cams.length === 0) {
  console.log('ONVIF 카메라를 찾지 못했습니다. (같은 네트워크/스위치에 연결됐는지 확인)');
} else {
  console.log(`발견된 ONVIF 장치 ${cams.length}대:`);
  for (const c of cams) {
    console.log(`  ${c.ip}\t${c.name ?? '?'}\t${c.hardware ?? ''}`);
  }
  console.log('\n예) 테스트:  npm run m3 -- --host <IP> --user admin --pass CHANGEME --freq 1000 --ms 5000 --amp 0.9');
}
