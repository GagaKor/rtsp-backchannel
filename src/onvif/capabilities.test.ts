import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  getCameraCapabilitiesWithDependencies,
  mergeEventServiceCapabilities,
  parseCapabilitiesResponse,
  parseEventPropertiesResponse,
  parseEventServiceCapabilitiesResponse,
  parseMedia1ProfilesResponse,
  parseMedia2OptionsResponse,
  parseMedia2ProfilesResponse,
  parsePtzNodesResponse,
  parsePtzServiceCapabilitiesResponse,
  parseScopesResponse,
  parseServicesResponse,
  selectService,
  type CameraCapabilityDependencies,
} from './capabilities.ts';

const SOAP_NS = 'http://www.w3.org/2003/05/soap-envelope';
const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';
const MEDIA1_NS = 'http://www.onvif.org/ver10/media/wsdl';
const MEDIA2_NS = 'http://www.onvif.org/ver20/media/wsdl';
const PTZ_NS = 'http://www.onvif.org/ver20/ptz/wsdl';
const EVENTS_NS = 'http://www.onvif.org/ver10/events/wsdl';
const WSTOP_NS = 'http://docs.oasis-open.org/wsn/t-1';
const TOPICS_NS = 'http://www.onvif.org/ver10/topics';

const GET_SCOPES = `<GetScopes xmlns="${DEV_NS}"/>`;
const GET_SERVICES = `<GetServices xmlns="${DEV_NS}"><IncludeCapability>true</IncludeCapability></GetServices>`;
const GET_ALL_CAPABILITIES = `<GetCapabilities xmlns="${DEV_NS}"><Category>All</Category></GetCapabilities>`;
const MEDIA1_GET_PROFILES = `<GetProfiles xmlns="${MEDIA1_NS}"/>`;
const MEDIA2_GET_PROFILES = `<GetProfiles xmlns="${MEDIA2_NS}"><Type>All</Type></GetProfiles>`;
const MEDIA2_GET_OPTIONS = `<GetVideoEncoderConfigurationOptions xmlns="${MEDIA2_NS}"/>`;
const PTZ_GET_CAPABILITIES = `<GetServiceCapabilities xmlns="${PTZ_NS}"/>`;
const PTZ_GET_NODES = `<GetNodes xmlns="${PTZ_NS}"/>`;
const EVENTS_GET_CAPABILITIES = `<GetServiceCapabilities xmlns="${EVENTS_NS}"/>`;
const EVENTS_GET_PROPERTIES = `<GetEventProperties xmlns="${EVENTS_NS}"/>`;

function soap(body: string): string {
  return (
    `<s:Envelope xmlns:s="${SOAP_NS}" xmlns:tds="${DEV_NS}"`
    + ` xmlns:tt="${SCHEMA_NS}" xmlns:trt="${MEDIA1_NS}"`
    + ` xmlns:tr2="${MEDIA2_NS}" xmlns:tptz="${PTZ_NS}"`
    + ` xmlns:tev="${EVENTS_NS}" xmlns:wstop="${WSTOP_NS}"`
    + ` xmlns:tns="${TOPICS_NS}" xmlns:vendor="urn:vendor">`
    + `<s:Body>${body}</s:Body></s:Envelope>`
  );
}

test('parses nested scopes, stable-deduplicates raw values, and normalizes profile names', () => {
  const streaming = 'onvif://www.onvif.org/Profile/Streaming';
  const profileT = 'onvif://www.onvif.org/Profile/%74';
  const vendorProfile = 'onvif://www.onvif.org/Profile/vendor%2Dplus';
  const hardware = 'onvif://www.onvif.org/hardware/Camera%201';
  const parsed = parseScopesResponse(soap(`
    <tds:GetScopesResponse>
      <tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>${streaming}</tt:ScopeItem></tds:Scopes>
      <tds:Scopes><tt:ScopeDef>Configurable</tt:ScopeDef><tt:ScopeItem>${hardware}</tt:ScopeItem></tds:Scopes>
      <tds:Scopes><tt:ScopeItem>${streaming}</tt:ScopeItem></tds:Scopes>
      <tds:Scopes><tt:ScopeItem>${profileT}</tt:ScopeItem></tds:Scopes>
      <tds:Scopes><tt:ScopeItem>${vendorProfile}</tt:ScopeItem></tds:Scopes>
    </tds:GetScopesResponse>
  `));

  assert.deepEqual(parsed, {
    scopes: [streaming, hardware, profileT, vendorProfile],
    declaredProfiles: ['S', 'T', 'VENDOR-PLUS'],
  });
});

