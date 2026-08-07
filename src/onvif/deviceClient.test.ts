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
          '<s:Body><tds:GetDeviceInformationResponse ' +
          'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">' +
          '<tds:Manufacturer>Test Camera</tds:Manufacturer>' +
          '</tds:GetDeviceInformationResponse></s:Body></s:Envelope>',
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

async function getDeviceInformationFromResponse(xml: string) {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'application/soap+xml' });
    response.end(xml);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    return await new OnvifDevice('camera').getDeviceInformation(
      `http://127.0.0.1:${address.port}/onvif/device_service`,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
}

function successfulConnectResponse(requestBody: string, mediaUrl: string): string {
  const envelope = (content: string) =>
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    + `<s:Body>${content}</s:Body></s:Envelope>`;
  if (requestBody.includes('GetSystemDateAndTime')) {
    return envelope(
      '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
      + '<Time><Hour>12</Hour><Minute>30</Minute><Second>0</Second></Time>'
      + '<Date><Year>2026</Year><Month>8</Month><Day>7</Day></Date>'
      + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>',
    );
  }
  if (requestBody.includes('GetDeviceInformation')) {
    return envelope(
      '<tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
      + '<tds:Manufacturer>normal-response-payload-secret</tds:Manufacturer>'
      + '</tds:GetDeviceInformationResponse>',
    );
  }
  return envelope(
    '<GetCapabilitiesResponse><Capabilities><Media><XAddr>'
    + mediaUrl
    + '</XAddr></Media></Capabilities></GetCapabilitiesResponse>',
  );
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

test('XML-decodes and trims only direct strict DeviceInfo fields', async () => {
  const info = await getDeviceInformationFromResponse(
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    + ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:vendor="urn:vendor">'
    + '<s:Body><tds:GetDeviceInformationResponse>'
    + '<vendor:Manufacturer>wrong-namespace-marker</vendor:Manufacturer>'
    + '<vendor:Wrapper><tds:Model>nested-decoy-marker</tds:Model></vendor:Wrapper>'
    + '<tds:Manufacturer> \tAcme &amp; Co\r\n </tds:Manufacturer>'
    + '<tds:Model> Model &lt;One&gt; </tds:Model>'
    + '<tds:FirmwareVersion> \r\n\t </tds:FirmwareVersion>'
    + '<tds:SerialNumber> S&amp;1 </tds:SerialNumber>'
    + '</tds:GetDeviceInformationResponse></s:Body></s:Envelope>',
  );

  assert.deepEqual(info, {
    manufacturer: 'Acme & Co',
    model: 'Model <One>',
    serial: 'S&1',
  });
});

test('rejects wrong-operation and nested DeviceInfo decoys with payload-free errors', async () => {
  const soapStart = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    + ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:vendor="urn:vendor"><s:Body>';
  const cases = [
    soapStart
      + '<vendor:GetDeviceInformationResponse><vendor:Manufacturer>wrong-operation-marker'
      + '</vendor:Manufacturer></vendor:GetDeviceInformationResponse></s:Body></s:Envelope>',
    soapStart
      + '<vendor:Wrapper><tds:GetDeviceInformationResponse>'
      + '<tds:Manufacturer>nested-response-marker</tds:Manufacturer>'
      + '</tds:GetDeviceInformationResponse></vendor:Wrapper></s:Body></s:Envelope>',
  ];

  for (const xml of cases) {
    await assert.rejects(
      getDeviceInformationFromResponse(xml),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, 'invalid GetDeviceInformation response');
        assert.doesNotMatch(error.message, /wrong-operation-marker|nested-response-marker/);
        return true;
      },
    );
  }
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

test('rejects every non-2xx connect response without following redirects or reflecting secrets', async () => {
  let attackerConnections = 0;
  let attackerRequests = 0;
  const attacker = http.createServer((_request, response) => {
    attackerRequests++;
    response.end();
  });
  attacker.on('connection', () => {
    attackerConnections++;
  });
  await new Promise<void>((resolve) => attacker.listen(0, '127.0.0.1', resolve));
  const attackerAddress = attacker.address();
  assert.ok(attackerAddress && typeof attackerAddress !== 'string');

  const server = http.createServer((request, response) => {
    let requestBody = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      requestBody += chunk;
    });
    request.on('end', () => {
      const statusCode = Number(request.url?.split('/')[2]);
      const address = server.address();
      assert.ok(address && typeof address !== 'string');
      const headers = statusCode >= 301 && statusCode <= 303
        ? {
            'Content-Type': 'application/soap+xml',
            Location: `http://127.0.0.1:${attackerAddress.port}/redirect-secret-marker`,
          }
        : { 'Content-Type': 'application/soap+xml' };
      response.writeHead(statusCode, headers);
      response.end(successfulConnectResponse(
        requestBody,
        `http://127.0.0.1:${address.port}/media_service`,
      ));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    for (const statusCode of [301, 302, 303, 401, 403, 404, 500]) {
      const deviceUrl = `http://127.0.0.1:${address.port}/status/${statusCode}/url-secret-marker`;
      await assert.rejects(
        new OnvifDevice('camera', 'viewer-marker', 'credential-secret-marker', {
          deviceUrls: [deviceUrl],
        }).connect(),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.equal(error.message, 'ONVIF connect failed: request failed');
          assert.doesNotMatch(
            error.message,
            /viewer-marker|credential-secret-marker|normal-response-payload-secret|url-secret-marker|redirect-secret-marker/,
          );
          return true;
        },
      );
    }
    assert.equal(attackerRequests, 0);
    assert.equal(attackerConnections, 0);
  } finally {
    await Promise.all([
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve()))),
      new Promise<void>((resolve, reject) =>
        attacker.close((error) => (error ? reject(error) : resolve()))),
    ]);
  }
});

