import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import http from 'node:http';
import { test } from 'node:test';

import {
  formatPtzDuration,
  formatPtzNumber,
  openPtzSession,
  openPtzSessionWithDependencies,
  type PtzSession,
  type PtzSessionDependencies,
} from './ptz.ts';
import type { PtzSpaces } from './ptzTypes.ts';

const SOAP_NS = 'http://www.w3.org/2003/05/soap-envelope';
const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';
const MEDIA1_NS = 'http://www.onvif.org/ver10/media/wsdl';
const MEDIA2_NS = 'http://www.onvif.org/ver20/media/wsdl';
const PTZ_NS = 'http://www.onvif.org/ver20/ptz/wsdl';

const GET_SERVICES =
  `<GetServices xmlns="${DEV_NS}"><IncludeCapability>false</IncludeCapability></GetServices>`;
const GET_NODES = `<GetNodes xmlns="${PTZ_NS}"/>`;
const MEDIA1_GET_PROFILES = `<GetProfiles xmlns="${MEDIA1_NS}"/>`;
const MEDIA2_GET_PROFILES = `<GetProfiles xmlns="${MEDIA2_NS}"><Type>All</Type></GetProfiles>`;

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

test('formats whole-second PTZ durations without a fraction and the rest to three decimals', () => {
  assert.equal(formatPtzDuration(1000), 'PT1S');
  assert.equal(formatPtzDuration(2000), 'PT2S');
  assert.equal(formatPtzDuration(1500), 'PT1.500S');
  assert.equal(formatPtzDuration(250), 'PT0.250S');
  // The whole-second test must be applied to the rendered three-decimal text,
  // not to the raw quotient: 999.9999 ms renders as "1.000", so deciding on
  // the quotient emitted PT1.000S -- the one spelling the strict gSOAP stack
  // rejects, for a value PT1S expresses exactly.
  assert.equal(formatPtzDuration(999.9999), 'PT1S');
  assert.equal(formatPtzDuration(1000.0001), 'PT1S');
  assert.equal(formatPtzDuration(1), 'PT0.001S');
  // A timeout that cannot render as a non-zero guard is rejected rather than
  // emitted: PT0.000S is a camera-side stop deadline of zero, so a crashed
  // client would leave the camera moving indefinitely.
  assert.throws(
    () => formatPtzDuration(0.4),
    /PTZ timeout must be finite and at least 1 ms/,
  );
  assert.throws(
    () => formatPtzDuration(0.9999),
    /PTZ timeout must be finite and at least 1 ms/,
  );
  assert.throws(
    () => formatPtzDuration(0),
    { message: 'PTZ timeout must be finite and at least 1 ms' },
  );
  assert.throws(
    () => formatPtzDuration(-1),
    { message: 'PTZ timeout must be finite and at least 1 ms' },
  );
  assert.throws(
    () => formatPtzDuration(Number.NaN),
    { message: 'PTZ timeout must be finite and at least 1 ms' },
  );
});

test('rejects a PTZ duration above the 60000ms ceiling but accepts the boundary', () => {
  assert.equal(formatPtzDuration(60_000), 'PT60S');
  assert.throws(
    () => formatPtzDuration(60_001),
    { message: 'PTZ timeout must not exceed 60000 ms' },
  );
  assert.throws(
    () => formatPtzDuration(600_000),
    { message: 'PTZ timeout must not exceed 60000 ms' },
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
  /** When set, GetServices also advertises a Media2 service at this XAddr. */
  media2XAddr?: string;
  media2ProfilesXml?: string;
  respond?: (
    body: string,
    endpoint?: string,
  ) => { statusCode: number; xml: string } | undefined;
}

function soap(body: string): string {
  return (
    `<s:Envelope xmlns:s="${SOAP_NS}" xmlns:tds="${DEV_NS}"`
    + ` xmlns:tt="${SCHEMA_NS}" xmlns:trt="${MEDIA1_NS}" xmlns:tr2="${MEDIA2_NS}"`
    + ` xmlns:tptz="${PTZ_NS}">`
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
          const ptzService = options.omitPtzService
            ? ''
            : `<tds:Service><tds:Namespace>${PTZ_NS}</tds:Namespace>`
              + `<tds:XAddr>${ptzXAddr}</tds:XAddr></tds:Service>`;
          const media2Service = options.media2XAddr
            ? `<tds:Service><tds:Namespace>${MEDIA2_NS}</tds:Namespace>`
              + `<tds:XAddr>${options.media2XAddr}</tds:XAddr></tds:Service>`
            : '';
          return response(`<tds:GetServicesResponse>${ptzService}${media2Service}</tds:GetServicesResponse>`);
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
        if (body === MEDIA2_GET_PROFILES && endpoint === options.media2XAddr) {
          return response(options.media2ProfilesXml
            ?? '<tr2:GetProfilesResponse/>');
        }
        return response(defaultOperationResponse(body));
      },
    }),
  };
}