test('associates only direct Service children and selects highest versions deterministically', () => {
  const parsed = parseServicesResponse(soap(`
    <tds:GetServicesResponse>
      <tds:Service>
        <tds:Namespace>${MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-z</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>9</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service>
        <tds:Namespace>${MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-b</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>10</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service>
        <tds:Namespace>${MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-a</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>10</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service>
        <tds:Namespace>urn:vendor:service</tds:Namespace><tds:XAddr>http://camera/vendor</tds:XAddr>
        <tds:Version><tt:Major>3</tt:Major><tt:Minor>invalid</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Wrapper><tds:Service>
        <tds:Namespace>urn:fake</tds:Namespace><tds:XAddr>http://camera/fake</tds:XAddr>
      </tds:Service></tds:Wrapper>
    </tds:GetServicesResponse>
  `));

  assert.deepEqual(parsed.services, [
    { namespace: MEDIA1_NS, xaddr: 'http://camera/media-a', version: { major: 2, minor: 10 } },
    { namespace: MEDIA1_NS, xaddr: 'http://camera/media-b', version: { major: 2, minor: 10 } },
    { namespace: MEDIA1_NS, xaddr: 'http://camera/media-z', version: { major: 2, minor: 9 } },
    { namespace: 'urn:vendor:service', xaddr: 'http://camera/vendor' },
  ]);
  assert.deepEqual(selectService(parsed.services, MEDIA1_NS), {
    namespace: MEDIA1_NS,
    xaddr: 'http://camera/media-a',
    version: { major: 2, minor: 10 },
  });
});

test('takes embedded Event capabilities from the selected highest-version service', () => {
  const parsed = parseServicesResponse(soap(`
    <tds:GetServicesResponse>
      <tds:Service><tds:Namespace>${EVENTS_NS}</tds:Namespace><tds:XAddr>http://camera/events-new</tds:XAddr>
        <tds:Capabilities><tev:Capabilities WSPullPointSupport="true"/></tds:Capabilities>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service><tds:Namespace>${EVENTS_NS}</tds:Namespace><tds:XAddr>http://camera/events-old</tds:XAddr>
        <tds:Capabilities><tev:Capabilities WSPullPointSupport="false"/></tds:Capabilities>
        <tds:Version><tt:Major>1</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
    </tds:GetServicesResponse>
  `));

  assert.deepEqual(parsed.eventServiceCapabilities, { wsPullPointSupport: true });
});

test('maps every legacy GetCapabilities service and retains legacy Event fields', () => {
  const parsed = parseCapabilitiesResponse(soap(`
    <tds:GetCapabilitiesResponse><tds:Capabilities>
      <tt:Device><tt:XAddr>http://camera/device</tt:XAddr></tt:Device>
      <tt:Media><tt:XAddr>http://camera/media</tt:XAddr></tt:Media>
      <tt:PTZ><tt:XAddr>http://camera/ptz</tt:XAddr></tt:PTZ>
      <tt:Events><tt:XAddr>http://camera/events</tt:XAddr>
        <tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport>
        <tt:WSPullPointSupport>1</tt:WSPullPointSupport>
        <tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>
      </tt:Events>
      <tt:Imaging><tt:XAddr>http://camera/imaging</tt:XAddr></tt:Imaging>
      <tt:Analytics><tt:XAddr>http://camera/analytics</tt:XAddr></tt:Analytics>
      <tt:Extension>
        <tt:DeviceIO><tt:XAddr>http://camera/deviceio</tt:XAddr></tt:DeviceIO>
        <tt:Recording><tt:XAddr>http://camera/recording</tt:XAddr></tt:Recording>
        <tt:Search><tt:XAddr>http://camera/search</tt:XAddr></tt:Search>
        <tt:Replay><tt:XAddr>http://camera/replay</tt:XAddr></tt:Replay>
        <tt:Receiver><tt:XAddr>http://camera/receiver</tt:XAddr></tt:Receiver>
        <tt:Display><tt:XAddr>http://camera/display</tt:XAddr></tt:Display>
      </tt:Extension>
    </tds:Capabilities></tds:GetCapabilitiesResponse>
  `));

  assert.deepEqual(Object.fromEntries(parsed.services.map((service) => [service.namespace, service.xaddr])), {
    'http://www.onvif.org/ver20/analytics/wsdl': 'http://camera/analytics',
    [DEV_NS]: 'http://camera/device',
    'http://www.onvif.org/ver10/deviceIO/wsdl': 'http://camera/deviceio',
    'http://www.onvif.org/ver10/display/wsdl': 'http://camera/display',
    [EVENTS_NS]: 'http://camera/events',
    'http://www.onvif.org/ver20/imaging/wsdl': 'http://camera/imaging',
    [MEDIA1_NS]: 'http://camera/media',
    [PTZ_NS]: 'http://camera/ptz',
    'http://www.onvif.org/ver10/receiver/wsdl': 'http://camera/receiver',
    'http://www.onvif.org/ver10/recording/wsdl': 'http://camera/recording',
    'http://www.onvif.org/ver10/replay/wsdl': 'http://camera/replay',
    'http://www.onvif.org/ver10/search/wsdl': 'http://camera/search',
  });
  assert.deepEqual(parsed.eventServiceCapabilities, {
    wsSubscriptionPolicySupport: true,
    wsPullPointSupport: true,
    wsPausableSubscriptionManagerInterfaceSupport: false,
  });
});

