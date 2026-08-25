import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import http from 'node:http';
import { test } from 'node:test';

import {
  getCameraCapabilities,
  getCameraCapabilitiesWithDependencies,
  parseCapabilitiesResponse,
  parseMedia1ProfilesResponse,
  parseMedia2OptionsResponse,
  parseMedia2ProfilesResponse,
  parsePtzNodesResponse,
  parsePtzServiceCapabilitiesResponse,
  parseScopesResponse,
  parseServicesResponse,
  selectService,
  type CameraCapabilityDependencies,
  type CameraCapabilityOptions,
  type CameraCapabilityReport,
} from './capabilities.ts';

const SOAP_NS = 'http://www.w3.org/2003/05/soap-envelope';
const SOAP11_NS = 'http://schemas.xmlsoap.org/soap/envelope/';
const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';
const MEDIA1_NS = 'http://www.onvif.org/ver10/media/wsdl';
const MEDIA2_NS = 'http://www.onvif.org/ver20/media/wsdl';
const PTZ_NS = 'http://www.onvif.org/ver20/ptz/wsdl';
const EVENTS_NS = 'http://www.onvif.org/ver10/events/wsdl';
const NBSP = '\u00a0';

const GET_SCOPES = `<GetScopes xmlns="${DEV_NS}"/>`;
const GET_SERVICES = `<GetServices xmlns="${DEV_NS}"><IncludeCapability>true</IncludeCapability></GetServices>`;
const GET_ALL_CAPABILITIES = `<GetCapabilities xmlns="${DEV_NS}"><Category>All</Category></GetCapabilities>`;
const MEDIA1_GET_PROFILES = `<GetProfiles xmlns="${MEDIA1_NS}"/>`;
const MEDIA2_GET_PROFILES = `<GetProfiles xmlns="${MEDIA2_NS}"><Type>All</Type></GetProfiles>`;
const MEDIA2_GET_OPTIONS = `<GetVideoEncoderConfigurationOptions xmlns="${MEDIA2_NS}"/>`;
const PTZ_GET_CAPABILITIES = `<GetServiceCapabilities xmlns="${PTZ_NS}"/>`;
const PTZ_GET_NODES = `<GetNodes xmlns="${PTZ_NS}"/>`;

function soap(body: string): string {
  return (
    `<s:Envelope xmlns:s="${SOAP_NS}" xmlns:tds="${DEV_NS}"`
    + ` xmlns:tt="${SCHEMA_NS}" xmlns:trt="${MEDIA1_NS}"`
    + ` xmlns:tr2="${MEDIA2_NS}" xmlns:tptz="${PTZ_NS}"`
    + ` xmlns:vendor="urn:vendor">`
    + `<s:Body>${body}</s:Body></s:Envelope>`
  );
}