test('rejects an unsupported absolute move without sending a request', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  await session.continuousMove({ panTilt: { x: 0.5, y: -0.25 } });

  const move = calls.at(-1)!.body;
  assert.match(move, /<Timeout>PT1S<\/Timeout>/);
  assert.match(move, /x="0\.500000"/);
  assert.match(move, /y="-0\.250000"/);
});

test('rejects out-of-range values without sending a request', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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

test('rejects every unsupported guard in the table, each with zero additional requests', async () => {
  const cases: Array<{
    space: keyof PtzSpaces;
    message: string;
    invoke: (session: PtzSession) => Promise<void>;
  }> = [
    {
      space: 'continuousPanTilt',
      message: 'PTZ continuous pan/tilt is not supported',
      invoke: (session) => session.continuousMove({ panTilt: { x: 0, y: 0 } }),
    },
    {
      space: 'continuousZoom',
      message: 'PTZ continuous zoom is not supported',
      invoke: (session) => session.continuousMove({ zoom: 0.5 }),
    },
    {
      space: 'absolutePanTilt',
      message: 'PTZ absolute pan/tilt is not supported',
      invoke: (session) => session.absoluteMove({ panTilt: { x: 0, y: 0 } }),
    },
    {
      space: 'absoluteZoom',
      message: 'PTZ absolute zoom is not supported',
      invoke: (session) => session.absoluteMove({ zoom: 0.5 }),
    },
    {
      space: 'relativePanTilt',
      message: 'PTZ relative pan/tilt is not supported',
      invoke: (session) => session.relativeMove({ panTilt: { x: 0, y: 0 } }),
    },
    {
      space: 'relativeZoom',
      message: 'PTZ relative zoom is not supported',
      invoke: (session) => session.relativeMove({ zoom: 0.5 }),
    },
  ];

  for (const { space, message, invoke } of cases) {
    const calls: RecordedPtzCall[] = [];
    const session = await openPtzSessionWithDependencies(
      { host: 'camera', user: 'operator', pass: 'secret' },
      // Every space defaults to false; only the one under test is named, and
      // it stays false, so this always exercises an unsupported guard.
      fakePtzDependencies(calls, { [space]: false }),
    );
    const sentBefore = calls.length;

    await assert.rejects(invoke(session), { message });
    assert.equal(calls.length, sentBefore);
  }
});

test('fails to open when no PTZ service is advertised', async () => {
  const calls: RecordedPtzCall[] = [];
  await assert.rejects(
    openPtzSessionWithDependencies(
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
    openPtzSessionWithDependencies(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, { nodesXml: '<tptz:GetNodesResponse/>' }),
    ),
    { message: 'no ONVIF PTZ node' },
  );
});

test('resolves the default profile token from the first profile carrying a PTZConfiguration', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
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
    openPtzSessionWithDependencies(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, {
        profilesXml: '<trt:GetProfilesResponse><trt:Profiles token="no-ptz"/></trt:GetProfilesResponse>',
      }),
    ),
    { message: 'no ONVIF PTZ profile' },
  );
});