test('rejects malformed, wrong-operation, empty, and SOAP Fault service responses', () => {
  assert.throws(() => parseServicesResponse('<broken>'), /invalid XML document/);
  assert.throws(
    () => parseServicesResponse(soap('<tds:GetScopesResponse/>')),
    /invalid GetServices response/,
  );
  assert.throws(
    () => parseServicesResponse(soap('<tds:GetServicesResponse/>')),
    /no services/,
  );
  assert.throws(
    () => parseServicesResponse(soap(`
      <s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>
        <s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:ActionNotSupported</s:Value>
      </s:Subcode></s:Code><s:Reason><s:Text>Unsupported request</s:Text></s:Reason></s:Fault>
    `)),
    /ActionNotSupported/,
  );
});

test('parses Media1 profiles with PTZ bindings without changing audio facts', () => {
  assert.deepEqual(parseMedia1ProfilesResponse(soap(`
    <trt:GetProfilesResponse>
      <trt:Profiles token="main&amp;special"><tt:Name>Main</tt:Name>
        <tt:AudioSourceConfiguration/><tt:AudioEncoderConfiguration/><tt:AudioOutputConfiguration/>
        <tt:PTZConfiguration token="ptz-config"><tt:NodeToken>node-main</tt:NodeToken></tt:PTZConfiguration>
      </trt:Profiles>
      <trt:Profiles token="silent"><tt:Name>Silent</tt:Name></trt:Profiles>
      <trt:Profiles token="vendor-only"><vendor:AudioEncoderConfiguration/>
        <vendor:PTZConfiguration token="vendor-ptz"/>
      </trt:Profiles>
      <trt:Wrapper><trt:Profiles token="nested"><tt:AudioEncoderConfiguration/></trt:Profiles></trt:Wrapper>
    </trt:GetProfilesResponse>
  `)), [
    {
      token: 'main&special',
      name: 'Main',
      source: 'media1',
      hasAudioEncoder: true,
      hasAudioOutput: true,
      hasAudioSource: true,
      ptzConfigurationToken: 'ptz-config',
      ptzNodeToken: 'node-main',
    },
    {
      token: 'silent',
      name: 'Silent',
      source: 'media1',
      hasAudioEncoder: false,
      hasAudioOutput: false,
      hasAudioSource: false,
    },
    {
      token: 'vendor-only',
      source: 'media1',
      hasAudioEncoder: false,
      hasAudioOutput: false,
      hasAudioSource: false,
    },
  ]);
});

test('parses Media2 Configurations and PTZ references independently from Media1', () => {
  assert.deepEqual(parseMedia2ProfilesResponse(soap(`
    <tr2:GetProfilesResponse>
      <tr2:Profiles token="main"><tr2:Name>Media2 Main</tr2:Name><tr2:Configurations>
        <tr2:AudioSource/><tr2:AudioEncoder/><tr2:AudioOutput/>
        <tr2:PTZ token="ptz-two"><tt:NodeToken>node-two</tt:NodeToken></tr2:PTZ>
      </tr2:Configurations></tr2:Profiles>
      <tr2:Profiles token="video"><tr2:Configurations/></tr2:Profiles>
      <tr2:Profiles token="vendor-only"><tr2:Configurations>
        <vendor:AudioEncoder/><vendor:PTZ token="vendor-ptz"/>
      </tr2:Configurations></tr2:Profiles>
    </tr2:GetProfilesResponse>
  `)), [
    {
      token: 'main',
      name: 'Media2 Main',
      source: 'media2',
      hasAudioEncoder: true,
      hasAudioOutput: true,
      hasAudioSource: true,
      ptzConfigurationToken: 'ptz-two',
      ptzNodeToken: 'node-two',
    },
    {
      token: 'vendor-only',
      source: 'media2',
      hasAudioEncoder: false,
      hasAudioOutput: false,
      hasAudioSource: false,
    },
    {
      token: 'video',
      source: 'media2',
      hasAudioEncoder: false,
      hasAudioOutput: false,
      hasAudioSource: false,
    },
  ]);
});