function soap11(body: string): string {
  return (
    `<env:Envelope xmlns:env="${SOAP11_NS}" xmlns:ter="http://www.onvif.org/ver10/error">`
    + `<env:Body>${body}</env:Body></env:Envelope>`
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

test('invalidates all of GetServices when any direct Service lacks Namespace or XAddr', () => {
  const valid = service(MEDIA1_NS, 'http://camera/media');
  for (const malformed of [
    '<tds:Service><tds:XAddr>http://camera/missing-namespace</tds:XAddr></tds:Service>',
    '<tds:Service><tds:Namespace>urn:missing-xaddr</tds:Namespace></tds:Service>',
    '<tds:Service><tds:Namespace></tds:Namespace><tds:XAddr>http://camera/empty</tds:XAddr></tds:Service>',
    '<tds:Service><tds:Namespace>urn:empty</tds:Namespace><tds:XAddr></tds:XAddr></tds:Service>',
  ]) {
    assert.throws(
      () => parseServicesResponse(soap(
        `<tds:GetServicesResponse>${valid}${malformed}</tds:GetServicesResponse>`,
      )),
      { name: 'OnvifResponseError', message: 'invalid GetServices response' },
    );
  }
});

test('accepts signed xs:int service versions but omits negative and out-of-range values', () => {
  const parsed = parseServicesResponse(soap(`
    <tds:GetServicesResponse>
      <tds:Service><tds:Namespace>urn:plus</tds:Namespace><tds:XAddr>http://camera/plus</tds:XAddr>
        <tds:Version><tt:Major>+1</tt:Major><tt:Minor>+2</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service><tds:Namespace>urn:negative</tds:Namespace><tds:XAddr>http://camera/negative</tds:XAddr>
        <tds:Version><tt:Major>-1</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service><tds:Namespace>urn:overflow</tds:Namespace><tds:XAddr>http://camera/overflow</tds:XAddr>
        <tds:Version><tt:Major>2147483648</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service><tds:Namespace>urn:underflow</tds:Namespace><tds:XAddr>http://camera/underflow</tds:XAddr>
        <tds:Version><tt:Major>-2147483649</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
    </tds:GetServicesResponse>
  `));

  assert.deepEqual(parsed.services, [
    { namespace: 'urn:negative', xaddr: 'http://camera/negative' },
    { namespace: 'urn:overflow', xaddr: 'http://camera/overflow' },
    { namespace: 'urn:plus', xaddr: 'http://camera/plus', version: { major: 1, minor: 2 } },
    { namespace: 'urn:underflow', xaddr: 'http://camera/underflow' },
  ]);
});

test('accepts only XML whitespace around integer version elements', () => {
  const parsed = parseServicesResponse(soap(`
    <tds:GetServicesResponse>
      <tds:Service><tds:Namespace>urn:nbsp</tds:Namespace><tds:XAddr>http://camera/nbsp</tds:XAddr>
        <tds:Version><tt:Major>${NBSP}+1${NBSP}</tt:Major><tt:Minor>2</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service><tds:Namespace>urn:xml-space</tds:Namespace><tds:XAddr>http://camera/xml-space</tds:XAddr>
        <tds:Version><tt:Major> \t+1\r\n</tt:Major><tt:Minor>\n+2\t</tt:Minor></tds:Version>
      </tds:Service>
    </tds:GetServicesResponse>
  `));

  assert.deepEqual(parsed.services, [
    { namespace: 'urn:nbsp', xaddr: 'http://camera/nbsp' },
    { namespace: 'urn:xml-space', xaddr: 'http://camera/xml-space', version: { major: 1, minor: 2 } },
  ]);
});

test('maps every legacy GetCapabilities service, including Events', () => {
  const parsed = parseCapabilitiesResponse(soap(`
    <tds:GetCapabilitiesResponse><tds:Capabilities>
      <tt:Device><tt:XAddr>http://camera/device</tt:XAddr></tt:Device>
      <tt:Media><tt:XAddr>http://camera/media</tt:XAddr></tt:Media>
      <tt:PTZ><tt:XAddr>http://camera/ptz</tt:XAddr></tt:PTZ>
      <tt:Events><tt:XAddr>http://camera/events</tt:XAddr></tt:Events>
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

test('uses a fixed SOAP fault allowlist without reflecting unknown camera payload markers', () => {
  const unknownFaults = [
    soap(
      '<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>'
      + '<s:Value>vendor:camera-password-marker</s:Value></s:Subcode></s:Code>'
      + '<s:Reason><s:Text>viewer-marker</s:Text></s:Reason>'
      + '<s:Detail>PasswordDigestABC123</s:Detail></s:Fault>',
    ),
    soap11(
      '<env:Fault><faultcode>vendor:camera-password-marker</faultcode>'
      + '<faultstring>viewer-marker</faultstring>'
      + '<detail>PasswordDigestABC123</detail></env:Fault>',
    ),
  ];

  for (const xml of unknownFaults) {
    assert.throws(
      () => parseServicesResponse(xml),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, 'SOAP Fault: Fault');
        assert.equal((error as Error & { faultCode?: string }).faultCode, 'Fault');
        assert.doesNotMatch(
          error.message,
          /camera-password-marker|viewer-marker|PasswordDigestABC123/i,
        );
        return true;
      },
    );
  }
});

test('canonicalizes only known authentication aliases and SOAP protocol fault codes', () => {
  for (const [reason, expected] of [
    ['not authorized', 'NotAuthorized'],
    ['invalid-security', 'InvalidSecurity'],
    ['failed_authentication', 'FailedAuthentication'],
    ['UNAUTHORIZED', 'Unauthorized'],
  ] as const) {
    assert.throws(
      () => parseServicesResponse(soap(
        '<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>'
        + `<s:Reason><s:Text>${reason}</s:Text></s:Reason></s:Fault>`,
      )),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, `SOAP Fault: ${expected}`);
        assert.equal((error as Error & { faultCode?: string }).faultCode, expected);
        return true;
      },
    );
  }

  for (const [xml, expected] of [
    [soap('<s:Fault><s:Code><s:Value>s:VersionMismatch</s:Value></s:Code></s:Fault>'), 'VersionMismatch'],
    [soap('<s:Fault><s:Code><s:Value>s:MustUnderstand</s:Value></s:Code></s:Fault>'), 'MustUnderstand'],
    [soap('<s:Fault><s:Code><s:Value>s:DataEncodingUnknown</s:Value></s:Code></s:Fault>'), 'DataEncodingUnknown'],
    [soap('<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code></s:Fault>'), 'Sender'],
    [soap('<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code></s:Fault>'), 'Receiver'],
    [soap11('<env:Fault><faultcode>env:Client</faultcode></env:Fault>'), 'Client'],
    [soap11('<env:Fault><faultcode>env:Server</faultcode></env:Fault>'), 'Server'],
    [soap11('<env:Fault><faultcode>ter:ActionNotSupported</faultcode></env:Fault>'), 'ActionNotSupported'],
  ] as const) {
    assert.throws(
      () => parseServicesResponse(xml),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, `SOAP Fault: ${expected}`);
        return true;
      },
    );
  }

  for (const unknownCode of ['UnauthorizedOperation', 'NotAuthorized2', 'foo.NotAuthorized']) {
    assert.throws(
      () => parseServicesResponse(soap(
        '<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>'
        + `<s:Value>${unknownCode}</s:Value></s:Subcode></s:Code></s:Fault>`,
      )),
      (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.equal(error.message, 'SOAP Fault: Fault');
        assert.doesNotMatch(error.message, new RegExp(unknownCode.replace('.', '\\.')));
        return true;
      },
    );
  }
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

test('invalidates a whole Media profile response when any direct profile token is missing or empty', () => {
  for (const [parser, responseName, profilePrefix, operation] of [
    [parseMedia1ProfilesResponse, 'trt:GetProfilesResponse', 'trt', 'Media1 GetProfiles'],
    [parseMedia2ProfilesResponse, 'tr2:GetProfilesResponse', 'tr2', 'Media2 GetProfiles'],
  ] as const) {
    for (const invalidProfile of [
      `<${profilePrefix}:Profiles/>`,
      `<${profilePrefix}:Profiles token=""/>`,
    ]) {
      assert.throws(
        () => parser(soap(
          `<${responseName}><${profilePrefix}:Profiles token="valid"/>`
          + `${invalidProfile}</${responseName}>`,
        )),
        { name: 'OnvifResponseError', message: `invalid ${operation} response` },
      );
    }
  }
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

test('rejects uppercase and mixed-case XML Schema boolean lexical forms', () => {
  assert.deepEqual(parsePtzServiceCapabilitiesResponse(soap(`
    <tptz:GetServiceCapabilitiesResponse>
      <tptz:Capabilities EFlip="TRUE" Reverse="False" MoveStatus="TrUe"/>
    </tptz:GetServiceCapabilitiesResponse>
  `)), {});
});

test('accepts only XML whitespace around boolean attributes', () => {
  assert.deepEqual(parsePtzServiceCapabilitiesResponse(soap(`
    <tptz:GetServiceCapabilitiesResponse>
      <tptz:Capabilities EFlip="${NBSP}true${NBSP}" Reverse=" \tfalse\r\n"
        MoveStatus="\n1\t"/>
    </tptz:GetServiceCapabilitiesResponse>
  `)), {
    reverse: false,
    moveStatus: true,
  });
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

test('rejects a PTZNode without its required token', () => {
  assert.throws(
    () => parsePtzNodesResponse(soap(`
      <tptz:GetNodesResponse><tptz:PTZNode><tt:SupportedPTZSpaces>
        <tt:AbsolutePanTiltPositionSpace/>
      </tt:SupportedPTZSpaces></tptz:PTZNode></tptz:GetNodesResponse>
    `)),
    /invalid PTZ GetNodes response/,
  );
});

test('rejects a PTZNode without its required SupportedPTZSpaces', () => {
  assert.throws(
    () => parsePtzNodesResponse(soap(`
      <tptz:GetNodesResponse><tptz:PTZNode token="missing-spaces"/></tptz:GetNodesResponse>
    `)),
    /invalid PTZ GetNodes response/,
  );
});

test('applies xs:int lexical and range rules before nonnegative PTZ preset semantics', () => {
  const parsed = parsePtzNodesResponse(soap(`
    <tptz:GetNodesResponse>
      <tptz:PTZNode token="plus"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets>+1</tt:MaximumNumberOfPresets></tptz:PTZNode>
      <tptz:PTZNode token="negative"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets>-1</tt:MaximumNumberOfPresets></tptz:PTZNode>
      <tptz:PTZNode token="boundary"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets>2147483647</tt:MaximumNumberOfPresets></tptz:PTZNode>
      <tptz:PTZNode token="overflow"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets>2147483648</tt:MaximumNumberOfPresets></tptz:PTZNode>
      <tptz:PTZNode token="underflow"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets>-2147483649</tt:MaximumNumberOfPresets></tptz:PTZNode>
    </tptz:GetNodesResponse>
  `));

  assert.deepEqual(parsed.nodes.map((node) => [node.token, node.maximumPresets]), [
    ['boundary', 2147483647],
    ['negative', undefined],
    ['overflow', undefined],
    ['plus', 1],
    ['underflow', undefined],
  ]);
});

test('accepts only XML whitespace around typed PTZ element values', () => {
  const parsed = parsePtzNodesResponse(soap(`
    <tptz:GetNodesResponse>
      <tptz:PTZNode token="nbsp"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets>${NBSP}+1${NBSP}</tt:MaximumNumberOfPresets>
        <tt:HomeSupported>${NBSP}true${NBSP}</tt:HomeSupported>
      </tptz:PTZNode>
      <tptz:PTZNode token="xml-space"><tt:SupportedPTZSpaces/>
        <tt:MaximumNumberOfPresets> \t+2\r\n</tt:MaximumNumberOfPresets>
        <tt:HomeSupported>\nfalse\t</tt:HomeSupported>
      </tptz:PTZNode>
    </tptz:GetNodesResponse>
  `));

  assert.deepEqual(parsed.nodes.map((node) => ({
    token: node.token,
    maximumPresets: node.maximumPresets,
    homeSupported: node.homeSupported,
  })), [
    { token: 'nbsp', maximumPresets: undefined, homeSupported: undefined },
    { token: 'xml-space', maximumPresets: 2, homeSupported: false },
  ]);
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
      serviceCall: async (body, endpoint) => {
        calls.push({ body, ...(endpoint ? { endpoint } : {}) });
        return respond(body, endpoint);
      },
    }),
    // Fixture tests are about the ONVIF facts above; default the audioSend
    // probes to a quiet "no answer" so they never add unexpected warnings
    // or network attempts. Tests that care override these explicitly.
    probeOnvifBackchannel: async () => false,
    probeVigiTalk: async () => false,
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

async function assertCanonicalAuthFailure(
  authFailure: { statusCode: number; xml: string },
  canonicalToken: string,
): Promise<void> {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body) => {
    if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
    if (body === GET_SERVICES) return authFailure;
    throw new Error('fallback must not run');
  });

  await assert.rejects(
    getCameraCapabilitiesWithDependencies({ host: 'camera' }, dependencies),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, `SOAP Fault: ${canonicalToken}`);
      assert.doesNotMatch(
        error.message,
        /viewer|url-secret|payload-secret|ActionNotSupported|faultstring|Reason|Detail/i,
      );
      return true;
    },
  );
  assert.deepEqual(calls.map(({ body }) => body), [GET_SCOPES, GET_SERVICES]);
}

/**
 * Builds a report using the same fake-device plumbing as every other test
 * here. None of the ONVIF SOAP calls are stubbed to succeed — every one
 * throws and is absorbed as an ordinary warning by the existing enrichment
 * try/catches — so each test can focus purely on the two audioSend probes
 * it injects, which run only after every other fact-gathering step has
 * already failed or succeeded.
 */
function capabilityReportWithProbes(
  probes: Pick<CameraCapabilityDependencies, 'probeOnvifBackchannel' | 'probeVigiTalk'>,
  options: Partial<CameraCapabilityOptions> = {},
): Promise<CameraCapabilityReport> {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies: CameraCapabilityDependencies = {
    ...fakeCapabilityDependencies(calls, async () => {
      throw new Error('onvif call not stubbed for audioSend fixture');
    }),
    ...probes,
  };
  return getCameraCapabilitiesWithDependencies({ host: 'camera', ...options }, dependencies);
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
    if (body === MEDIA2_GET_PROFILES && endpoint === 'http://camera/media2') {
      return response('<tr2:GetProfilesResponse><tr2:Profiles token="modern"><tr2:Configurations><tr2:PTZ token="ptz-modern"/></tr2:Configurations></tr2:Profiles></tr2:GetProfilesResponse>');
    }
    if (body === MEDIA2_GET_OPTIONS && endpoint === 'http://camera/media2') {
      return response('<tr2:GetVideoEncoderConfigurationOptionsResponse><tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options><tr2:Options><tt:Encoding>H265</tt:Encoding></tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>');
    }
    throw new Error(`unexpected fake operation: ${body} at ${endpoint}`);
  });
  const wrappedDependencies: CameraCapabilityDependencies = {
    ...dependencies,
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
  assert.equal(report.media2.detected, true);
  assert.deepEqual(report.media2.encodings, ['H264', 'H265']);
  assert.equal(report.media2.h265Supported, true);
  assert.deepEqual(report.warnings, []);
});

test('keeps PTZ movement unknown when an advertised service returns an invalid node', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
    if (body === GET_SERVICES) {
      return response(`<tds:GetServicesResponse>${service(MEDIA1_NS, 'http://camera/media1')}`
        + service(PTZ_NS, 'http://camera/ptz')
        + '</tds:GetServicesResponse>');
    }
    if (body === MEDIA1_GET_PROFILES && endpoint === 'http://camera/media1') {
      return response('<trt:GetProfilesResponse/>');
    }
    if (body === PTZ_GET_CAPABILITIES && endpoint === 'http://camera/ptz') {
      return response(
        '<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities/></tptz:GetServiceCapabilitiesResponse>',
      );
    }
    if (body === PTZ_GET_NODES && endpoint === 'http://camera/ptz') {
      return response(
        '<tptz:GetNodesResponse><tptz:PTZNode token="invalid-node"/></tptz:GetNodesResponse>',
      );
    }
    throw new Error(`unexpected fake operation: ${body} at ${endpoint}`);
  });

  const report = await getCameraCapabilitiesWithDependencies(
    { host: 'camera' },
    dependencies,
  );

  assert.equal(report.ptz.detected, true);
  assert.equal(report.ptz.panTiltSupported, null);
  assert.equal(report.ptz.zoomSupported, null);
  assert.deepEqual(report.ptz.nodes, []);
  assert.deepEqual(report.warnings.map(({ operation }) => operation), ['PTZ GetNodes']);
});

test('keeps Media2 unknown after falling back to legacy GetCapabilities All', async () => {
  const getServicesFailures = [
    response(`<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>`
      + '<s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:ActionNotSupported</s:Value>'
      + '</s:Subcode></s:Code></s:Fault>', 500),
    response('<tds:GetServicesResponse/>'),
    response(`<tds:GetServicesResponse>${service(MEDIA1_NS, 'http://camera/ignored-media')}`
      + '<tds:Service><tds:Namespace>urn:missing-xaddr</tds:Namespace></tds:Service>'
      + '</tds:GetServicesResponse>'),
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

test('treats a SOAP 1.1 Client fault with a NotAuthorized faultstring as fatal', async () => {
  await assertCanonicalAuthFailure({
    statusCode: 500,
    xml: soap11(
      '<env:Fault><faultcode>env:Client</faultcode>'
      + '<faultstring>ter:NotAuthorized viewer url-secret payload-secret</faultstring>'
      + '<detail><Value>ter:ActionNotSupported</Value></detail></env:Fault>',
    ),
  }, 'NotAuthorized');
});

test('canonicalizes known SOAP 1.2 authentication reasons with generic fault codes', async () => {
  for (const token of ['InvalidSecurity', 'FailedAuthentication', 'Unauthorized']) {
    await assertCanonicalAuthFailure(response(
      '<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>'
      + `<s:Reason><s:Text>ter:${token} viewer url-secret payload-secret</s:Text></s:Reason>`
      + '<s:Detail><vendor:Value>ter:ActionNotSupported</vendor:Value></s:Detail></s:Fault>',
      500,
    ), token);
  }
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

test('does not infer authentication failure from a generic transport error message', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) {
      throw new Error(
        'getaddrinfo ENOTFOUND unauthorized-camera.example transport-sensitive-marker',
      );
    }
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
    { host: 'camera', user: 'viewer', pass: 'credential-sensitive-marker' },
    dependencies,
  );

  assert.deepEqual(calls.map(({ body }) => body), [
    GET_SCOPES,
    GET_SERVICES,
    MEDIA1_GET_PROFILES,
  ]);
  assert.deepEqual(report.warnings, [{
    operation: 'GetScopes',
    message: 'network request failed (ENOTFOUND)',
  }]);
  assert.doesNotMatch(
    JSON.stringify(report.warnings),
    /unauthorized-camera|transport-sensitive-marker|credential-sensitive-marker|viewer/,
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

test('continues with Media2 when Media1 and optional PTZ enrichment fail', async () => {
  const calls: RecordedCapabilityCall[] = [];
  const dependencies = fakeCapabilityDependencies(calls, async (body, endpoint) => {
    if (body === GET_SCOPES) return response('<tds:GetScopesResponse/>');
    if (body === GET_SERVICES) {
      return response(`<tds:GetServicesResponse>${service(MEDIA1_NS, 'http://camera/media1')}`
        + service(PTZ_NS, 'http://camera/ptz')
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
  assert.equal(report.media2.detected, true);
  assert.deepEqual(report.media2.encodings, ['H264']);
  assert.equal(report.media2.h265Supported, false);
  assert.deepEqual(report.warnings.map(({ operation }) => operation), [
    'Media1 GetProfiles', 'PTZ GetNodes',
  ]);
  assert.doesNotMatch(JSON.stringify(report.warnings), /operator|camera-pass|@camera/i);
});

test('retains cross-host advertised XAddr facts but warns without contacting their server', async () => {
  let attackerConnections = 0;
  let attackerRequests = 0;
  const attacker = http.createServer((_request, response) => {
    attackerRequests++;
    response.end();
  });
  attacker.on('connection', () => {
    attackerConnections++;
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
        `<s:Envelope xmlns:s="${SOAP_NS}"><s:Body>${content}</s:Body></s:Envelope>`;
      response.setHeader('Content-Type', 'application/soap+xml');
      if (body.includes('GetSystemDateAndTime')) {
        response.end(envelope(
          '<GetSystemDateAndTimeResponse><SystemDateAndTime><UTCDateTime>'
          + '<Time><Hour>12</Hour></Time><Date><Year>2026</Year><Month>8</Month><Day>6</Day></Date>'
          + '</UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>',
        ));
      } else if (body.includes('GetDeviceInformation')) {
        response.end(envelope(
          `<tds:GetDeviceInformationResponse xmlns:tds="${DEV_NS}">`
          + '<tds:Manufacturer> Acme &amp; Co </tds:Manufacturer>'
          + '</tds:GetDeviceInformationResponse>',
        ));
      } else if (body.includes('<Category>Media</Category>')) {
        const address = deviceServer.address();
        assert.ok(address && typeof address !== 'string');
        response.end(envelope(
          '<GetCapabilitiesResponse><Capabilities><Media><XAddr>'
          + `http://127.0.0.1:${address.port}/connected/media`
          + '</XAddr></Media></Capabilities></GetCapabilitiesResponse>',
        ));
      } else if (body.includes('<GetScopes ')) {
        response.end(envelope(`<tds:GetScopesResponse xmlns:tds="${DEV_NS}"/>`));
      } else if (body.includes('<GetServices ')) {
        const attackerBase = `http://localhost:${attackerAddress.port}`;
        response.end(envelope(
          `<tds:GetServicesResponse xmlns:tds="${DEV_NS}" xmlns:tt="${SCHEMA_NS}">`
          + service(MEDIA1_NS, `${attackerBase}/media1`)
          + service(PTZ_NS, `${attackerBase}/ptz`)
          + service(EVENTS_NS, `${attackerBase}/events`)
          + service(MEDIA2_NS, `${attackerBase}/media2`)
          + '</tds:GetServicesResponse>',
        ));
      } else {
        response.statusCode = 500;
        response.end(envelope('<s:Fault/>'));
      }
    });
  });
  await new Promise<void>((resolve) => deviceServer.listen(0, '127.0.0.1', resolve));
  const deviceAddress = deviceServer.address();
  assert.ok(deviceAddress && typeof deviceAddress !== 'string');

  try {
    const report = await getCameraCapabilities({
      host: 'camera',
      user: 'viewer',
      pass: 'camera-secret',
      deviceUrls: [`http://127.0.0.1:${deviceAddress.port}/onvif/device_service`],
      // This test is about ONVIF service-discovery safety, not audioSend;
      // disable the probes so the real default dependencies never open a
      // second device connection or dispatch real network I/O to VIGI.
      probeAudioSend: false,
    });

    assert.equal(report.device.manufacturer, 'Acme & Co');
    assert.equal(report.services.length, 4);
    assert.ok(report.services.every(({ xaddr }) =>
      xaddr.startsWith(`http://localhost:${attackerAddress.port}/`)));
    assert.deepEqual(report.warnings.map(({ operation }) => operation), [
      'Media1 GetProfiles',
      'PTZ GetServiceCapabilities',
      'PTZ GetNodes',
      'Media2 GetProfiles',
      'Media2 GetVideoEncoderConfigurationOptions',
    ]);
    assert.ok(report.warnings.every(({ message }) => message === 'request failed'));
    assert.deepEqual({ deviceRequests, attackerConnections, attackerRequests }, {
      deviceRequests: 5,
      attackerConnections: 0,
      attackerRequests: 0,
    });
  } finally {
    await Promise.all([
      new Promise<void>((resolve, reject) =>
        deviceServer.close((error) => (error ? reject(error) : resolve()))),
      new Promise<void>((resolve, reject) =>
        attacker.close((error) => (error ? reject(error) : resolve()))),
    ]);
  }
});