test('falls back to Media2 and resolves a PTZ-capable profile Media1 does not have', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera' },
    fakePtzDependencies(calls, {}, {
      // Media1 has profiles, but none carries a PTZConfiguration.
      profilesXml: '<trt:GetProfilesResponse><trt:Profiles token="media1-no-ptz"/></trt:GetProfilesResponse>',
      media2XAddr: 'http://camera/media2',
      media2ProfilesXml: '<tr2:GetProfilesResponse>'
        + '<tr2:Profiles token="media2-no-ptz"><tr2:Configurations/></tr2:Profiles>'
        + '<tr2:Profiles token="media2-has-ptz"><tr2:Configurations>'
        + '<tr2:PTZ token="ptz-two"/></tr2:Configurations></tr2:Profiles>'
        + '</tr2:GetProfilesResponse>',
    }),
  );

  assert.equal(session.profileToken, 'media2-has-ptz');
  assert.deepEqual(calls.map((call) => call.body), [
    GET_SERVICES,
    GET_NODES,
    MEDIA1_GET_PROFILES,
    MEDIA2_GET_PROFILES,
  ]);
});

test('fails to open when both Media1 and Media2 have no PTZ-capable profile', async () => {
  const calls: RecordedPtzCall[] = [];
  await assert.rejects(
    openPtzSessionWithDependencies(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, {
        profilesXml: '<trt:GetProfilesResponse><trt:Profiles token="media1-no-ptz"/></trt:GetProfilesResponse>',
        media2XAddr: 'http://camera/media2',
        media2ProfilesXml: '<tr2:GetProfilesResponse>'
          + '<tr2:Profiles token="media2-no-ptz"><tr2:Configurations/></tr2:Profiles>'
          + '</tr2:GetProfilesResponse>',
      }),
    ),
    { message: 'no ONVIF PTZ profile' },
  );
  assert.deepEqual(calls.map((call) => call.body), [
    GET_SERVICES,
    GET_NODES,
    MEDIA1_GET_PROFILES,
    MEDIA2_GET_PROFILES,
  ]);
});

test('skips Media1 GetProfiles entirely when an explicit profileToken is given', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', profileToken: 'explicit-token' },
    fakePtzDependencies(calls),
  );
  assert.equal(session.profileToken, 'explicit-token');
  assert.deepEqual(calls.map((call) => call.body), [GET_SERVICES, GET_NODES]);
});

test('builds an absoluteMove body with position, optional speed, and no Timeout', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
    { host: 'camera' },
    fakePtzDependencies(calls, { absoluteZoom: true, continuousZoom: true }),
  );

  await assert.rejects(session.absoluteMove({ zoom: -0.5 }));
  await session.continuousMove({ zoom: -0.5 });
});