test('parses strict PTZ service booleans and omits invalid values', () => {
  assert.deepEqual(parsePtzServiceCapabilitiesResponse(soap(`
    <tptz:GetServiceCapabilitiesResponse>
      <tptz:Capabilities EFlip="true" Reverse="false" GetCompatibleConfigurations="1"
        MoveStatus="0" StatusPosition="yes"/>
    </tptz:GetServiceCapabilitiesResponse>
  `)), {
    eFlip: true,
    reverse: false,
    getCompatibleConfigurations: true,
    moveStatus: false,
  });
  assert.deepEqual(parsePtzServiceCapabilitiesResponse(soap(`
    <tptz:GetServiceCapabilitiesResponse>
      <tptz:Capabilities vendor:EFlip="true" vendor:MoveStatus="1"/>
    </tptz:GetServiceCapabilitiesResponse>
  `)), {});
});

test('keeps PTZ movement spaces distinct across multiple nodes and zoom-only nodes', () => {
  const parsed = parsePtzNodesResponse(soap(`
    <tptz:GetNodesResponse>
      <tptz:PTZNode token="pan"><tt:Name>Pan node</tt:Name><tt:SupportedPTZSpaces>
        <tt:AbsolutePanTiltPositionSpace/><tt:ContinuousPanTiltVelocitySpace/>
      </tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets>
        <tt:HomeSupported>true</tt:HomeSupported><tt:AuxiliaryCommands>LightOn</tt:AuxiliaryCommands>
        <tt:AuxiliaryCommands>LightOff</tt:AuxiliaryCommands>
      </tptz:PTZNode>
      <tptz:PTZNode token="zoom"><tt:SupportedPTZSpaces>
        <tt:RelativeZoomTranslationSpace/><tt:ContinuousZoomVelocitySpace/>
      </tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>2.5</tt:MaximumNumberOfPresets>
        <tt:HomeSupported>0</tt:HomeSupported>
      </tptz:PTZNode>
    </tptz:GetNodesResponse>
  `));

  assert.equal(parsed.panTiltSupported, true);
  assert.equal(parsed.zoomSupported, true);
  assert.deepEqual(parsed.nodes, [
    {
      token: 'pan',
      name: 'Pan node',
      spaces: {
        absolutePanTilt: true,
        absoluteZoom: false,
        relativePanTilt: false,
        relativeZoom: false,
        continuousPanTilt: true,
        continuousZoom: false,
      },
      maximumPresets: 8,
      homeSupported: true,
      auxiliaryCommands: ['LightOff', 'LightOn'],
    },
    {
      token: 'zoom',
      spaces: {
        absolutePanTilt: false,
        absoluteZoom: false,
        relativePanTilt: false,
        relativeZoom: true,
        continuousPanTilt: false,
        continuousZoom: true,
      },
      homeSupported: false,
      auxiliaryCommands: [],
    },
  ]);

  const zoomOnly = parsePtzNodesResponse(soap(`
    <tptz:GetNodesResponse><tptz:PTZNode token="zoom-only"><tt:SupportedPTZSpaces>
      <tt:AbsoluteZoomPositionSpace/>
    </tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>
  `));
  assert.equal(zoomOnly.panTiltSupported, false);
  assert.equal(zoomOnly.zoomSupported, true);
});

test('parses modern Event attributes strictly and merges them over legacy fields', () => {
  const modern = parseEventServiceCapabilitiesResponse(soap(`
    <tev:GetServiceCapabilitiesResponse><tev:Capabilities
      WSSubscriptionPolicySupport="false" WSPausableSubscriptionManagerInterfaceSupport="1"
      PersistentNotificationStorage="true" MaxNotificationProducers="12" MaxPullPoints="invalid"
      EventBrokerProtocols="mqtt mqtts mqtt" MaxEventBrokers="2"/>
    </tev:GetServiceCapabilitiesResponse>
  `));
  assert.deepEqual(modern, {
    wsSubscriptionPolicySupport: false,
    wsPausableSubscriptionManagerInterfaceSupport: true,
    persistentNotificationStorage: true,
    maxNotificationProducers: 12,
    eventBrokerProtocols: ['mqtt', 'mqtts'],
    maxEventBrokers: 2,
  });
  assert.deepEqual(mergeEventServiceCapabilities(
    { wsSubscriptionPolicySupport: true, wsPullPointSupport: true },
    modern,
  ), {
    wsSubscriptionPolicySupport: false,
    wsPullPointSupport: true,
    wsPausableSubscriptionManagerInterfaceSupport: true,
    persistentNotificationStorage: true,
    maxNotificationProducers: 12,
    eventBrokerProtocols: ['mqtt', 'mqtts'],
    maxEventBrokers: 2,
  });
  assert.deepEqual(parseEventServiceCapabilitiesResponse(soap(`
    <tev:GetServiceCapabilitiesResponse>
      <tev:Capabilities vendor:WSPullPointSupport="true" vendor:MaxPullPoints="99"/>
    </tev:GetServiceCapabilitiesResponse>
  `)), {});
});