test('capability report matches shared cross-language fixture', async () => {
  const fixtureUrl = new URL('../../rust/tests/fixtures/capability-parity.json', import.meta.url);
  assert.equal(existsSync(fixtureUrl), true, 'shared capability parity fixture is missing');

  type Fixture = {
    operations: Record<string, string>;
    expectedReport: unknown;
  };
  const rawFixture = readFileSync(fixtureUrl, 'utf8');
  const expectedOperations = [
    'GetSystemDateAndTime',
    'GetDeviceInformation',
    'GetCapabilitiesMedia',
    'GetScopes',
    'GetServices',
    'Media1GetProfiles',
    'PtzGetServiceCapabilities',
    'PtzGetNodes',
    'Media2GetProfiles',
    'Media2GetVideoEncoderConfigurationOptions',
  ];
  let fixture: Fixture;
  const operationsSeen: string[] = [];
  const server = http.createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
    });
    request.on('end', () => {
      const path = request.url ?? '';
      const operation = body.includes('GetSystemDateAndTime')
        ? 'GetSystemDateAndTime'
        : body.includes('GetDeviceInformation')
          ? 'GetDeviceInformation'
          : body.includes('GetCapabilities') && body.includes('<Category>Media</Category>')
            ? 'GetCapabilitiesMedia'
            : body.includes('GetScopes')
              ? 'GetScopes'
              : body.includes('GetServices')
                ? 'GetServices'
                : body.includes('GetVideoEncoderConfigurationOptions')
                  ? 'Media2GetVideoEncoderConfigurationOptions'
                  : body.includes('GetNodes')
                    ? 'PtzGetNodes'
                    : body.includes('GetProfiles') && path.endsWith('/media1')
                      ? 'Media1GetProfiles'
                      : body.includes('GetProfiles') && path.endsWith('/media2')
                        ? 'Media2GetProfiles'
                        : body.includes('GetServiceCapabilities') && path.endsWith('/ptz')
                          ? 'PtzGetServiceCapabilities'
                          : undefined;

      response.setHeader('Connection', 'close');
      response.setHeader('Content-Type', 'application/soap+xml');
      if (!operation || !Object.hasOwn(fixture.operations, operation)) {
        response.statusCode = 500;
        response.end(soap('<s:Fault/>'));
        return;
      }
      operationsSeen.push(operation);
      response.end(soap(fixture.operations[operation]));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const baseUrl = `http://127.0.0.1:${address.port}`;
    fixture = JSON.parse(
      rawFixture.replaceAll('{{BASE_URL}}', baseUrl),
    ) as Fixture;
    const report = await getCameraCapabilities({
      host: 'camera',
      user: 'viewer',
      pass: 'camera-secret',
      deviceUrls: [`${baseUrl}/device`],
      timeoutMs: 2_000,
      // audioSend is TypeScript-only so far; the shared fixture (consumed
      // by the Rust parity test too) does not describe it. Disable the
      // probes here and compare the rest of the report exactly.
      probeAudioSend: false,
    });

    assert.deepEqual(operationsSeen, expectedOperations);
    assert.deepEqual(JSON.parse(JSON.stringify(report)), {
      ...(fixture.expectedReport as object),
      audioSend: { detected: null, transport: null, onvifBackchannel: null, vigiTalk: null },
    });
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())));
  }
});