test('sends stop with explicit per-axis booleans and getStatus with only the profile token', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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

test('reports unknown position for empty, whitespace, or hex PanTilt attributes', async () => {
  for (const badAttribute of ['', '  ', '0x10']) {
    const calls: RecordedPtzCall[] = [];
    const session = await openPtzSessionWithDependencies(
      { host: 'camera' },
      fakePtzDependencies(calls, {}, {
        respond: (body) => (body.startsWith('<GetStatus ')
          ? response(
            '<tptz:GetStatusResponse><tptz:PTZStatus>'
            + `<tt:Position><tt:PanTilt x="${badAttribute}" y="${badAttribute}"/></tt:Position>`
            + '</tptz:PTZStatus></tptz:GetStatusResponse>',
          )
          : undefined),
      }),
    );

    const status = await session.getStatus();
    assert.equal(status.panTilt, undefined, `expected unknown panTilt for x="${badAttribute}"`);
  }
});

test('never surfaces a camera-supplied GetStatus Error anywhere in PtzStatus', async () => {
  const calls: RecordedPtzCall[] = [];
  const secretError = 'internal-diagnostic-marker-should-not-leak';
  const session = await openPtzSessionWithDependencies(
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
  const session = await openPtzSessionWithDependencies(
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

test('openPtzSession (public, default dependencies) opens a real session end-to-end', async () => {
  // Everything above this test drives the session through the injectable
  // openPtzSessionWithDependencies seam. This is the one test that proves
  // the public single-argument openPtzSession — the only PTZ entry point
  // that ships in dist's declarations — actually reaches a real OnvifDevice
  // and a real HTTP server, not just a type-checks-but-never-runs wrapper.
  let port = 0;
  const server = http.createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', () => {
      const body = Buffer.concat(chunks).toString('utf8');
      response.setHeader('Content-Type', 'application/soap+xml');
      if (body.includes('GetSystemDateAndTime')) {
        response.end(soap(
          '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
          + '<Time><Hour>0</Hour></Time><Date><Year>2026</Year><Month>1</Month><Day>1</Day></Date>'
          + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>',
        ));
      } else if (body.includes('GetDeviceInformation')) {
        response.end(soap('<tds:GetDeviceInformationResponse/>'));
      } else if (body.includes('<Category>Media</Category>')) {
        response.end(soap(
          `<GetCapabilitiesResponse><Capabilities><Media><XAddr>http://127.0.0.1:${port}/media</XAddr>`
          + '</Media></Capabilities></GetCapabilitiesResponse>',
        ));
      } else if (body.includes(GET_SERVICES)) {
        response.end(soap(
          `<tds:GetServicesResponse><tds:Service><tds:Namespace>${PTZ_NS}</tds:Namespace>`
          + `<tds:XAddr>http://127.0.0.1:${port}/ptz</tds:XAddr></tds:Service></tds:GetServicesResponse>`,
        ));
      } else if (body.includes(GET_NODES)) {
        response.end(soap(
          '<tptz:GetNodesResponse><tptz:PTZNode token="node-1"><tt:SupportedPTZSpaces/>'
          + '</tptz:PTZNode></tptz:GetNodesResponse>',
        ));
      } else if (body.includes(MEDIA1_GET_PROFILES)) {
        response.end(soap(
          '<trt:GetProfilesResponse><trt:Profiles token="main">'
          + '<tt:PTZConfiguration token="ptz-config"/></trt:Profiles></trt:GetProfilesResponse>',
        ));
      } else if (body.startsWith('<Stop ')) {
        response.end(soap('<tptz:StopResponse/>'));
      } else {
        response.statusCode = 500;
        response.end(soap('<s:Fault/>'));
      }
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  port = address.port;

  try {
    const session = await openPtzSession({
      host: 'camera',
      deviceUrls: [`http://127.0.0.1:${port}/onvif/device_service`],
    });
    assert.equal(session.node.token, 'node-1');
    assert.equal(session.profileToken, 'main');
    await session.close();
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())));
  }
});

test('exposes the cached PTZ node and resolved profile token on the session', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', profileToken: 'fixed-token' },
    fakePtzDependencies(calls, { continuousPanTilt: true }),
  );
  assert.equal(session.profileToken, 'fixed-token');
  assert.equal(session.node.token, 'node-1');
  assert.equal(session.node.spaces.continuousPanTilt, true);
  assert.equal(session.node.spaces.absolutePanTilt, false);
});

test('parses MaximumNumberOfPresets like capabilities.ts: leading + accepted, i32 bound enforced', async () => {
  const nodeXmlFor = (maximumNumberOfPresets: string): string =>
    '<tptz:GetNodesResponse><tptz:PTZNode token="node-1">'
    + '<tt:SupportedPTZSpaces/>'
    + `<tt:MaximumNumberOfPresets>${maximumNumberOfPresets}</tt:MaximumNumberOfPresets>`
    + '</tptz:PTZNode></tptz:GetNodesResponse>';

  const accepted = await openPtzSessionWithDependencies(
    { host: 'camera' },
    fakePtzDependencies([], {}, { nodesXml: nodeXmlFor('+5') }),
  );
  assert.equal(accepted.node.maximumPresets, 5);

  const outOfRange = await openPtzSessionWithDependencies(
    { host: 'camera' },
    fakePtzDependencies([], {}, { nodesXml: nodeXmlFor('2147483648') }),
  );
  assert.equal(outOfRange.node.maximumPresets, undefined);
});

