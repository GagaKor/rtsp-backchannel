import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  formatPtzDuration,
  formatPtzNumber,
  openPtzSession,
  type PtzSessionDependencies,
} from './ptz.ts';
import type { PtzSpaces } from './ptzTypes.ts';

const SOAP_NS = 'http://www.w3.org/2003/05/soap-envelope';
const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';
const MEDIA1_NS = 'http://www.onvif.org/ver10/media/wsdl';
const PTZ_NS = 'http://www.onvif.org/ver20/ptz/wsdl';

const GET_SERVICES = `<GetServices xmlns="${DEV_NS}"/>`;
const GET_NODES = `<GetNodes xmlns="${PTZ_NS}"/>`;
const MEDIA1_GET_PROFILES = `<GetProfiles xmlns="${MEDIA1_NS}"/>`;

test('formats PTZ numbers as fixed six-decimal strings', () => {
  assert.equal(formatPtzNumber(0.5), '0.500000');
  assert.equal(formatPtzNumber(-1), '-1.000000');
  assert.equal(formatPtzNumber(0), '0.000000');
  assert.equal(formatPtzNumber(-0), '0.000000');
  assert.equal(formatPtzNumber(0.1 + 0.2), '0.300000');
  assert.throws(() => formatPtzNumber(Number.NaN), { message: 'PTZ value must be finite' });
  assert.throws(
    () => formatPtzNumber(Number.POSITIVE_INFINITY),
    { message: 'PTZ value must be finite' },
  );
});

test('formats PTZ durations as fixed three-decimal seconds', () => {
  assert.equal(formatPtzDuration(1000), 'PT1.000S');
  assert.equal(formatPtzDuration(1500), 'PT1.500S');
  assert.equal(formatPtzDuration(250), 'PT0.250S');
  assert.throws(
    () => formatPtzDuration(0),
    { message: 'PTZ timeout must be finite and greater than 0' },
  );
  assert.throws(
    () => formatPtzDuration(-1),
    { message: 'PTZ timeout must be finite and greater than 0' },
  );
  assert.throws(
    () => formatPtzDuration(Number.NaN),
    { message: 'PTZ timeout must be finite and greater than 0' },
  );
});

interface RecordedPtzCall {
  body: string;
  endpoint?: string;
}

interface FakePtzOptions {
  profileToken?: string;
  mediaUrl?: string;
  ptzXAddr?: string;
  omitPtzService?: boolean;
  nodesXml?: string;
  profilesXml?: string;
  respond?: (
    body: string,
    endpoint?: string,
  ) => { statusCode: number; xml: string } | undefined;
}

function soap(body: string): string {
  return (
    `<s:Envelope xmlns:s="${SOAP_NS}" xmlns:tds="${DEV_NS}"`
    + ` xmlns:tt="${SCHEMA_NS}" xmlns:trt="${MEDIA1_NS}" xmlns:tptz="${PTZ_NS}">`
    + `<s:Body>${body}</s:Body></s:Envelope>`
  );
}

function response(xml: string, statusCode = 200): { statusCode: number; xml: string } {
  return { statusCode, xml: soap(xml) };
}

function spaceElements(spaces: PtzSpaces): string {
  const fields: Array<[boolean, string]> = [
    [spaces.absolutePanTilt, 'AbsolutePanTiltPositionSpace'],
    [spaces.absoluteZoom, 'AbsoluteZoomPositionSpace'],
    [spaces.relativePanTilt, 'RelativePanTiltTranslationSpace'],
    [spaces.relativeZoom, 'RelativeZoomTranslationSpace'],
    [spaces.continuousPanTilt, 'ContinuousPanTiltVelocitySpace'],
    [spaces.continuousZoom, 'ContinuousZoomVelocitySpace'],
  ];
  return fields.filter(([enabled]) => enabled).map(([, tag]) => `<tt:${tag}/>`).join('');
}

function defaultOperationResponse(body: string): string {
  if (body.startsWith('<ContinuousMove ')) return '<tptz:ContinuousMoveResponse/>';
  if (body.startsWith('<AbsoluteMove ')) return '<tptz:AbsoluteMoveResponse/>';
  if (body.startsWith('<RelativeMove ')) return '<tptz:RelativeMoveResponse/>';
  if (body.startsWith('<Stop ')) return '<tptz:StopResponse/>';
  if (body.startsWith('<GetStatus ')) {
    return '<tptz:GetStatusResponse><tptz:PTZStatus/></tptz:GetStatusResponse>';
  }
  throw new Error(`fake PTZ responder: unexpected request body: ${body}`);
}

