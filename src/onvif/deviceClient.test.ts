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