test('rejects structurally valid 5xx SOAP 1.1 and 1.2 Faults with a fixed classification', async () => {
  const soap12Fault =
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    + ' xmlns:ter="http://www.onvif.org/ver10/error"><s:Body><s:Fault>'
    + '<s:Code><s:Value>s:Sender</s:Value><s:Subcode>'
    + '<s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>'
    + '<s:Reason><s:Text>fault-payload-secret</s:Text></s:Reason>'
    + '</s:Fault></s:Body></s:Envelope>';
  const soap11Fault =
    '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"'
    + ' xmlns:ter="http://www.onvif.org/ver10/error"><env:Body><env:Fault>'
    + '<faultcode>ter:NotAuthorized</faultcode>'
    + '<faultstring>fault-payload-secret</faultstring>'
    + '</env:Fault></env:Body></env:Envelope>';
  const server = http.createServer((request, response) => {
    response.writeHead(request.url === '/outside-5xx' ? 600 : 500, {
      'Content-Type': 'application/soap+xml',
    });
    response.end(request.url === '/soap11' ? soap11Fault : soap12Fault);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    for (const path of ['soap11', 'soap12']) {
      await assert.rejects(
        new OnvifDevice('camera').getDeviceInformation(
          `http://127.0.0.1:${address.port}/${path}`,
        ),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.equal(error.message, 'ONVIF SOAP Fault');
          assert.doesNotMatch(error.message, /fault-payload-secret|127\.0\.0\.1/);
          return true;
        },
      );
    }
    await assert.rejects(
      new OnvifDevice('camera').getDeviceInformation(
        `http://127.0.0.1:${address.port}/outside-5xx`,
      ),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, 'ONVIF HTTP response error');
        assert.doesNotMatch(error.message, /fault-payload-secret|127\.0\.0\.1/);
        return true;
      },
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('rejects a valid 5xx SOAP Fault before legacy parsers can read Detail decoys', async () => {
  const server = http.createServer((request, response) => {
    const statusCode = request.url === '/status-200' ? 200 : 500;
    response.writeHead(statusCode, { 'Content-Type': 'application/soap+xml' });
    response.end(
      '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><s:Fault>'
      + '<s:Code><s:Value>s:Receiver</s:Value></s:Code>'
      + '<s:Reason><s:Text>fault-detail-secret</s:Text></s:Reason><s:Detail>'
      + '<UTCDateTime><Time><Hour>12</Hour></Time>'
      + '<Date><Year>2026</Year><Month>8</Month><Day>7</Day></Date></UTCDateTime>'
      + '<XAddr>http://attacker.example/fault-detail-secret</XAddr>'
      + '<Profiles token="fault-detail-secret"/>'
      + '</s:Detail></s:Fault></s:Body></s:Envelope>',
    );
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    for (const path of ['status-200', 'status-500']) {
      await assert.rejects(
        new OnvifDevice('camera').getSystemDateAndTime(
          `http://127.0.0.1:${address.port}/${path}`,
        ),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.equal(error.message, 'ONVIF SOAP Fault');
          assert.doesNotMatch(error.message, /fault-detail-secret|attacker|127\.0\.0\.1/);
          return true;
        },
      );
    }
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('does not hide third-stage HTTP authentication or SOAP Fault responses during connect', async () => {
  const requestCounts = new Map<string, number>();
  const server = http.createServer((request, response) => {
    let requestBody = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      requestBody += chunk;
    });
    request.on('end', () => {
      const mode = request.url?.split('/')[2] ?? '';
      requestCounts.set(mode, (requestCounts.get(mode) ?? 0) + 1);
      const address = server.address();
      assert.ok(address && typeof address !== 'string');
      response.setHeader('Content-Type', 'application/soap+xml');
      if (!requestBody.includes('GetCapabilities')) {
        response.end(successfulConnectResponse(
          requestBody,
          `http://127.0.0.1:${address.port}/media_service`,
        ));
        return;
      }
      if (mode === 'fault') {
        response.statusCode = 500;
        response.end(
          '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
          + '<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>'
          + '<s:Reason><s:Text>third-stage-fault-secret</s:Text></s:Reason>'
          + '</s:Fault></s:Body></s:Envelope>',
        );
        return;
      }
      response.statusCode = Number(mode);
      response.end(successfulConnectResponse(
        requestBody,
        `http://127.0.0.1:${address.port}/media_service`,
      ));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    for (const mode of ['401', '403', 'fault']) {
      await assert.rejects(
        new OnvifDevice('camera', 'viewer', 'third-stage-credential-secret', {
          deviceUrls: [`http://127.0.0.1:${address.port}/mode/${mode}/device_service`],
        }).connect(),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.equal(error.message, 'ONVIF connect failed: request failed');
          assert.doesNotMatch(
            error.message,
            /viewer|third-stage-credential-secret|third-stage-fault-secret|127\.0\.0\.1/,
          );
          return true;
        },
      );
      assert.equal(requestCounts.get(mode), 3);
    }
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('derives the legacy Media URL only after a successful capability response without XAddr', async () => {
  const server = http.createServer((request, response) => {
    let requestBody = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      requestBody += chunk;
    });
    request.on('end', () => {
      const address = server.address();
      assert.ok(address && typeof address !== 'string');
      response.setHeader('Content-Type', 'application/soap+xml');
      if (requestBody.includes('GetCapabilities')) {
        response.end(
          '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
          + '<GetCapabilitiesResponse><Capabilities/></GetCapabilitiesResponse>'
          + '</s:Body></s:Envelope>',
        );
      } else {
        response.end(successfulConnectResponse(
          requestBody,
          `http://127.0.0.1:${address.port}/unused-media`,
        ));
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const deviceUrl = `http://127.0.0.1:${address.port}/onvif/device_service`;

  try {
    const device = new OnvifDevice('camera', '', '', { deviceUrls: [deviceUrl] });
    await device.connect();
    assert.equal(
      device.connectedMediaUrl(),
      `http://127.0.0.1:${address.port}/onvif/media_service`,
    );
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});

test('rejects 5xx fault-looking envelopes that contain operation-response decoys', async () => {
  const operationResponse =
    '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
    + '<Time><Hour>12</Hour></Time>'
    + '<Date><Year>2026</Year><Month>8</Month><Day>7</Day></Date>'
    + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>';
  const server = http.createServer((request, response) => {
    const body = request.url === '/body-sibling'
      ? `<s:Body><s:Fault/>${operationResponse}</s:Body>`
      : `<s:Body><s:Fault/></s:Body>${operationResponse}`;
    response.writeHead(500, { 'Content-Type': 'application/soap+xml' });
    response.end(
      '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
      + `${body}</s:Envelope>`,
    );
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');

  try {
    for (const path of ['body-sibling', 'envelope-sibling']) {
      await assert.rejects(
        new OnvifDevice('camera').getSystemDateAndTime(
          `http://127.0.0.1:${address.port}/${path}`,
        ),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.equal(error.message, 'ONVIF HTTP response error');
          return true;
        },
      );
    }
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
          '<tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
          + '<tds:Manufacturer>Test Camera</tds:Manufacturer>'
          + '</tds:GetDeviceInformationResponse>',
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

test('rejects an advertised cross-host Media XAddr before connecting to the attacker', async () => {
  let attackerRequests = 0;
  const attacker = http.createServer((_request, response) => {
    attackerRequests++;
    response.end();
  });
  await new Promise<void>((resolve) => attacker.listen(0, resolve));
  const attackerAddress = attacker.address();
  assert.ok(attackerAddress && typeof attackerAddress !== 'string');

  let deviceRequests = 0;
  const deviceServer = http.createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      deviceRequests++;
      const envelope = (content: string) =>
        `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>${content}</s:Body></s:Envelope>`;
      response.setHeader('Content-Type', 'application/soap+xml');
      if (body.includes('GetSystemDateAndTime')) {
        response.end(envelope(
          '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
          + '<Time><Hour>12</Hour><Minute>30</Minute><Second>0</Second></Time>'
          + '<Date><Year>2026</Year><Month>8</Month><Day>6</Day></Date>'
          + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>',
        ));
      } else if (body.includes('GetDeviceInformation')) {
        response.end(envelope(
          '<tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>',
        ));
      } else {
        response.end(envelope(
          '<GetCapabilitiesResponse><Capabilities><Media><XAddr>'
          + `http://localhost:${attackerAddress.port}/advertised/media`
          + '</XAddr></Media></Capabilities></GetCapabilitiesResponse>',
        ));
      }
    });
  });
  await new Promise<void>((resolve) => deviceServer.listen(0, '127.0.0.1', resolve));
  const deviceAddress = deviceServer.address();
  assert.ok(deviceAddress && typeof deviceAddress !== 'string');

  try {
    const client = new OnvifDevice('camera', 'viewer', 'camera-secret', {
      deviceUrls: [`http://127.0.0.1:${deviceAddress.port}/onvif/device_service`],
    });
    await assert.rejects(client.connect(), /invalid ONVIF service URL/);
    assert.equal(deviceRequests, 3);
    assert.equal(attackerRequests, 0);
  } finally {
    await Promise.all([
      new Promise<void>((resolve, reject) =>
        deviceServer.close((error) => (error ? reject(error) : resolve()))),
      new Promise<void>((resolve, reject) =>
        attacker.close((error) => (error ? reject(error) : resolve()))),
    ]);
  }
});

test('binds explicit authenticated calls by scheme and canonical host while allowing ports and paths', async () => {
  let peerRequests = 0;
  let peerConnections = 0;
  const peer = http.createServer((_request, response) => {
    peerRequests++;
    response.setHeader('Content-Type', 'application/soap+xml');
    response.end(
      '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
      + '<s:Body><GetScopesResponse/></s:Body></s:Envelope>',
    );
  });
  peer.on('connection', () => {
    peerConnections++;
  });
  await new Promise<void>((resolve) => peer.listen(0, resolve));
  const peerAddress = peer.address();
  assert.ok(peerAddress && typeof peerAddress !== 'string');

  const deviceServer = http.createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      const envelope = (content: string) =>
        `<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>${content}</s:Body></s:Envelope>`;
      response.setHeader('Content-Type', 'application/soap+xml');
      if (body.includes('GetSystemDateAndTime')) {
        response.end(envelope(
          '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
          + '<Time><Hour>12</Hour></Time><Date><Year>2026</Year><Month>8</Month><Day>6</Day></Date>'
          + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>',
        ));
      } else if (body.includes('GetDeviceInformation')) {
        response.end(envelope(
          '<tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>',
        ));
      } else {
        response.end(envelope(
          '<GetCapabilitiesResponse><Capabilities><Media><XAddr>'
          + `http://127.0.0.1:${peerAddress.port}/trusted/media`
          + '</XAddr></Media></Capabilities></GetCapabilitiesResponse>',
        ));
      }
    });
  });
  await new Promise<void>((resolve) => deviceServer.listen(0, '127.0.0.1', resolve));
  const deviceAddress = deviceServer.address();
  assert.ok(deviceAddress && typeof deviceAddress !== 'string');

  try {
    const client = new OnvifDevice('camera', 'viewer', 'camera-secret', {
      deviceUrls: [`http://127.1:${deviceAddress.port}/onvif/device_service`],
    });
    await client.connect();

    await assert.rejects(
      client.readOnlyCall(
        '<GetScopes xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
        `http://localhost:${peerAddress.port}/attacker`,
      ),
      /invalid ONVIF service URL/,
    );
    assert.equal(peerRequests, 0);
    assert.equal(peerConnections, 0);

    await assert.rejects(
      client.readOnlyCall(
        '<GetScopes xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
        `https://127.0.0.1:${peerAddress.port}/wrong-scheme`,
      ),
      /invalid ONVIF service URL/,
    );
    assert.equal(peerConnections, 0);

    const allowed = await client.readOnlyCall(
      '<GetScopes xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
      `http://127.0.0.1:${peerAddress.port}/different/path`,
    );
    assert.equal(allowed.statusCode, 200);
    assert.equal(peerRequests, 1);
    assert.equal(peerConnections, 1);
  } finally {
    await Promise.all([
      new Promise<void>((resolve, reject) =>
        deviceServer.close((error) => (error ? reject(error) : resolve()))),
      new Promise<void>((resolve, reject) =>
        peer.close((error) => (error ? reject(error) : resolve()))),
    ]);
  }
});

test('rejects unsafe service URLs without transmitting a request or exposing URL contents', async () => {
  let requestCount = 0;
  const server = http.createServer((_request, response) => {
    requestCount++;
    response.setHeader('Content-Type', 'application/soap+xml');
    response.end(
      '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
      + '<tds:GetDeviceInformationResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
      + '<tds:Manufacturer>Unexpected Camera</tds:Manufacturer>'
      + '</tds:GetDeviceInformationResponse></s:Body></s:Envelope>',
    );
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const device = new OnvifDevice('camera', 'viewer', 'camera-secret');
  const endpoints = [
    `ftp://127.0.0.1:${address.port}/must-not-reach`,
    `http://viewer:url-secret@127.0.0.1:${address.port}/must-not-reach`,
    `http://127.0.0.1:${address.port}/must-not-reach#fragment-secret`,
    'http://127.0.0.1:0/must-not-reach',
    ` http://127.0.0.1:${address.port}/must-not-reach`,
    `http://127.0.0.1:${address.port}/must-not\nreach`,
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