test('extracts only marked arbitrary-depth WS-Topics nodes with local-name paths', () => {
  assert.deepEqual(parseEventPropertiesResponse(soap(`
    <tev:GetEventPropertiesResponse><wstop:TopicSet>
      <tns:Device wstop:topic="true"><tns:Trigger><vendor:Motion wstop:topic="1">
        <tt:MessageDescription><tt:Source><tt:SimpleItemDescription Name="Token"/></tt:Source>
          <tt:Data><tt:SimpleItemDescription Name="State"/></tt:Data></tt:MessageDescription>
      </vendor:Motion></tns:Trigger></tns:Device>
      <vendor:Deep><vendor:Branch><vendor:Leaf wstop:topic="true"/></vendor:Branch></vendor:Deep>
      <tns:Ignored topic="true"><tns:Child/></tns:Ignored>
    </wstop:TopicSet></tev:GetEventPropertiesResponse>
  `)), [
    { namespace: 'urn:vendor', path: 'Deep/Branch/Leaf' },
    { namespace: TOPICS_NS, path: 'Device' },
    { namespace: 'urn:vendor', path: 'Device/Trigger/Motion' },
  ]);
  assert.throws(
    () => parseEventPropertiesResponse(soap('<tev:GetEventPropertiesResponse/>')),
    /invalid Events GetEventProperties response/,
  );
});

test('walks a deeply nested TopicSet without recursion limits', () => {
  const depth = 12_000;
  const parsed = parseEventPropertiesResponse(soap(
    '<tev:GetEventPropertiesResponse><wstop:TopicSet>'
    + '<tns:L>'.repeat(depth)
    + '<vendor:Leaf wstop:topic="true"/>'
    + '</tns:L>'.repeat(depth)
    + '</wstop:TopicSet></tev:GetEventPropertiesResponse>',
  ));

  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].namespace, 'urn:vendor');
  assert.equal(parsed[0].path.split('/').length, depth + 1);
  assert.match(parsed[0].path, /^L\/L\//);
  assert.match(parsed[0].path, /\/Leaf$/);
});

test('deduplicates repeated Media2 option encodings from children and compatible attributes', () => {
  assert.deepEqual(parseMedia2OptionsResponse(soap(`
    <tr2:GetVideoEncoderConfigurationOptionsResponse>
      <tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options>
      <tr2:Options Encoding="h265"><tt:Encoding>H264</tt:Encoding></tr2:Options>
      <tr2:Options Encoding="VP9"><tt:Encoding>h265</tt:Encoding></tr2:Options>
      <tr2:Options vendor:Encoding="H266"><vendor:Encoding>H267</vendor:Encoding></tr2:Options>
    </tr2:GetVideoEncoderConfigurationOptionsResponse>
  `)), ['H264', 'H265', 'VP9']);
  assert.throws(
    () => parseMedia2OptionsResponse(soap(`
      <tr2:GetVideoEncoderConfigurationOptionsResponse>
        <tr2:Options><vendor:Encoding>H267</vendor:Encoding></tr2:Options>
      </tr2:GetVideoEncoderConfigurationOptionsResponse>
    `)),
    /invalid Media2 GetVideoEncoderConfigurationOptions response/,
  );
});

interface RecordedCapabilityCall {
  body: string;
  endpoint?: string;
}

function fakeCapabilityDependencies(
  calls: RecordedCapabilityCall[],
  respond: (body: string, endpoint?: string) => Promise<{ statusCode: number; xml: string }>,
  options: {
    connect?: () => Promise<{ manufacturer?: string; model?: string; firmware?: string; serial?: string }>;
    mediaUrl?: string;
  } = {},
): CameraCapabilityDependencies {
  return {
    createDevice: () => ({
      connect: options.connect ?? (async () => ({ manufacturer: 'Fixture Camera', model: 'C1' })),
      connectedMediaUrl: () => options.mediaUrl ?? 'http://camera/connected-media',
      readOnlyCall: async (body, endpoint) => {
        calls.push({ body, ...(endpoint ? { endpoint } : {}) });
        return respond(body, endpoint);
      },
    }),
  };
}

function service(namespace: string, xaddr: string, major = 1, minor = 0): string {
  return `<tds:Service><tds:Namespace>${namespace}</tds:Namespace><tds:XAddr>${xaddr}</tds:XAddr>`
    + `<tds:Version><tt:Major>${major}</tt:Major><tt:Minor>${minor}</tt:Minor></tds:Version>`
    + '</tds:Service>';
}

function response(xml: string, statusCode = 200): { statusCode: number; xml: string } {
  return { statusCode, xml: soap(xml) };
}

