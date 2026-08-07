import {
  OnvifDevice,
  type DeviceInfo,
  type OnvifOptions,
  type OnvifRawResponse,
} from './deviceClient.ts';
import {
  attribute,
  childElements,
  firstChild,
  parseXml,
  textOf,
  type XmlElement,
} from './xml.ts';

const SOAP_12_NS = 'http://www.w3.org/2003/05/soap-envelope';
const SOAP_11_NS = 'http://schemas.xmlsoap.org/soap/envelope/';
const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const SCHEMA_NS = 'http://www.onvif.org/ver10/schema';
const MEDIA1_NS = 'http://www.onvif.org/ver10/media/wsdl';
const MEDIA2_NS = 'http://www.onvif.org/ver20/media/wsdl';
const PTZ_NS = 'http://www.onvif.org/ver20/ptz/wsdl';
const EVENTS_NS = 'http://www.onvif.org/ver10/events/wsdl';
const WSTOP_NS = 'http://docs.oasis-open.org/wsn/t-1';
const PROFILE_SCOPE_PREFIX = 'onvif://www.onvif.org/Profile/';
const MAX_EVENT_TOPICS = 1_024;
const MAX_EVENT_TOPIC_PATH_BYTES = 4_096;
const MAX_EVENT_TOPIC_NAMESPACE_BYTES = 2_048;
const MAX_EVENT_TOPIC_RETAINED_BYTES = 256 * 1_024;

export interface CameraCapabilityOptions {
  host: string;
  user?: string;
  pass?: string;
  deviceUrls?: string[];
  timeoutMs?: number;
}

export interface CameraCapabilityService {
  namespace: string;
  xaddr: string;
  version?: { major: number; minor: number };
}

export interface CameraCapabilityProfile {
  token: string;
  name?: string;
  source: 'media1' | 'media2';
  hasAudioEncoder: boolean;
  hasAudioOutput: boolean;
  hasAudioSource: boolean;
  ptzConfigurationToken?: string;
  ptzNodeToken?: string;
}

export interface PtzServiceCapabilities {
  eFlip?: boolean;
  reverse?: boolean;
  getCompatibleConfigurations?: boolean;
  moveStatus?: boolean;
  statusPosition?: boolean;
}

export interface PtzSpaces {
  absolutePanTilt: boolean;
  absoluteZoom: boolean;
  relativePanTilt: boolean;
  relativeZoom: boolean;
  continuousPanTilt: boolean;
  continuousZoom: boolean;
}

export interface PtzNode {
  token: string;
  name?: string;
  spaces: PtzSpaces;
  maximumPresets?: number;
  homeSupported?: boolean;
  auxiliaryCommands: string[];
}

export interface EventServiceCapabilities {
  wsSubscriptionPolicySupport?: boolean;
  wsPullPointSupport?: boolean;
  wsPausableSubscriptionManagerInterfaceSupport?: boolean;
  persistentNotificationStorage?: boolean;
  maxNotificationProducers?: number;
  maxPullPoints?: number;
  eventBrokerProtocols?: string[];
  maxEventBrokers?: number;
}

export interface EventTopic {
  namespace?: string;
  path: string;
}

export interface CameraCapabilityWarning {
  operation: string;
  message: string;
}

export interface CameraCapabilityReport {
  device: DeviceInfo;
  scopes: string[];
  declaredProfiles: string[];
  serviceDiscovery: 'getServices' | 'getCapabilities' | 'unavailable';
  services: CameraCapabilityService[];
  profiles: CameraCapabilityProfile[];
  ptz: {
    detected: boolean | null;
    panTiltSupported: boolean | null;
    zoomSupported: boolean | null;
    profileTokens: string[];
    serviceCapabilities?: PtzServiceCapabilities;
    nodes: PtzNode[];
  };
  events: {
    detected: boolean | null;
    serviceCapabilities?: EventServiceCapabilities;
    topics: EventTopic[];
  };
  media2: {
    detected: boolean | null;
    encodings: string[];
    h265Supported: boolean | null;
  };
  warnings: CameraCapabilityWarning[];
}

/** @internal */
export class OnvifResponseError extends Error {
  constructor(
    readonly kind: 'invalid' | 'fault',
    message: string,
    readonly faultCode?: string,
  ) {
    super(message);
    this.name = 'OnvifResponseError';
  }
}

/** @internal */
export interface ParsedServiceDiscovery {
  services: CameraCapabilityService[];
  eventServiceCapabilities?: EventServiceCapabilities;
}