function fakePtzDependencies(
  calls: RecordedPtzCall[],
  spaceOverrides: Partial<PtzSpaces> = {},
  options: FakePtzOptions = {},
): PtzSessionDependencies {
  const spaces: PtzSpaces = {
    absolutePanTilt: false,
    absoluteZoom: false,
    relativePanTilt: false,
    relativeZoom: false,
    continuousPanTilt: false,
    continuousZoom: false,
    ...spaceOverrides,
  };
  const ptzXAddr = options.ptzXAddr ?? 'http://camera/ptz';
  const mediaUrl = options.mediaUrl ?? 'http://camera/media';
  const profileToken = options.profileToken ?? 'main';

  return {
    createDevice: () => ({
      connect: async () => ({}),
      connectedMediaUrl: () => mediaUrl,
      serviceCall: async (body: string, endpoint?: string) => {
        calls.push({ body, ...(endpoint !== undefined ? { endpoint } : {}) });
        const custom = options.respond?.(body, endpoint);
        if (custom) return custom;
        if (body === GET_SERVICES) {
          return response(options.omitPtzService
            ? '<tds:GetServicesResponse/>'
            : `<tds:GetServicesResponse><tds:Service><tds:Namespace>${PTZ_NS}</tds:Namespace>`
              + `<tds:XAddr>${ptzXAddr}</tds:XAddr></tds:Service></tds:GetServicesResponse>`);
        }
        if (body === GET_NODES && endpoint === ptzXAddr) {
          return response(options.nodesXml ?? (
            '<tptz:GetNodesResponse><tptz:PTZNode token="node-1"><tt:SupportedPTZSpaces>'
            + spaceElements(spaces)
            + '</tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>'
          ));
        }
        if (body === MEDIA1_GET_PROFILES && endpoint === mediaUrl) {
          return response(options.profilesXml ?? (
            `<trt:GetProfilesResponse><trt:Profiles token="${profileToken}">`
            + '<tt:PTZConfiguration token="ptz-config"/></trt:Profiles></trt:GetProfilesResponse>'
          ));
        }
        return response(defaultOperationResponse(body));
      },
    }),
  };
}

test('rejects an unsupported absolute move without sending a request', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { absolutePanTilt: false }),
  );
  const sentBefore = calls.length;

  await assert.rejects(
    session.absoluteMove({ panTilt: { x: 0.5, y: 0 } }),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, 'PTZ absolute pan/tilt is not supported');
      return true;
    },
  );

  assert.equal(calls.length, sentBefore);
});

test('sends every continuous move with an explicit timeout', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  await session.continuousMove({ panTilt: { x: 0.5, y: -0.25 } });

  const move = calls.at(-1)!.body;
  assert.match(move, /<Timeout>PT1\.000S<\/Timeout>/);
  assert.match(move, /x="0\.500000"/);
  assert.match(move, /y="-0\.250000"/);
});

test('rejects out-of-range values without sending a request', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  const sentBefore = calls.length;
  for (const bad of [1.5, -1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    await assert.rejects(session.continuousMove({ panTilt: { x: bad, y: 0 } }));
  }
  assert.equal(calls.length, sentBefore);
});

test('stops both axes on close and keeps the original error', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  await session.close();
  const stop = calls.at(-1)!.body;
  assert.match(stop, /<PanTilt>true<\/PanTilt>/);
  assert.match(stop, /<Zoom>true<\/Zoom>/);
});

test('close swallows a failing stop and still marks the session closed', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }, {
      respond: (body) => (body.startsWith('<Stop ') ? response('<s:Fault/>', 500) : undefined),
    }),
  );
  await session.close();
  await assert.rejects(session.getStatus(), { message: 'PTZ session is closed' });
});

test('rejects every call after close with a fixed message', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, {
      continuousPanTilt: true,
      absolutePanTilt: true,
      relativePanTilt: true,
    }),
  );
  await session.close();
  const sentBefore = calls.length;

  for (const attempt of [
    () => session.continuousMove({ panTilt: { x: 0, y: 0 } }),
    () => session.absoluteMove({ panTilt: { x: 0, y: 0 } }),
    () => session.relativeMove({ panTilt: { x: 0, y: 0 } }),
    () => session.stop(),
    () => session.getStatus(),
  ]) {
    await assert.rejects(attempt(), { message: 'PTZ session is closed' });
  }
  assert.equal(calls.length, sentBefore);
});