test('orchestrates exact authenticated bodies and routes advertised services deterministically', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const createCalls: unknown[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) {
      return response(`<tds:GetScopesResponse><tds:Scopes><tt:ScopeItem>`
        + 'onvif://www.onvif.org/Profile/Streaming'
        + '</tt:ScopeItem></tds:Scopes></tds:GetScopesResponse>');
    }
    if (body === GET_SERVICES) {
      return response(`<tds:GetServicesResponse>${service(MEDIA1_NS, 'http://camera/media1', 2, 0)}`
        + service(PTZ_NS, 'http://camera/ptz', 2, 0)
        + service(EVENTS_NS, 'http://camera/events', 2, 0)
        + service(MEDIA2_NS, 'http://camera/media2', 2, 0)
        + '</tds:GetServicesResponse>');
    }
    if (body === MEDIA1_GET_PROFILES && endpoint === 'http://camera/media1') {
      return response(`<trt:GetProfilesResponse><trt:Profiles token="legacy"><tt:Name>Legacy</tt:Name>`
        + '<tt:PTZConfiguration token="ptz-legacy"><tt:NodeToken>node-1</tt:NodeToken></tt:PTZConfiguration>'
        + '</trt:Profiles></trt:GetProfilesResponse>');
    }
    if (body === PTZ_GET_CAPABILITIES && endpoint === 'http://camera/ptz') {
      return response('<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities EFlip="true"/></tptz:GetServiceCapabilitiesResponse>');
    }
    if (body === PTZ_GET_NODES && endpoint === 'http://camera/ptz') {
      return response(`<tptz:GetNodesResponse><tptz:PTZNode token="node-1"><tt:SupportedPTZSpaces>`
        + '<tt:AbsolutePanTiltPositionSpace/><tt:AbsoluteZoomPositionSpace/>'
        + '</tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>');
    }
    if (body === EVENTS_GET_CAPABILITIES && endpoint === 'http://camera/events') {
      return response('<tev:GetServiceCapabilitiesResponse><tev:Capabilities WSPullPointSupport="true"/></tev:GetServiceCapabilitiesResponse>');
    }
    if (body === EVENTS_GET_PROPERTIES && endpoint === 'http://camera/events') {
      return response('<tev:GetEventPropertiesResponse><wstop:TopicSet><tns:Motion wstop:topic="true"/></wstop:TopicSet></tev:GetEventPropertiesResponse>');
    }
    if (body === MEDIA2_GET_PROFILES && endpoint === 'http://camera/media2') {
      return response('<tr2:GetProfilesResponse><tr2:Profiles token="modern"><tr2:Configurations><tr2:PTZ token="ptz-modern"/></tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>');
    }
    if (body === MEDIA2_GET_OPTIONS && endpoint === 'http://camera/media2') {
      return response('<tr2:GetVideoEncoderConfigurationOptionsResponse><tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options><tr2:Options><tt:Encoding>H265</tt:Encoding></tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>');
    }
    throw new Error(`unexpected fake operation: ${body} at ${endpoint}`);
  });
  const wrappedDependencies: CameraCapabilityDependencies = {
    createDevice: (host, user, pass, options) => {
      createCalls.push({ host, user, pass, options });
      return dependencies.createDevice(host, user, pass, options);
    },
  };

  const report = await getCameraCapabilitiesWithDependencies({
    host: 'camera',
    user: 'admin',
    pass: 'password',
    deviceUrls: ['http://camera/device'],
    timeoutMs: 1_250,
  }, wrappedDependencies);

  assert.deepEqual(createCalls, [{
    host: 'camera',
    user: 'admin',
    pass: 'password',
    options: { deviceUrls: ['http://camera/device'], timeoutMs: 1_250 },
  }]);
  assert.deepEqual(calls, [
    { body: GET_SCOPES },
    { body: GET_SERVICES },
    { body: MEDIA1_GET_PROFILES, endpoint: 'http://camera/media1' },
    { body: PTZ_GET_CAPABILITIES, endpoint: 'http://camera/ptz' },
    { body: PTZ_GET_NODES, endpoint: 'http://camera/ptz' },
    { body: EVENTS_GET_CAPABILITIES, endpoint: 'http://camera/events' },
    { body: EVENTS_GET_PROPERTIES, endpoint: 'http://camera/events' },
    { body: MEDIA2_GET_PROFILES, endpoint: 'http://camera/media2' },
    { body: MEDIA2_GET_OPTIONS, endpoint: 'http://camera/media2' },
  ]);
  assert.deepEqual(report.device, { manufacturer: 'Fixture Camera', model: 'C1' });
  assert.deepEqual(report.declaredProfiles, ['S']);
  assert.equal(report.serviceDiscovery, 'getServices');
  assert.deepEqual(report.profiles.map(({ token, source }) => ({ token, source })), [
    { token: 'legacy', source: 'media1' },
    { token: 'modern', source: 'media2' },
  ]);
  assert.deepEqual(report.ptz.profileTokens, ['legacy', 'modern']);
  assert.equal(report.ptz.detected, true);
  assert.equal(report.ptz.panTiltSupported, true);
  assert.equal(report.ptz.zoomSupported, true);
  assert.equal(report.ptz.serviceCapabilities?.eFlip, true);
  assert.equal(report.events.detected, true);
  assert.equal(report.events.serviceCapabilities?.wsPullPointSupport, true);
  assert.deepEqual(report.events.topics, [{ namespace: TOPICS_NS, path: 'Motion' }]);
  assert.equal(report.media2.detected, true);
  assert.deepEqual(report.media2.encodings, ['H264', 'H265']);
  assert.equal(report.media2.h265Supported, true);
  assert.deepEqual(report.warnings, []);
});