test('reports an ONVIF backchannel as the audio-send transport', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => true,
    probeVigiTalk: async () => { throw new Error('must not be probed'); },
  });
  assert.deepEqual(report.audioSend, {
    detected: true, transport: 'onvif', onvifBackchannel: true, vigiTalk: null,
  });
});

test('falls through to VIGI when no sendonly track is offered', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => false,
    probeVigiTalk: async () => true,
  });
  assert.deepEqual(report.audioSend, {
    detected: true, transport: 'vigi', onvifBackchannel: false, vigiTalk: true,
  });
});

test('reports no audio-send path when neither transport answers', async () => {
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => false,
    probeVigiTalk: async () => false,
  });
  assert.deepEqual(report.audioSend, {
    detected: false, transport: null, onvifBackchannel: false, vigiTalk: false,
  });
});

test('a failed ONVIF probe warns, skips VIGI entirely, and leaves every fact null', async () => {
  // A probe that throws has not proven anything — not even that ONVIF lacks
  // a backchannel. Falling through to VIGI here would fire a credential-
  // bearing doAuth against a password ONVIF never validated (VIGI hardware
  // configures the OpenAPI admin account separately from the ONVIF
  // account), advancing the device's lockout counter. See capabilities.ts.
  const report = await capabilityReportWithProbes({
    probeOnvifBackchannel: async () => { throw new Error('describe blew up'); },
    probeVigiTalk: async () => { throw new Error('must not be probed'); },
  });
  assert.equal(report.audioSend.onvifBackchannel, null);
  assert.equal(report.audioSend.vigiTalk, null);
  assert.equal(report.audioSend.detected, null);
  assert.equal(report.audioSend.transport, null);
  assert.ok(report.warnings.some((w) => w.operation === 'AudioSendProbe'));
});

test('probeAudioSend false leaves every audioSend fact null and runs no probe', async () => {
  let probed = false;
  const report = await capabilityReportWithProbes(
    {
      probeOnvifBackchannel: async () => { probed = true; return true; },
      probeVigiTalk: async () => { probed = true; return true; },
    },
    { probeAudioSend: false },
  );
  assert.equal(probed, false);
  assert.deepEqual(report.audioSend, {
    detected: null, transport: null, onvifBackchannel: null, vigiTalk: null,
  });
});