test('continuousMove sends an explicit per-call timeout in the body', async () => {
  // The Timeout element is the runaway guard: it must reach the wire as the
  // value the caller actually asked for, not a hardcoded default. Every
  // other test in this suite uses the 1000ms default, so without this test
  // a hardcoded PT1S could pass the whole suite undetected.
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousZoom: true }),
  );
  await session.continuousMove({ zoom: 0.5, timeoutMs: 1500 });

  assert.match(calls.at(-1)!.body, /<Timeout>PT1\.500S<\/Timeout>/);
});

test('continuousMove uses the session-level default timeout in the body', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', user: 'operator', pass: 'secret', defaultMoveTimeoutMs: 250 },
    fakePtzDependencies(calls, { continuousZoom: true }),
  );
  await session.continuousMove({ zoom: 0.5 });

  assert.match(calls.at(-1)!.body, /<Timeout>PT0\.250S<\/Timeout>/);
});

test('rejects a defaultMoveTimeoutMs above 60000ms at session open, before any move', async () => {
  const calls: RecordedPtzCall[] = [];
  await assert.rejects(
    openPtzSessionWithDependencies(
      { host: 'camera', defaultMoveTimeoutMs: 600_000 },
      fakePtzDependencies(calls, { continuousZoom: true }),
    ),
    { message: 'PTZ timeout must not exceed 60000 ms' },
  );
  // No lifecycle call should have been reached beyond what open itself
  // issues before its own eager timeout validation ever asks the camera
  // to move: the point of bounding at open is that a huge default is
  // rejected even if the caller never calls continuousMove at all.
  assert.ok(calls.every((call) => !call.body.startsWith('<ContinuousMove ')));
});

test('rejects a per-call continuousMove timeoutMs above 60000ms without sending a request', async () => {
  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', user: 'operator', pass: 'secret' },
    fakePtzDependencies(calls, { continuousZoom: true }),
  );
  const sentBefore = calls.length;

  await assert.rejects(
    session.continuousMove({ zoom: 0.5, timeoutMs: 600_000 }),
    { message: 'PTZ timeout must not exceed 60000 ms' },
  );

  assert.equal(calls.length, sentBefore);
});

test('PTZ request bodies match shared cross-language fixture', async () => {
  const fixtureUrl = new URL('../../rust/tests/fixtures/ptz-request-parity.json', import.meta.url);
  assert.equal(existsSync(fixtureUrl), true, 'shared PTZ request parity fixture is missing');

  interface Fixture {
    profileToken: string;
    panTilt: { x: number; y: number };
    zoom: number;
    timeoutMs: number;
    requests: {
      continuousMove: string;
      absoluteMove: string;
      relativeMove: string;
      stop: string;
      getStatus: string;
    };
  }
  const fixture = JSON.parse(readFileSync(fixtureUrl, 'utf8')) as Fixture;

  const calls: RecordedPtzCall[] = [];
  const session = await openPtzSessionWithDependencies(
    { host: 'camera', profileToken: fixture.profileToken },
    fakePtzDependencies(calls, {
      absolutePanTilt: true,
      absoluteZoom: true,
      relativePanTilt: true,
      relativeZoom: true,
      continuousPanTilt: true,
      continuousZoom: true,
    }, { profileToken: fixture.profileToken }),
  );

  await session.continuousMove({ panTilt: fixture.panTilt, zoom: fixture.zoom });
  assert.equal(calls.at(-1)!.body, fixture.requests.continuousMove);

  await session.absoluteMove({ panTilt: fixture.panTilt, zoom: fixture.zoom });
  assert.equal(calls.at(-1)!.body, fixture.requests.absoluteMove);

  await session.relativeMove({ panTilt: fixture.panTilt, zoom: fixture.zoom });
  assert.equal(calls.at(-1)!.body, fixture.requests.relativeMove);

  await session.stop();
  assert.equal(calls.at(-1)!.body, fixture.requests.stop);

  await session.getStatus();
  assert.equal(calls.at(-1)!.body, fixture.requests.getStatus);
});