test('rejects a move with neither pan/tilt nor zoom without sending a request', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true, continuousZoom: true }),
  );
  const sentBefore = calls.length;

  await assert.rejects(session.continuousMove({}), { message: 'PTZ move requires pan/tilt or zoom' });
  await assert.rejects(session.absoluteMove({}), { message: 'PTZ move requires pan/tilt or zoom' });
  await assert.rejects(session.relativeMove({}), { message: 'PTZ move requires pan/tilt or zoom' });

  assert.equal(calls.length, sentBefore);
});

test('rejects unsupported continuous and relative zoom the same way as pan/tilt', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { relativePanTilt: true }),
  );
  const sentBefore = calls.length;

  await assert.rejects(
    session.continuousMove({ zoom: 0.5 }),
    { message: 'PTZ continuous zoom is not supported' },
  );
  await assert.rejects(
    session.relativeMove({ panTilt: { x: 0, y: 0 }, zoom: 0.5 }),
    { message: 'PTZ relative zoom is not supported' },
  );
  assert.equal(calls.length, sentBefore);
});

test('fails to open when no PTZ service is advertised', async () => {
  const calls: RecordedPtzCall[] = [];
  await assert.rejects(
    openPtzSession(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, { omitPtzService: true }),
    ),
    { message: 'no ONVIF PTZ service' },
  );
  assert.deepEqual(calls.map((call) => call.body), [GET_SERVICES]);
});

test('fails to open when GetNodes returns no node', async () => {
  const calls: RecordedPtzCall[] = [];
  await assert.rejects(
    openPtzSession(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, { nodesXml: '<tptz:GetNodesResponse/>' }),
    ),
    { message: 'no ONVIF PTZ node' },
  );
});

test('resolves the default profile token from the first profile carrying a PTZConfiguration', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, {}, {
      profilesXml: '<trt:GetProfilesResponse>'
        + '<trt:Profiles token="no-ptz"/>'
        + '<trt:Profiles token="has-ptz"><tt:PTZConfiguration token="cfg"/></trt:Profiles>'
        + '</trt:GetProfilesResponse>',
    }),
  );
  assert.equal(session.profileToken, 'has-ptz');
  assert.deepEqual(calls.map((call) => call.body), [GET_SERVICES, GET_NODES, MEDIA1_GET_PROFILES]);
});

test('fails to open when no media profile carries a PTZConfiguration', async () => {
  const calls: RecordedPtzCall[] = [];
  await assert.rejects(
    openPtzSession(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, {
        profilesXml: '<trt:GetProfilesResponse><trt:Profiles token="no-ptz"/></trt:GetProfilesResponse>',
      }),
    ),
    { message: 'no ONVIF PTZ profile' },
  );
});

test('skips Media1 GetProfiles entirely when an explicit profileToken is given', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', profileToken: 'explicit-token' },
    fakePtzDependencies(calls),
  );
  assert.equal(session.profileToken, 'explicit-token');
  assert.deepEqual(calls.map((call) => call.body), [GET_SERVICES, GET_NODES]);
});

test('builds an absoluteMove body with position, optional speed, and no Timeout', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, { absolutePanTilt: true, absoluteZoom: true }),
  );
  await session.absoluteMove({
    panTilt: { x: -1, y: 1 },
    zoom: 0.25,
    speed: { panTilt: { x: 0.1, y: 0.2 }, zoom: 0.3 },
  });

  const body = calls.at(-1)!.body;
  assert.equal(
    body,
    `<AbsoluteMove xmlns="${PTZ_NS}"><ProfileToken>main</ProfileToken>`
      + `<Position><PanTilt xmlns="${SCHEMA_NS}" x="-1.000000" y="1.000000"/>`
      + `<Zoom xmlns="${SCHEMA_NS}" x="0.250000"/></Position>`
      + `<Speed><PanTilt xmlns="${SCHEMA_NS}" x="0.100000" y="0.200000"/>`
      + `<Zoom xmlns="${SCHEMA_NS}" x="0.300000"/></Speed></AbsoluteMove>`,
  );
  assert.doesNotMatch(body, /Timeout/);
});