/** @internal */
export interface ParsedPtzNodes {
  nodes: PtzNode[];
  panTiltSupported: boolean;
  zoomSupported: boolean;
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function normalizeXmlScalarWhitespace(value: string): string {
  return value
    .replace(/[\x20\t\r\n]+/g, ' ')
    .replace(/^ +/, '')
    .replace(/ +$/, '');
}

function strictBoolean(value: string | undefined): boolean | undefined {
  if (value === undefined) return undefined;
  switch (normalizeXmlScalarWhitespace(value)) {
    case 'true':
    case '1':
      return true;
    case 'false':
    case '0':
      return false;
    default:
      return undefined;
  }
}

function strictInteger(value: string | undefined): number | undefined {
  if (value === undefined) return undefined;
  const normalized = normalizeXmlScalarWhitespace(value);
  if (!/^[+-]?\d+$/.test(normalized)) return undefined;
  const parsed = Number(normalized);
  if (parsed < -2_147_483_648 || parsed > 2_147_483_647) return undefined;
  return parsed === 0 ? 0 : parsed;
}

function strictNonNegativeInteger(value: string | undefined): number | undefined {
  const parsed = strictInteger(value);
  return parsed !== undefined && parsed >= 0 ? parsed : undefined;
}

const AUTHENTICATION_FAULT_TOKENS: ReadonlyArray<readonly [string, string]> = [
  ['NotAuthorized', 'notauthorized'],
  ['NotAuthorized', 'not authorized'],
  ['NotAuthorized', 'not_authorized'],
  ['NotAuthorized', 'not-authorized'],
  ['InvalidSecurity', 'invalidsecurity'],
  ['InvalidSecurity', 'invalid security'],
  ['InvalidSecurity', 'invalid_security'],
  ['InvalidSecurity', 'invalid-security'],
  ['FailedAuthentication', 'failedauthentication'],
  ['FailedAuthentication', 'failed authentication'],
  ['FailedAuthentication', 'failed_authentication'],
  ['FailedAuthentication', 'failed-authentication'],
  ['Unauthorized', 'unauthorized'],
] as const;
const AUTHENTICATION_FAULT_CODES = new Set(
  AUTHENTICATION_FAULT_TOKENS.map(([canonical]) => canonical),
);

function canonicalAuthenticationFault(value: string | undefined): string | undefined {
  if (!value) return undefined;
  for (const [canonical, token] of AUTHENTICATION_FAULT_TOKENS) {
    if (new RegExp(`(?:^|[^A-Za-z0-9_.-])${token}(?:$|[^A-Za-z0-9_.-])`, 'i').test(value)) {
      return canonical;
    }
  }
  return undefined;
}

function canonicalProtocolFault(
  value: string | undefined,
  soapNamespace: string,
): string | undefined {
  const local = value?.trim().split(':').at(-1);
  if (!local) return undefined;
  const allowlist = [
    'ActionNotSupported',
    ...(soapNamespace === SOAP_11_NS
      ? ['VersionMismatch', 'MustUnderstand', 'Client', 'Server']
      : ['VersionMismatch', 'MustUnderstand', 'DataEncodingUnknown', 'Sender', 'Receiver']),
  ];
  return allowlist.find((candidate) => candidate.toLowerCase() === local.toLowerCase());
}

function operationResponse(
  xml: string,
  namespace: string,
  responseName: string,
  operation: string,
): XmlElement {
  const root = parseXml(xml);
  const soapNamespace = root.uri === SOAP_11_NS ? SOAP_11_NS : SOAP_12_NS;
  if (root.uri !== soapNamespace || root.local !== 'Envelope') {
    throw new OnvifResponseError('invalid', `invalid ${operation} response`);
  }
  const body = firstChild(root, soapNamespace, 'Body');
  if (!body) throw new OnvifResponseError('invalid', `invalid ${operation} response`);
  const fault = firstChild(body, soapNamespace, 'Fault');
  if (fault) {
    const faultValues: string[] = [];
    let deepestCode: string | undefined;
    if (soapNamespace === SOAP_11_NS) {
      deepestCode = textOf(firstChild(fault, '', 'faultcode'));
      const reason = textOf(firstChild(fault, '', 'faultstring'));
      if (deepestCode) faultValues.push(deepestCode);
      if (reason) faultValues.push(reason);
    } else {
      let code = firstChild(fault, soapNamespace, 'Code');
      while (code) {
        const value = textOf(firstChild(code, soapNamespace, 'Value'));
        if (value) {
          deepestCode = value;
          faultValues.push(value);
        }
        code = firstChild(code, soapNamespace, 'Subcode');
      }
      const reason = firstChild(fault, soapNamespace, 'Reason');
      if (reason) {
        for (const text of childElements(reason, soapNamespace, 'Text')) {
          const value = textOf(text);
          if (value) faultValues.push(value);
        }
      }
    }
    const shortCode = canonicalAuthenticationFault(faultValues.join(' '))
      ?? canonicalProtocolFault(deepestCode, soapNamespace)
      ?? 'Fault';
    throw new OnvifResponseError('fault', `SOAP Fault: ${shortCode}`, shortCode);
  }
  const response = firstChild(body, namespace, responseName);
  if (!response) throw new OnvifResponseError('invalid', `invalid ${operation} response`);
  return response;
}

function serviceComparator(
  left: CameraCapabilityService,
  right: CameraCapabilityService,
): number {
  return compareText(left.namespace, right.namespace)
    || compareText(left.xaddr, right.xaddr)
    || (left.version?.major ?? -1) - (right.version?.major ?? -1)
    || (left.version?.minor ?? -1) - (right.version?.minor ?? -1);
}

function eventCapabilitiesFromAttributes(node: XmlElement): EventServiceCapabilities {
  const booleanAttributes: Array<[string, keyof EventServiceCapabilities]> = [
    ['WSSubscriptionPolicySupport', 'wsSubscriptionPolicySupport'],
    ['WSPullPointSupport', 'wsPullPointSupport'],
    ['WSPausableSubscriptionManagerInterfaceSupport', 'wsPausableSubscriptionManagerInterfaceSupport'],
    ['PersistentNotificationStorage', 'persistentNotificationStorage'],
  ];
  const integerAttributes: Array<[string, keyof EventServiceCapabilities]> = [
    ['MaxNotificationProducers', 'maxNotificationProducers'],
    ['MaxPullPoints', 'maxPullPoints'],
    ['MaxEventBrokers', 'maxEventBrokers'],
  ];
  const result: EventServiceCapabilities = {};
  for (const [attributeName, property] of booleanAttributes) {
    const value = strictBoolean(attribute(node, '', attributeName));
    if (value !== undefined) Object.assign(result, { [property]: value });
  }
  for (const [attributeName, property] of integerAttributes) {
    const value = strictNonNegativeInteger(attribute(node, '', attributeName));
    if (value !== undefined) Object.assign(result, { [property]: value });
  }
  const protocols = attribute(node, '', 'EventBrokerProtocols')
    ?.split(/\s+/)
    .map((value) => value.trim())
    .filter(Boolean);
  if (protocols?.length) {
    result.eventBrokerProtocols = [...new Set(protocols)].sort(compareText);
  }
  return result;
}

function eventCapabilitiesFromChildren(node: XmlElement): EventServiceCapabilities {
  const fields: Array<[string, keyof EventServiceCapabilities]> = [
    ['WSSubscriptionPolicySupport', 'wsSubscriptionPolicySupport'],
    ['WSPullPointSupport', 'wsPullPointSupport'],
    ['WSPausableSubscriptionManagerInterfaceSupport', 'wsPausableSubscriptionManagerInterfaceSupport'],
    ['PersistentNotificationStorage', 'persistentNotificationStorage'],
  ];
  const result: EventServiceCapabilities = {};
  for (const [elementName, property] of fields) {
    const value = strictBoolean(firstChild(node, SCHEMA_NS, elementName)?.text);
    if (value !== undefined) Object.assign(result, { [property]: value });
  }
  return result;
}

/** @internal */
export function parseScopesResponse(xml: string): {
  scopes: string[];
  declaredProfiles: string[];
} {
  const response = operationResponse(xml, DEV_NS, 'GetScopesResponse', 'GetScopes');
  const scopes: string[] = [];
  const seenScopes = new Set<string>();
  for (const scope of childElements(response, DEV_NS, 'Scopes')) {
    const item = textOf(firstChild(scope, SCHEMA_NS, 'ScopeItem'));
    if (item && !seenScopes.has(item)) {
      seenScopes.add(item);
      scopes.push(item);
    }
  }

  const declaredProfiles: string[] = [];
  const seenProfiles = new Set<string>();
  for (const scope of scopes) {
    if (!scope.startsWith(PROFILE_SCOPE_PREFIX)) continue;
    const encoded = scope.slice(PROFILE_SCOPE_PREFIX.length).split(/[/?#]/, 1)[0];
    let decoded: string;
    try {
      decoded = decodeURIComponent(encoded);
    } catch {
      continue;
    }
    const profile = decoded.toLowerCase() === 'streaming' ? 'S' : decoded.toUpperCase();
    if (profile && !seenProfiles.has(profile)) {
      seenProfiles.add(profile);
      declaredProfiles.push(profile);
    }
  }
  return { scopes, declaredProfiles };
}

/** @internal */
export function parseServicesResponse(xml: string): ParsedServiceDiscovery {
  const response = operationResponse(xml, DEV_NS, 'GetServicesResponse', 'GetServices');
  const sourceServices = childElements(response, DEV_NS, 'Service');
  const services: CameraCapabilityService[] = [];
  const eventCapabilityCandidates: Array<{
    service: CameraCapabilityService;
    capabilities: EventServiceCapabilities;
  }> = [];
  for (const source of sourceServices) {
    const namespace = textOf(firstChild(source, DEV_NS, 'Namespace'));
    const xaddr = textOf(firstChild(source, DEV_NS, 'XAddr'));
    if (!namespace || !xaddr) {
      throw new OnvifResponseError('invalid', 'invalid GetServices response');
    }
    const versionNode = firstChild(source, DEV_NS, 'Version');
    const major = strictNonNegativeInteger(
      versionNode ? firstChild(versionNode, SCHEMA_NS, 'Major')?.text : undefined,
    );
    const minor = strictNonNegativeInteger(
      versionNode ? firstChild(versionNode, SCHEMA_NS, 'Minor')?.text : undefined,
    );
    const service: CameraCapabilityService = {
      namespace,
      xaddr,
      ...(major !== undefined && minor !== undefined ? { version: { major, minor } } : {}),
    };
    services.push(service);
    if (namespace === EVENTS_NS) {
      const wrapper = firstChild(source, DEV_NS, 'Capabilities');
      const capabilities = wrapper?.children.find(
        (node) => node.uri === EVENTS_NS && node.local === 'Capabilities',
      );
      if (capabilities) {
        const parsed = eventCapabilitiesFromAttributes(capabilities);
        if (Object.keys(parsed).length > 0) {
          eventCapabilityCandidates.push({ service, capabilities: parsed });
        }
      }
    }
  }
  if (services.length === 0) {
    throw new OnvifResponseError('invalid', 'invalid GetServices response: no services');
  }
  services.sort(serviceComparator);
  const selectedEventService = selectService(services, EVENTS_NS);
  const eventServiceCapabilities = eventCapabilityCandidates.find(
    (candidate) => candidate.service === selectedEventService,
  )?.capabilities;
  return {
    services,
    ...(eventServiceCapabilities && Object.keys(eventServiceCapabilities).length > 0
      ? { eventServiceCapabilities }
      : {}),
  };
}

const LEGACY_SERVICE_NAMESPACES: Readonly<Record<string, string>> = {
  Analytics: 'http://www.onvif.org/ver20/analytics/wsdl',
  Device: DEV_NS,
  Events: EVENTS_NS,
  Imaging: 'http://www.onvif.org/ver20/imaging/wsdl',
  Media: MEDIA1_NS,
  PTZ: PTZ_NS,
  DeviceIO: 'http://www.onvif.org/ver10/deviceIO/wsdl',
  Display: 'http://www.onvif.org/ver10/display/wsdl',
  Recording: 'http://www.onvif.org/ver10/recording/wsdl',
  Search: 'http://www.onvif.org/ver10/search/wsdl',
  Replay: 'http://www.onvif.org/ver10/replay/wsdl',
  Receiver: 'http://www.onvif.org/ver10/receiver/wsdl',
};

/** @internal */
export function parseCapabilitiesResponse(xml: string): ParsedServiceDiscovery {
  const response = operationResponse(
    xml,
    DEV_NS,
    'GetCapabilitiesResponse',
    'GetCapabilities',
  );
  const capabilities = firstChild(response, DEV_NS, 'Capabilities');
  if (!capabilities) {
    throw new OnvifResponseError('invalid', 'invalid GetCapabilities response');
  }
  const categories = capabilities.children.filter((node) => node.uri === SCHEMA_NS);
  const extension = categories.find((node) => node.local === 'Extension');
  const candidates = [
    ...categories.filter((node) => node.local !== 'Extension'),
    ...(extension?.children.filter((node) => node.uri === SCHEMA_NS) ?? []),
  ];
  const services: CameraCapabilityService[] = [];
  const eventCapabilityCandidates: Array<{
    service: CameraCapabilityService;
    capabilities?: EventServiceCapabilities;
  }> = [];
  for (const candidate of candidates) {
    const namespace = LEGACY_SERVICE_NAMESPACES[candidate.local];
    const xaddr = textOf(firstChild(candidate, SCHEMA_NS, 'XAddr'));
    if (!namespace || !xaddr) continue;
    const service = { namespace, xaddr };
    services.push(service);
    if (candidate.local === 'Events') {
      const parsed = eventCapabilitiesFromChildren(candidate);
      eventCapabilityCandidates.push({
        service,
        ...(Object.keys(parsed).length > 0 ? { capabilities: parsed } : {}),
      });
    }
  }
  if (services.length === 0) {
    throw new OnvifResponseError('invalid', 'invalid GetCapabilities response: no services');
  }
  services.sort(serviceComparator);
  const selectedEventService = selectService(services, EVENTS_NS);
  const eventServiceCapabilities = eventCapabilityCandidates.find(
    (candidate) => candidate.service === selectedEventService,
  )?.capabilities;
  return {
    services,
    ...(eventServiceCapabilities ? { eventServiceCapabilities } : {}),
  };
}

/** @internal */
export function selectService(
  services: readonly CameraCapabilityService[],
  namespace: string,
): CameraCapabilityService | undefined {
  return services
    .filter((service) => service.namespace === namespace)
    .sort((left, right) => {
      const major = (right.version?.major ?? -1) - (left.version?.major ?? -1);
      if (major !== 0) return major;
      const minor = (right.version?.minor ?? -1) - (left.version?.minor ?? -1);
      return minor || compareText(left.xaddr, right.xaddr);
    })[0];
}

function parseProfile(
  profile: XmlElement,
  source: 'media1' | 'media2',
): CameraCapabilityProfile | undefined {
  const token = attribute(profile, '', 'token');
  if (!token) return undefined;
  if (source === 'media1') {
    const name = textOf(firstChild(profile, SCHEMA_NS, 'Name'));
    const ptz = firstChild(profile, SCHEMA_NS, 'PTZConfiguration');
    const ptzConfigurationToken = ptz ? attribute(ptz, '', 'token') : undefined;
    const ptzNodeToken = ptz ? textOf(firstChild(ptz, SCHEMA_NS, 'NodeToken')) : undefined;
    return {
      token,
      ...(name ? { name } : {}),
      source,
      hasAudioEncoder: Boolean(firstChild(profile, SCHEMA_NS, 'AudioEncoderConfiguration')),
      hasAudioOutput: Boolean(firstChild(profile, SCHEMA_NS, 'AudioOutputConfiguration')),
      hasAudioSource: Boolean(firstChild(profile, SCHEMA_NS, 'AudioSourceConfiguration')),
      ...(ptzConfigurationToken ? { ptzConfigurationToken } : {}),
      ...(ptzNodeToken ? { ptzNodeToken } : {}),
    };
  }
  const name = textOf(firstChild(profile, MEDIA2_NS, 'Name'));
  const configurations = firstChild(profile, MEDIA2_NS, 'Configurations');
  const ptz = configurations ? firstChild(configurations, MEDIA2_NS, 'PTZ') : undefined;
  const ptzConfigurationToken = ptz ? attribute(ptz, '', 'token') : undefined;
  const ptzNodeToken = ptz ? textOf(firstChild(ptz, SCHEMA_NS, 'NodeToken')) : undefined;
  return {
    token,
    ...(name ? { name } : {}),
    source,
    hasAudioEncoder: Boolean(configurations && firstChild(configurations, MEDIA2_NS, 'AudioEncoder')),
    hasAudioOutput: Boolean(configurations && firstChild(configurations, MEDIA2_NS, 'AudioOutput')),
    hasAudioSource: Boolean(configurations && firstChild(configurations, MEDIA2_NS, 'AudioSource')),
    ...(ptzConfigurationToken ? { ptzConfigurationToken } : {}),
    ...(ptzNodeToken ? { ptzNodeToken } : {}),
  };
}

function profileComparator(
  left: CameraCapabilityProfile,
  right: CameraCapabilityProfile,
): number {
  return compareText(left.token, right.token) || compareText(left.source, right.source);
}

/** @internal */
export function parseMedia1ProfilesResponse(xml: string): CameraCapabilityProfile[] {
  const response = operationResponse(xml, MEDIA1_NS, 'GetProfilesResponse', 'Media1 GetProfiles');
  const profiles: CameraCapabilityProfile[] = [];
  for (const element of childElements(response, MEDIA1_NS, 'Profiles')) {
    const profile = parseProfile(element, 'media1');
    if (!profile) {
      throw new OnvifResponseError('invalid', 'invalid Media1 GetProfiles response');
    }
    profiles.push(profile);
  }
  return profiles.sort(profileComparator);
}

/** @internal */
export function parseMedia2ProfilesResponse(xml: string): CameraCapabilityProfile[] {
  const response = operationResponse(xml, MEDIA2_NS, 'GetProfilesResponse', 'Media2 GetProfiles');
  const profiles: CameraCapabilityProfile[] = [];
  for (const element of childElements(response, MEDIA2_NS, 'Profiles')) {
    const profile = parseProfile(element, 'media2');
    if (!profile) {
      throw new OnvifResponseError('invalid', 'invalid Media2 GetProfiles response');
    }
    profiles.push(profile);
  }
  return profiles.sort(profileComparator);
}

/** @internal */
export function parsePtzServiceCapabilitiesResponse(xml: string): PtzServiceCapabilities {
  const response = operationResponse(
    xml,
    PTZ_NS,
    'GetServiceCapabilitiesResponse',
    'PTZ GetServiceCapabilities',
  );
  const capabilities = firstChild(response, PTZ_NS, 'Capabilities');
  if (!capabilities) {
    throw new OnvifResponseError('invalid', 'invalid PTZ GetServiceCapabilities response');
  }
  const fields: Array<[string, keyof PtzServiceCapabilities]> = [
    ['EFlip', 'eFlip'],
    ['Reverse', 'reverse'],
    ['GetCompatibleConfigurations', 'getCompatibleConfigurations'],
    ['MoveStatus', 'moveStatus'],
    ['StatusPosition', 'statusPosition'],
  ];
  const result: PtzServiceCapabilities = {};
  for (const [attributeName, property] of fields) {
    const value = strictBoolean(attribute(capabilities, '', attributeName));
    if (value !== undefined) Object.assign(result, { [property]: value });
  }
  return result;
}

const PTZ_SPACE_FIELDS: ReadonlyArray<[string, keyof PtzSpaces]> = [
  ['AbsolutePanTiltPositionSpace', 'absolutePanTilt'],
  ['AbsoluteZoomPositionSpace', 'absoluteZoom'],
  ['RelativePanTiltTranslationSpace', 'relativePanTilt'],
  ['RelativeZoomTranslationSpace', 'relativeZoom'],
  ['ContinuousPanTiltVelocitySpace', 'continuousPanTilt'],
  ['ContinuousZoomVelocitySpace', 'continuousZoom'],
];

/** @internal */
export function parsePtzNodesResponse(xml: string): ParsedPtzNodes {
  const response = operationResponse(xml, PTZ_NS, 'GetNodesResponse', 'PTZ GetNodes');
  const nodes: PtzNode[] = [];
  for (const node of childElements(response, PTZ_NS, 'PTZNode')) {
    const token = attribute(node, '', 'token');
    const supported = firstChild(node, SCHEMA_NS, 'SupportedPTZSpaces');
    if (!token || !supported) {
      throw new OnvifResponseError('invalid', 'invalid PTZ GetNodes response');
    }
    const spaces: PtzSpaces = {
      absolutePanTilt: false,
      absoluteZoom: false,
      relativePanTilt: false,
      relativeZoom: false,
      continuousPanTilt: false,
      continuousZoom: false,
    };
    for (const [elementName, property] of PTZ_SPACE_FIELDS) {
      spaces[property] = Boolean(firstChild(supported, SCHEMA_NS, elementName));
    }
    const maximumPresets = strictNonNegativeInteger(
      firstChild(node, SCHEMA_NS, 'MaximumNumberOfPresets')?.text,
    );
    const homeSupported = strictBoolean(
      firstChild(node, SCHEMA_NS, 'HomeSupported')?.text,
    );
    const auxiliaryCommands = [...new Set(
      childElements(node, SCHEMA_NS, 'AuxiliaryCommands')
        .map((entry) => textOf(entry))
        .filter((value): value is string => Boolean(value)),
    )].sort(compareText);
    const name = textOf(firstChild(node, SCHEMA_NS, 'Name'));
    nodes.push({
      token,
      ...(name ? { name } : {}),
      spaces,
      ...(maximumPresets !== undefined ? { maximumPresets } : {}),
      ...(homeSupported !== undefined ? { homeSupported } : {}),
      auxiliaryCommands,
    });
  }
  nodes.sort((left, right) => compareText(left.token, right.token));
  return {
    nodes,
    panTiltSupported: nodes.some((node) =>
      node.spaces.absolutePanTilt
      || node.spaces.relativePanTilt
      || node.spaces.continuousPanTilt),
    zoomSupported: nodes.some((node) =>
      node.spaces.absoluteZoom
      || node.spaces.relativeZoom
      || node.spaces.continuousZoom),
  };
}

/** @internal */
export function parseEventServiceCapabilitiesResponse(xml: string): EventServiceCapabilities {
  const response = operationResponse(
    xml,
    EVENTS_NS,
    'GetServiceCapabilitiesResponse',
    'Events GetServiceCapabilities',
  );
  const capabilities = firstChild(response, EVENTS_NS, 'Capabilities');
  if (!capabilities) {
    throw new OnvifResponseError('invalid', 'invalid Events GetServiceCapabilities response');
  }
  return eventCapabilitiesFromAttributes(capabilities);
}

/** @internal */
export function mergeEventServiceCapabilities(
  legacy: EventServiceCapabilities | undefined,
  current: EventServiceCapabilities | undefined,
): EventServiceCapabilities {
  return { ...legacy, ...current };
}

/** @internal */
export function parseEventPropertiesResponse(xml: string): EventTopic[] {
  const response = operationResponse(
    xml,
    EVENTS_NS,
    'GetEventPropertiesResponse',
    'Events GetEventProperties',
  );
  const topicSet = firstChild(response, WSTOP_NS, 'TopicSet');
  if (!topicSet) {
    throw new OnvifResponseError('invalid', 'invalid Events GetEventProperties response');
  }

  const invalidResponse = () => new OnvifResponseError(
    'invalid',
    'invalid Events GetEventProperties response',
  );
  const retained = new Map<string, Map<string, EventTopic>>();
  let retainedTopicCount = 0;
  let retainedTopicBytes = 0;
  let path = '';
  const pending: Array<{ node: XmlElement; restoreLength?: number }> = [];
  for (let index = topicSet.children.length - 1; index >= 0; index--) {
    pending.push({ node: topicSet.children[index] });
  }
  while (pending.length > 0) {
    const { node, restoreLength } = pending.pop()!;
    if (restoreLength !== undefined) {
      path = path.slice(0, restoreLength);
      continue;
    }
    const previousLength = path.length;
    path += `${path ? '/' : ''}${node.local}`;
    if (strictBoolean(attribute(node, WSTOP_NS, 'topic')) === true) {
      const pathBytes = Buffer.byteLength(path, 'utf8');
      if (pathBytes > MAX_EVENT_TOPIC_PATH_BYTES) throw invalidResponse();
      const namespace = node.uri || undefined;
      const namespaceBytes = Buffer.byteLength(namespace ?? '', 'utf8');
      if (namespaceBytes > MAX_EVENT_TOPIC_NAMESPACE_BYTES) throw invalidResponse();
      const namespaceKey = namespace ?? '';
      const duplicate = retained.get(path)?.has(namespaceKey) ?? false;
      if (!duplicate) {
        if (retainedTopicCount >= MAX_EVENT_TOPICS) throw invalidResponse();
        const topicBytes = pathBytes + namespaceBytes;
        if (retainedTopicBytes + topicBytes > MAX_EVENT_TOPIC_RETAINED_BYTES) {
          throw invalidResponse();
        }
        retainedTopicBytes += topicBytes;
        retainedTopicCount++;
        let namespaces = retained.get(path);
        if (!namespaces) {
          namespaces = new Map();
          retained.set(path, namespaces);
        }
        namespaces.set(namespaceKey, {
          ...(namespace ? { namespace } : {}),
          path,
        });
      }
    }
    pending.push({ node, restoreLength: previousLength });
    for (let index = node.children.length - 1; index >= 0; index--) {
      pending.push({ node: node.children[index] });
    }
  }

  const topics = [...retained.values()].flatMap((namespaces) => [...namespaces.values()]);
  return topics.sort((left, right) =>
    compareText(left.path, right.path)
    || compareText(left.namespace ?? '', right.namespace ?? ''));
}

/** @internal */
export function parseMedia2OptionsResponse(xml: string): string[] {
  const response = operationResponse(
    xml,
    MEDIA2_NS,
    'GetVideoEncoderConfigurationOptionsResponse',
    'Media2 GetVideoEncoderConfigurationOptions',
  );
  const encodings = new Set<string>();
  let hasStandardEncoding = false;
  for (const options of childElements(response, MEDIA2_NS, 'Options')) {
    for (const encoding of childElements(options, SCHEMA_NS, 'Encoding')) {
      const value = textOf(encoding)?.toUpperCase();
      if (value) {
        hasStandardEncoding = true;
        encodings.add(value);
      }
    }
    const attributeValue = attribute(options, '', 'Encoding')?.trim().toUpperCase();
    if (attributeValue) encodings.add(attributeValue);
  }
  if (!hasStandardEncoding) {
    throw new OnvifResponseError(
      'invalid',
      'invalid Media2 GetVideoEncoderConfigurationOptions response',
    );
  }
  return [...encodings].sort(compareText);
}

/** @internal */
export interface CameraCapabilityDevice {
  connect(): Promise<DeviceInfo>;
  connectedMediaUrl(): string;
  readOnlyCall(body: string, endpoint?: string): Promise<OnvifRawResponse>;
}

/** @internal */
export interface CameraCapabilityDependencies {
  createDevice(
    host: string,
    user: string,
    pass: string,
    options: OnvifOptions,
  ): CameraCapabilityDevice;
}

class OnvifHttpError extends Error {
  constructor(readonly statusCode: number) {
    super(`HTTP ${statusCode}`);
    this.name = 'OnvifHttpError';
  }
}

const defaultDependencies: CameraCapabilityDependencies = {
  createDevice: (host, user, pass, options) => new OnvifDevice(host, user, pass, options),
};

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

function parseReadOnlyResponse<T>(
  response: OnvifRawResponse,
  parser: (xml: string) => T,
): T {
  if (response.statusCode === 401 || response.statusCode === 403) {
    throw new OnvifHttpError(response.statusCode);
  }
  try {
    const parsed = parser(response.xml);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw new OnvifHttpError(response.statusCode);
    }
    return parsed;
  } catch (error) {
    if (error instanceof OnvifResponseError && error.kind === 'fault') throw error;
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw new OnvifHttpError(response.statusCode);
    }
    throw error;
  }
}

function isAuthenticationFailure(error: unknown): boolean {
  if (error instanceof OnvifHttpError) {
    return error.statusCode === 401 || error.statusCode === 403;
  }
  return error instanceof OnvifResponseError
    && error.kind === 'fault'
    && error.faultCode !== undefined
    && AUTHENTICATION_FAULT_CODES.has(error.faultCode);
}

function sanitizedWarningMessage(
  error: unknown,
): string {
  if (error instanceof OnvifHttpError) return `HTTP ${error.statusCode}`;
  if (error instanceof OnvifResponseError) return error.message;
  const message = error instanceof Error ? error.message : String(error);
  if (/timeout/i.test(message)) return 'request timeout';
  if (/response body exceeds/i.test(message)) return 'response body exceeds limit';
  if (/header/i.test(message)) return 'response headers exceed limit';
  if (/aborted/i.test(message)) return 'response aborted before completion';
  if (/closed before completion/i.test(message)) return 'response closed before completion';
  const networkCode = /\b(E(?:CONNRESET|CONNREFUSED|HOSTUNREACH|NETUNREACH|NOTFOUND))\b/i
    .exec(message)?.[1];
  return networkCode ? `network request failed (${networkCode.toUpperCase()})` : 'request failed';
}

function profileReportComparator(
  left: CameraCapabilityProfile,
  right: CameraCapabilityProfile,
): number {
  return compareText(left.token, right.token) || compareText(left.source, right.source);
}

export function getCameraCapabilities(
  options: CameraCapabilityOptions,
): Promise<CameraCapabilityReport> {
  return getCameraCapabilitiesWithDependencies(options, defaultDependencies);
}

/** @internal */
export async function getCameraCapabilitiesWithDependencies(
  options: CameraCapabilityOptions,
  dependencies: CameraCapabilityDependencies,
): Promise<CameraCapabilityReport> {
  const user = options.user ?? '';
  const pass = options.pass ?? '';
  const clientOptions: OnvifOptions = {
    ...(options.deviceUrls ? { deviceUrls: options.deviceUrls } : {}),
    ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
  };
  const deviceClient = dependencies.createDevice(options.host, user, pass, clientOptions);
  const device = await deviceClient.connect();
  const warnings: CameraCapabilityWarning[] = [];
  const warn = (operation: string, error: unknown) => {
    if (isAuthenticationFailure(error)) throw error;
    warnings.push({ operation, message: sanitizedWarningMessage(error) });
  };
  const call = async <T>(
    body: string,
    parser: (xml: string) => T,
    endpoint?: string,
  ): Promise<T> => parseReadOnlyResponse(
    await deviceClient.readOnlyCall(body, endpoint),
    parser,
  );

  let scopes: string[] = [];
  let declaredProfiles: string[] = [];
  try {
    ({ scopes, declaredProfiles } = await call(GET_SCOPES, parseScopesResponse));
  } catch (error) {
    warn('GetScopes', error);
  }

  let serviceDiscovery: CameraCapabilityReport['serviceDiscovery'] = 'unavailable';
  let services: CameraCapabilityService[] = [];
  let discoveredEventCapabilities: EventServiceCapabilities | undefined;
  try {
    const parsed = await call(GET_SERVICES, parseServicesResponse);
    serviceDiscovery = 'getServices';
    services = parsed.services;
    discoveredEventCapabilities = parsed.eventServiceCapabilities;
  } catch (getServicesError) {
    warn('GetServices', getServicesError);
    try {
      const parsed = await call(GET_ALL_CAPABILITIES, parseCapabilitiesResponse);
      serviceDiscovery = 'getCapabilities';
      services = parsed.services;
      discoveredEventCapabilities = parsed.eventServiceCapabilities;
    } catch (getCapabilitiesError) {
      warn('GetCapabilities', getCapabilitiesError);
    }
  }

  const profiles: CameraCapabilityProfile[] = [];
  const media1Endpoint = selectService(services, MEDIA1_NS)?.xaddr
    ?? deviceClient.connectedMediaUrl();
  try {
    profiles.push(...await call(
      MEDIA1_GET_PROFILES,
      parseMedia1ProfilesResponse,
      media1Endpoint,
    ));
  } catch (error) {
    warn('Media1 GetProfiles', error);
  }

  const discoveryAvailable = serviceDiscovery !== 'unavailable';
  const ptzService = selectService(services, PTZ_NS);
  const ptzDetected = discoveryAvailable ? Boolean(ptzService) : null;
  let ptzServiceCapabilities: PtzServiceCapabilities | undefined;
  let ptzNodes: PtzNode[] = [];
  let panTiltSupported: boolean | null = null;
  let zoomSupported: boolean | null = null;
  if (ptzService) {
    try {
      const parsed = await call(
        PTZ_GET_CAPABILITIES,
        parsePtzServiceCapabilitiesResponse,
        ptzService.xaddr,
      );
      if (Object.keys(parsed).length > 0) ptzServiceCapabilities = parsed;
    } catch (error) {
      warn('PTZ GetServiceCapabilities', error);
    }
    try {
      const parsed = await call(PTZ_GET_NODES, parsePtzNodesResponse, ptzService.xaddr);
      ptzNodes = parsed.nodes;
      panTiltSupported = parsed.panTiltSupported;
      zoomSupported = parsed.zoomSupported;
    } catch (error) {
      warn('PTZ GetNodes', error);
    }
  }

  const eventService = selectService(services, EVENTS_NS);
  const eventsDetected = discoveryAvailable ? Boolean(eventService) : null;
  let currentEventCapabilities: EventServiceCapabilities | undefined;
  let topics: EventTopic[] = [];
  if (eventService) {
    try {
      currentEventCapabilities = await call(
        EVENTS_GET_CAPABILITIES,
        parseEventServiceCapabilitiesResponse,
        eventService.xaddr,
      );
    } catch (error) {
      warn('Events GetServiceCapabilities', error);
    }
    try {
      topics = await call(
        EVENTS_GET_PROPERTIES,
        parseEventPropertiesResponse,
        eventService.xaddr,
      );
    } catch (error) {
      warn('Events GetEventProperties', error);
    }
  }
  const eventServiceCapabilities = mergeEventServiceCapabilities(
    discoveredEventCapabilities,
    currentEventCapabilities,
  );

  const media2Service = selectService(services, MEDIA2_NS);
  const media2Detected = serviceDiscovery === 'getServices' ? Boolean(media2Service) : null;
  let encodings: string[] = [];
  let h265Supported: boolean | null = null;
  if (media2Service) {
    try {
      profiles.push(...await call(
        MEDIA2_GET_PROFILES,
        parseMedia2ProfilesResponse,
        media2Service.xaddr,
      ));
    } catch (error) {
      warn('Media2 GetProfiles', error);
    }
    try {
      encodings = await call(
        MEDIA2_GET_OPTIONS,
        parseMedia2OptionsResponse,
        media2Service.xaddr,
      );
      h265Supported = encodings.some((encoding) => encoding.toUpperCase() === 'H265');
    } catch (error) {
      warn('Media2 GetVideoEncoderConfigurationOptions', error);
    }
  }

  profiles.sort(profileReportComparator);
  const profileTokens = [...new Set(
    profiles
      .filter((profile) => Boolean(profile.ptzConfigurationToken))
      .map((profile) => profile.token),
  )].sort(compareText);

  return {
    device,
    scopes,
    declaredProfiles,
    serviceDiscovery,
    services,
    profiles,
    ptz: {
      detected: ptzDetected,
      panTiltSupported,
      zoomSupported,
      profileTokens,
      ...(ptzServiceCapabilities ? { serviceCapabilities: ptzServiceCapabilities } : {}),
      nodes: ptzNodes,
    },
    events: {
      detected: eventsDetected,
      ...(Object.keys(eventServiceCapabilities).length > 0
        ? { serviceCapabilities: eventServiceCapabilities }
        : {}),
      topics,
    },
    media2: {
      detected: media2Detected,
      encodings,
      h265Supported,
    },
    warnings,
  };
}