test('keeps Media2 unknown after falling back to legacy GetCapabilities All', async () => {
  const getServicesFailures = [
    response(`<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>`
      + '<s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:ActionNotSupported</s:Value>'
      + '</s:Subcode></s:Code></s:Fault>', 500),
    response('<tds:GetServicesResponse/>'),
  ];

  for (const getServicesFailure of getServicesFailures) {
    const calls: RecordedCapabilityCall[] = [];
    const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
      if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
      if (body === GET_SERVICES) return getServicesFailure;
      if (body === GET_ALL_CAPABILITIES) {
        return response('<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>'
          + '<tt:XAddr>http://camera/legacy-media</tt:XAddr></tt:Media>'
          + '</tds:Capabilities></tds:GetCapabilitiesResponse>');
      }
      if (body === MEDIA1_GET_PROFILES && endpoint === 'http://camera/legacy-media') {
        return response('<trt:GetProfilesResponse/>');
      }
      throw new Error(`unexpected fake operation: ${body}`);
    });

    const report = await getCameraCapabilitiesWithDependencies(
      { host: 'camera' },
      dependencies,
    );

    assert.equal(report.serviceDiscovery, 'getCapabilities');
    assert.equal(report.warnings[0]?.operation, 'GetServices');
    assert.deepEqual(calls.map(({ body }) => body), [
      GET_SCOPES,
      GET_SERVICES,
      GET_ALL_CAPABILITIES,
      MEDIA1_GET_PROFILES,
    ]);
    assert.equal(report.ptz.detected, false);
    assert.equal(report.ptz.panTiltSupported, null);
    assert.equal(report.ptz.zoomSupported, null);
    assert.equal(report.events.detected, false);
    assert.equal(report.media2.detected, null);
    assert.equal(report.media2.h265Supported, null);
  }
});

test('establishes Media2 absence after a successful GetServices response', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
    if (body === GET_SERVICES) {
      return response(`<tds:GetServicesResponse>${service(
        MEDIA1_NS,
        'http://camera/media1',
      )}</tds:GetServicesResponse>`);
    }
    if (body === MEDIA1_GET_PROFILES && endpoint === 'http://camera/media1') {
      return response('<trt:GetProfilesResponse/>');
    }
    throw new Error(`unexpected fake operation: ${body} at ${endpoint}`);
  });

  const report = await getCameraCapabilitiesWithDependencies(
    { host: 'camera' },
    dependencies,
  );

  assert.equal(report.serviceDiscovery, 'getServices');
  assert.equal(report.media2.detected, false);
  assert.equal(report.media2.h265Supported, null);
  assert.deepEqual(calls.map(({ body }) => body), [
    GET_SCOPES,
    GET_SERVICES,
    MEDIA1_GET_PROFILES,
  ]);
});

test('does not hide HTTP or SOAP authentication failures behind service fallback', async () => {
  for (const authFailure of [
    response(`<s:Fault><s:Code><s:Subcode><s:Value xmlns:ter="http://www.onvif.org/ver10/error">`
      + 'ter:NotAuthorized</s:Value></s:Subcode></s:Code>'
      + '<s:Detail><vendor:Value>NotAnAuthCode</vendor:Value></s:Detail></s:Fault>', 500),
    { statusCode: 401, xml: '' },
  ]) {
    const calls: RecordedCapabilityCall[] = [];
    const dependencies = fakeCapabilityDependencies(calls, async (body) => {
      if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
      if (body === GET_SERVICES) return authFailure;
      throw new Error('fallback must not run');
    });

    await assert.rejects(
      getCameraCapabilitiesWithDependencies({ host: 'camera' }, dependencies),
      /auth|NotAuthorized|HTTP 401/i,
    );
    assert.deepEqual(calls.map(({ body }) => body), [GET_SCOPES, GET_SERVICES]);
  }

  const connectFailure = fakeCapabilityDependencies([], async () => {
    throw new Error('no request expected');
  }, {
    connect: async () => {
      throw new Error('GetDeviceInformation rejected: ter:InvalidSecurity');
    },
  });
  await assert.rejects(
    getCameraCapabilitiesWithDependencies({ host: 'camera' }, connectFailure),
    /InvalidSecurity/,
  );
});