test('builds a relativeMove body with translation and omits absent fields', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, { relativeZoom: true }),
  );
  await session.relativeMove({ zoom: -0.5 });

  const body = calls.at(-1)!.body;
  assert.equal(
    body,
    `<RelativeMove xmlns="${PTZ_NS}"><ProfileToken>main</ProfileToken>`
      + `<Translation><Zoom xmlns="${SCHEMA_NS}" x="-0.500000"/></Translation></RelativeMove>`,
  );
  assert.doesNotMatch(body, /PanTilt|Speed|Timeout/);
});

test('rejects an absolute zoom position above 1.0 while a continuous zoom velocity of the same magnitude is in range', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, { absoluteZoom: true, continuousZoom: true }),
  );

  await assert.rejects(session.absoluteMove({ zoom: -0.5 }));
  await session.continuousMove({ zoom: -0.5 });
});

test('sends stop with explicit per-axis booleans and getStatus with only the profile token', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls),
  );
  await session.stop({ panTilt: true, zoom: false });
  assert.equal(
    calls.at(-1)!.body,
    `<Stop xmlns="${PTZ_NS}"><ProfileToken>main</ProfileToken><PanTilt>true</PanTilt><Zoom>false</Zoom></Stop>`,
  );

  await session.getStatus();
  assert.equal(
    calls.at(-1)!.body,
    `<GetStatus xmlns="${PTZ_NS}"><ProfileToken>main</ProfileToken></GetStatus>`,
  );
});

test('parses position, move status, and UTC time from GetStatus', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, {}, {
      respond: (body) => (body.startsWith('<GetStatus ')
        ? response(
          '<tptz:GetStatusResponse><tptz:PTZStatus>'
          + `<tt:Position><tt:PanTilt x="0.25" y="-0.5"/><tt:Zoom x="0.75"/></tt:Position>`
          + '<tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>MOVING</tt:Zoom></tt:MoveStatus>'
          + '<tt:UtcTime>2026-08-10T00:00:00Z</tt:UtcTime>'
          + '</tptz:PTZStatus></tptz:GetStatusResponse>',
        )
        : undefined),
    }),
  );

  const status = await session.getStatus();
  assert.deepEqual(status, {
    panTilt: { x: 0.25, y: -0.5 },
    zoom: 0.75,
    panTiltMoveStatus: 'IDLE',
    zoomMoveStatus: 'MOVING',
    utcTime: '2026-08-10T00:00:00Z',
  });
});

test('never surfaces a camera-supplied GetStatus Error anywhere in PtzStatus', async () => {
  const calls: RecordedPtzCall[] = [];
  const secretError = 'internal-diagnostic-marker-should-not-leak';
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, {}, {
      respond: (body) => (body.startsWith('<GetStatus ')
        ? response(
          '<tptz:GetStatusResponse><tptz:PTZStatus>'
          + `<tt:Error>${secretError}</tt:Error>`
          + '<tt:UtcTime>2026-08-10T00:00:00Z</tt:UtcTime>'
          + '</tptz:PTZStatus></tptz:GetStatusResponse>',
        )
        : undefined),
    }),
  );

  const status = await session.getStatus();
  assert.equal(status.utcTime, '2026-08-10T00:00:00Z');
  assert.doesNotMatch(JSON.stringify(status), new RegExp(secretError));
  assert.ok(!('error' in status));
  assert.ok(!('Error' in status));
});

test('classifies a PTZ SOAP Fault the same way the capability report does', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera' },
    fakePtzDependencies(calls, {}, {
      respond: (body) => (body.startsWith('<GetStatus ')
        ? response(
          '<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>'
          + '<s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:ActionNotSupported</s:Value>'
          + '</s:Subcode></s:Code></s:Fault>',
          500,
        )
        : undefined),
    }),
  );

  await assert.rejects(session.getStatus(), (error: unknown) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, 'SOAP Fault: ActionNotSupported');
    return true;
  });
});

test('exposes the cached PTZ node and resolved profile token on the session', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSession(
    { host: 'camera', profileToken: 'fixed-token' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  assert.equal(session.profileToken, 'fixed-token');
  assert.equal(session.node.token, 'node-1');
  assert.equal(session.node.spaces.continuousPanTilt, true);
  assert.equal(session.node.spaces.absolutePanTilt, false);
});
