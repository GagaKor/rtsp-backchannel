import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import { test } from 'node:test';

import { OnvifDevice } from './deviceClient.ts';

async function captureDeviceInformationRequest(
  device: OnvifDevice,
): Promise<string> {
  let requestBody = '';
  const server = http.createServer((request, response) => {
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      requestBody += chunk;
    });
    request.on('end', () => {
      response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
      response.end(
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">' +
          '<s:Body><GetDeviceInformationResponse>' +
          '<Manufacturer>Test Camera</Manufacturer>' +
          '</GetDeviceInformationResponse></s:Body></s:Envelope>',
      );
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    await device.getDeviceInformation(
      `http://127.0.0.1:${address.port}/onvif/device_service`,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
  return requestBody;
}

test('omits WS-Security entirely when both ONVIF credentials are empty', async () => {
  const body = await captureDeviceInformationRequest(new OnvifDevice('camera'));

  assert.match(body, /<s:Header><\/s:Header>/);
  assert.doesNotMatch(body, /wsse:Security/);
  assert.doesNotMatch(body, /UsernameToken/);
  assert.doesNotMatch(body, /PasswordDigest/);
});

test('builds PasswordDigest from nonce, created time, and the exact password bytes', async () => {
  const password = 'p@ss:word';
  const body = await captureDeviceInformationRequest(
    new OnvifDevice('camera', 'admin&ops', password),
  );
  const nonceBase64 = /<wsse:Nonce>([^<]+)<\/wsse:Nonce>/.exec(body)?.[1];
  const created = /<wsu:Created>([^<]+)<\/wsu:Created>/.exec(body)?.[1];
  const digest = /<wsse:Password[^>]*>([^<]+)<\/wsse:Password>/.exec(body)?.[1];
  assert.ok(nonceBase64);
  assert.ok(created);
  assert.ok(digest);

  const expected = crypto
    .createHash('sha1')
    .update(Buffer.concat([
      Buffer.from(nonceBase64, 'base64'),
      Buffer.from(created, 'utf8'),
      Buffer.from(password, 'utf8'),
    ]))
    .digest('base64');
  assert.equal(digest, expected);
  assert.match(body, /<wsse:Username>admin&amp;ops<\/wsse:Username>/);
  assert.match(body, /#PasswordDigest/);
});

test('enforces an absolute wall-clock timeout during a trickle response', async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
    const interval = setInterval(() => response.write('x'), 5);
    response.on('close', () => clearInterval(interval));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    const startedAt = Date.now();
    await assert.rejects(
      new OnvifDevice('camera', '', '', {
        timeoutMs: 75,
        deviceUrls: [`http://127.0.0.1:${address.port}/onvif/device_service`],
      }).getSystemDateAndTime(`http://127.0.0.1:${address.port}/onvif/device_service`),
      /timeout/i,
    );
    assert.ok(Date.now() - startedAt < 400);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('rejects responses whose headers exceed the ONVIF limit', async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, {
      'Content-Type': 'application/soap+xml',
      'X-Oversized': 'x'.repeat(70_000),
    });
    response.end();
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    await assert.rejects(
      new OnvifDevice('camera', '', '', {
        timeoutMs: 500,
        deviceUrls: [`http://127.0.0.1:${address.port}/onvif/device_service`],
      }).getSystemDateAndTime(`http://127.0.0.1:${address.port}/onvif/device_service`),
      /header/i,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('rejects responses whose body exceeds the ONVIF limit', async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
    response.end('x'.repeat(1_048_577));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    await assert.rejects(
      new OnvifDevice('camera', '', '', {
        timeoutMs: 500,
        deviceUrls: [`http://127.0.0.1:${address.port}/onvif/device_service`],
      }).getSystemDateAndTime(`http://127.0.0.1:${address.port}/onvif/device_service`),
      /body/i,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('keeps the three-call connect sequence and exposes selected and explicit read-only endpoints', async () => {
  const requests: Array<{ path: string; body: string }> = [];
  const server = http.createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      requests.push({ path: request.url ?? '', body });
      const soapBody = /<s:Body>([\s\S]*?)<\/s:Body>/.exec(body)?.[1] ?? '';
      const envelope = (content: string) =>
        `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>${content}</s:Body></s:Envelope>`;
      response.setHeader('Content-Type', 'application/soap+xml');
      if (soapBody.includes('GetSystemDateAndTime')) {
        response.end(envelope(
          '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
          + '<Time><Hour>12</Hour><Minute>30</Minute><Second>0</Second></Time>'
          + '<Date><Year>2026</Year><Month>8</Month><Day>6</Day></Date>'
          + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>',
        ));
      } else if (soapBody.includes('GetDeviceInformation')) {
        response.end(envelope(
          '<GetDeviceInformationResponse><Manufacturer>Test Camera</Manufacturer>'
          + '</GetDeviceInformationResponse>',
        ));
      } else if (soapBody.includes('<Category>Media</Category>')) {
        const address = server.address();
        assert.ok(address && typeof address !== 'string');
        response.end(envelope(
          '<GetCapabilitiesResponse><Capabilities><Media><XAddr>'
          + `http://127.0.0.1:${address.port}/advertised/media`
          + '</XAddr></Media></Capabilities></GetCapabilitiesResponse>',
        ));
      } else if (request.url === '/auth-fault') {
        response.statusCode = 401;
        response.end(envelope('<s:Fault><s:Reason><s:Text>Not authorized</s:Text></s:Reason></s:Fault>'));
      } else {
        response.end(envelope('<GetScopesResponse/>'));
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const selectedDeviceUrl = `http://127.0.0.1:${address.port}/selected/device`;
  const device = new OnvifDevice('camera', 'admin', 'password', {
    deviceUrls: [selectedDeviceUrl],
  });

  try {
    await device.connect();
    const connectBodies = requests.map(({ body }) =>
      /<s:Body>([\s\S]*?)<\/s:Body>/.exec(body)?.[1]);
    assert.deepEqual(connectBodies, [
      `<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>`,
      `<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>`,
      `<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl"><Category>Media</Category></GetCapabilities>`,
    ]);

    const selected = await device.readOnlyCall(
      `<GetScopes xmlns="http://www.onvif.org/ver10/device/wsdl"/>`,
    );
    const authFault = await device.readOnlyCall(
      `<GetScopes xmlns="http://www.onvif.org/ver10/device/wsdl"/>`,
      `http://127.0.0.1:${address.port}/auth-fault`,
    );

    assert.equal(requests[3].path, '/selected/device');
    assert.equal(selected.statusCode, 200);
    assert.match(selected.xml, /GetScopesResponse/);
    assert.equal(requests[4].path, '/auth-fault');
    assert.equal(authFault.statusCode, 401);
    assert.match(authFault.xml, /Not authorized/);
    assert.match(requests[3].body, /wsse:Security/);
    assert.match(requests[4].body, /wsse:Security/);
    assert.equal(device.connectedMediaUrl(), `http://127.0.0.1:${address.port}/advertised/media`);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('rejects unsafe service URLs without transmitting a request or exposing URL contents', async () => {
  let requestCount = 0;
  const server = http.createServer((_request, response) => {
    requestCount++;
    response.setHeader('Content-Type', 'application/soap+xml');
    response.end(
      '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
      + '<GetDeviceInformationResponse><Manufacturer>Unexpected Camera</Manufacturer>'
      + '</GetDeviceInformationResponse></s:Body></s:Envelope>',
    );
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const device = new OnvifDevice('camera', 'viewer', 'camera-secret');
  const endpoints = [
    `ftp://127.0.0.1:${address.port}/must-not-reach`,
    `http://viewer:url-secret@127.0.0.1:${address.port}/must-not-reach`,
    '/relative/onvif/device_service',
  ];

  try {
    const outcomes: string[] = [];
    for (const endpoint of endpoints) {
      try {
        await device.getDeviceInformation(endpoint);
        outcomes.push('resolved');
      } catch (error) {
        outcomes.push(error instanceof Error ? error.message : String(error));
      }
    }

    assert.deepEqual({ requestCount, outcomes }, {
      requestCount: 0,
      outcomes: endpoints.map(() => 'invalid ONVIF service URL'),
    });
    assert.doesNotMatch(JSON.stringify(outcomes), /viewer|camera-secret|url-secret|must-not-reach/i);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('keeps credential-like hosts and service URLs out of final connect errors', async () => {
  const devices = [
    new OnvifDevice('viewer:top-secret@camera'),
    new OnvifDevice('camera', '', '', {
      deviceUrls: ['http://viewer:url-secret@camera/onvif/device_service'],
    }),
  ];
  const messages: string[] = [];

  for (const device of devices) {
    try {
      await device.connect();
      messages.push('resolved');
    } catch (error) {
      messages.push(error instanceof Error ? error.message : String(error));
    }
  }

  assert.deepEqual(messages, [
    'ONVIF connect failed: invalid ONVIF service URL',
    'ONVIF connect failed: invalid ONVIF service URL',
  ]);
  assert.doesNotMatch(
    JSON.stringify(messages),
    /viewer|secret|@camera/,
  );
});