test('keeps Media1 available when discovery fails and sanitizes optional warnings', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) {
      throw new Error('request http://viewer:top-secret@camera/scopes used password password');
    }
    if (body === GET_SERVICES) return response('<tds:GetServicesResponse/>');
    if (body === GET_ALL_CAPABILITIES) {
      throw new Error(
        'connect http://viewer:top-secret@camera/all failed for admin '
        + '<tds:Password>payload-secret</tds:Password> PasswordDigest digest-token',
      );
    }
    if (body === MEDIA1_GET_PROFILES && endpoint === 'http://camera/connected-media') {
      return response('<trt:GetProfilesResponse><trt:Profiles token="fallback"/></trt:GetProfilesResponse>');
    }
    throw new Error(`unexpected fake operation: ${body}`);
  });

  const report = await getCameraCapabilitiesWithDependencies({
    host: 'camera', user: 'admin', pass: 'password',
  }, dependencies);

  assert.equal(report.serviceDiscovery, 'unavailable');
  assert.deepEqual(report.profiles.map((profile) => profile.token), ['fallback']);
  assert.equal(report.ptz.detected, null);
  assert.equal(report.ptz.panTiltSupported, null);
  assert.equal(report.events.detected, null);
  assert.equal(report.media2.detected, null);
  assert.equal(report.media2.h265Supported, null);
  assert.deepEqual(report.warnings.map(({ operation }) => operation), [
    'GetScopes', 'GetServices', 'GetCapabilities',
  ]);
  const warningText = JSON.stringify(report.warnings);
  assert.doesNotMatch(
    warningText,
    /admin|password|viewer|top-secret|@camera|<tds:|payload-secret|PasswordDigest|digest-token/i,
  );
});

test('continues with Media2 when Media1 and optional PTZ or Event enrichment fail', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
    if (body === GET_SERVICES) {
      return response(`<tds:GetServicesResponse>${service(MEDIA1_NS, 'http://camera/media1')}`
        + service(PTZ_NS, 'http://camera/ptz')
        + service(EVENTS_NS, 'http://camera/events')
        + service(MEDIA2_NS, 'http://camera/media2')
        + '</tds:GetServicesResponse>');
    }
    if (body === MEDIA1_GET_PROFILES) {
      throw new Error('media1 failed at http://operator:camera-pass@camera/media1');
    }
    if (body === PTZ_GET_CAPABILITIES) {
      return response('<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities Reverse="1"/></tptz:GetServiceCapabilitiesResponse>');
    }
    if (body === PTZ_GET_NODES) throw new Error('request timeout');
    if (body === EVENTS_GET_CAPABILITIES) throw new Error('HTTP 500');
    if (body === EVENTS_GET_PROPERTIES) {
      return response('<tev:GetEventPropertiesResponse><wstop:TopicSet/></tev:GetEventPropertiesResponse>');
    }
    if (body === MEDIA2_GET_PROFILES && endpoint === 'http://camera/media2') {
      return response('<tr2:GetProfilesResponse><tr2:Profiles token="media2-only"><tr2:Configurations><tr2:AudioEncoder/></tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>');
    }
    if (body === MEDIA2_GET_OPTIONS && endpoint === 'http://camera/media2') {
      return response('<tr2:GetVideoEncoderConfigurationOptionsResponse><tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>');
    }
    throw new Error(`unexpected fake operation: ${body} at ${endpoint}`);
  });

  const report = await getCameraCapabilitiesWithDependencies(
    { host: 'camera', user: 'operator', pass: 'camera-pass' },
    dependencies,
  );

  assert.deepEqual(report.profiles.map(({ token, source }) => ({ token, source })), [
    { token: 'media2-only', source: 'media2' },
  ]);
  assert.equal(report.ptz.detected, true);
  assert.equal(report.ptz.panTiltSupported, null);
  assert.equal(report.ptz.zoomSupported, null);
  assert.deepEqual(report.ptz.nodes, []);
  assert.equal(report.ptz.serviceCapabilities?.reverse, true);
  assert.equal(report.events.detected, true);
  assert.equal(report.media2.detected, true);
  assert.deepEqual(report.media2.encodings, ['H264']);
  assert.equal(report.media2.h265Supported, false);
  assert.deepEqual(report.warnings.map(({ operation }) => operation), [
    'Media1 GetProfiles', 'PTZ GetNodes', 'Events GetServiceCapabilities',
  ]);
  assert.doesNotMatch(JSON.stringify(report.warnings), /operator|camera-pass|@camera/i);
});
