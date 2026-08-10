/**
 * ONVIF PTZ movement control.
 *
 * A session opens once (connect -> GetServices -> GetNodes -> resolve the
 * profile token), caches the node's supported spaces, and rejects any
 * operation the camera did not advertise before building a request. Control
 * reuses the same authenticated transport the read-only capability report
 * uses; it is a different SOAP body on the existing pipe, not a new one.
 *
 * This module intentionally does not import `./capabilities.ts` (and vice
 * versa) so the two stay independent; shared value types live in
 * `./ptzTypes.ts`.
 */
import {
  encodeXml,
  OnvifDevice,
  type DeviceInfo,
  type OnvifOptions,
} from './deviceClient.ts';
import type { PtzNode, PtzSpaces } from './ptzTypes.ts';
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

const DEFAULT_MOVE_TIMEOUT_MS = 1000;
const PAN_TILT_RANGE: readonly [number, number] = [-1, 1];
/** Every zoom quantity is -1..1 except an absolute zoom *position*, which is 0..1. */
const ZOOM_GENERIC_RANGE: readonly [number, number] = [-1, 1];
const ZOOM_POSITION_RANGE: readonly [number, number] = [0, 1];

export interface PtzVector {
  x: number;
  y: number;
}

export interface PtzSessionOptions {
  host: string;
  user?: string;
  pass?: string;
  /** Default: first media profile with a PTZConfiguration. */
  profileToken?: string;
  deviceUrls?: string[];
  /** Per-request transport timeout. */
  timeoutMs?: number;
  /** ContinuousMove Timeout, default 1000. */
  defaultMoveTimeoutMs?: number;
}

export interface PtzStatus {
  panTilt?: PtzVector;
  zoom?: number;
  panTiltMoveStatus?: string;
  zoomMoveStatus?: string;
  utcTime?: string;
}

interface PtzMoveVector {
  panTilt?: PtzVector;
  zoom?: number;
}

interface PtzContinuousMoveOptions extends PtzMoveVector {
  timeoutMs?: number;
}

interface PtzAbsoluteMoveOptions extends PtzMoveVector {
  speed?: PtzMoveVector;
}

interface PtzRelativeMoveOptions extends PtzMoveVector {
  speed?: PtzMoveVector;
}

interface PtzStopOptions {
  panTilt?: boolean;
  zoom?: boolean;
}

export interface PtzSession {
  readonly profileToken: string;
  readonly node: PtzNode;
  continuousMove(move: PtzContinuousMoveOptions): Promise<void>;
  absoluteMove(move: PtzAbsoluteMoveOptions): Promise<void>;
  relativeMove(move: PtzRelativeMoveOptions): Promise<void>;
  stop(options?: PtzStopOptions): Promise<void>;
  getStatus(): Promise<PtzStatus>;
  close(): Promise<void>;
}

/** @internal */
export class PtzResponseError extends Error {
  constructor(
    readonly kind: 'invalid' | 'fault',
    message: string,
    readonly faultCode?: string,
  ) {
    super(message);
    this.name = 'PtzResponseError';
  }
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

// Mirrors capabilities.ts's fault canonicalization so a PTZ SOAP Fault is
// classified the same way a capability-report SOAP Fault is: a fixed,
// sanitized code, never the raw response text.
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
  let root: XmlElement;
  try {
    root = parseXml(xml);
  } catch {
    throw new PtzResponseError('invalid', `invalid ${operation} response`);
  }
  const soapNamespace = root.uri === SOAP_11_NS ? SOAP_11_NS : SOAP_12_NS;
  if (root.uri !== soapNamespace || root.local !== 'Envelope') {
    throw new PtzResponseError('invalid', `invalid ${operation} response`);
  }
  const body = firstChild(root, soapNamespace, 'Body');
  if (!body) throw new PtzResponseError('invalid', `invalid ${operation} response`);
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
    throw new PtzResponseError('fault', `SOAP Fault: ${shortCode}`, shortCode);
  }
  const response = firstChild(body, namespace, responseName);
  if (!response) throw new PtzResponseError('invalid', `invalid ${operation} response`);
  return response;
}

function requireFiniteInRange(value: number, range: readonly [number, number]): number {
  if (!Number.isFinite(value)) throw new RangeError('PTZ value must be finite');
  if (value < range[0] || value > range[1]) {
    throw new RangeError('PTZ value must be within its valid range');
  }
  return value;
}

function validatedVector(
  vector: PtzVector | undefined,
  range: readonly [number, number],
): PtzVector | undefined {
  if (!vector) return undefined;
  return {
    x: requireFiniteInRange(vector.x, range),
    y: requireFiniteInRange(vector.y, range),
  };
}

function validatedZoom(
  zoom: number | undefined,
  range: readonly [number, number],
): number | undefined {
  return zoom === undefined ? undefined : requireFiniteInRange(zoom, range);
}

/** @internal */
export function formatPtzNumber(value: number): string {
  if (!Number.isFinite(value)) throw new RangeError('PTZ value must be finite');
  const text = value.toFixed(6);
  return text === '-0.000000' ? '0.000000' : text;
}

/** @internal */
export function formatPtzDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) {
    throw new RangeError('PTZ timeout must be finite and greater than 0');
  }
  return `PT${(milliseconds / 1000).toFixed(3)}S`;
}

function vectorXml(tag: string, panTilt?: PtzVector, zoom?: number): string {
  let inner = '';
  if (panTilt) {
    inner += `<PanTilt xmlns="${SCHEMA_NS}" x="${formatPtzNumber(panTilt.x)}"`
      + ` y="${formatPtzNumber(panTilt.y)}"/>`;
  }
  if (zoom !== undefined) {
    inner += `<Zoom xmlns="${SCHEMA_NS}" x="${formatPtzNumber(zoom)}"/>`;
  }
  return `<${tag}>${inner}</${tag}>`;
}

function assertMoveShape(move: PtzMoveVector): void {
  if (move.panTilt === undefined && move.zoom === undefined) {
    throw new Error('PTZ move requires pan/tilt or zoom');
  }
}

const PTZ_SPACE_FIELDS: ReadonlyArray<[string, keyof PtzSpaces]> = [
  ['AbsolutePanTiltPositionSpace', 'absolutePanTilt'],
  ['AbsoluteZoomPositionSpace', 'absoluteZoom'],
  ['RelativePanTiltTranslationSpace', 'relativePanTilt'],
  ['RelativeZoomTranslationSpace', 'relativeZoom'],
  ['ContinuousPanTiltVelocitySpace', 'continuousPanTilt'],
  ['ContinuousZoomVelocitySpace', 'continuousZoom'],
];

function parseNode(node: XmlElement): PtzNode | undefined {
  const token = attribute(node, '', 'token');
  const supported = firstChild(node, SCHEMA_NS, 'SupportedPTZSpaces');
  if (!token || !supported) return undefined;
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
  const name = textOf(firstChild(node, SCHEMA_NS, 'Name'));
  const maximumPresetsText = textOf(firstChild(node, SCHEMA_NS, 'MaximumNumberOfPresets'));
  const maximumPresets = maximumPresetsText !== undefined && /^\d+$/.test(maximumPresetsText)
    ? Number(maximumPresetsText)
    : undefined;
  const homeSupportedText = textOf(firstChild(node, SCHEMA_NS, 'HomeSupported'));
  const homeSupported = homeSupportedText === 'true' || homeSupportedText === '1'
    ? true
    : homeSupportedText === 'false' || homeSupportedText === '0'
      ? false
      : undefined;
  const auxiliaryCommands = [...new Set(
    childElements(node, SCHEMA_NS, 'AuxiliaryCommands')
      .map((entry) => textOf(entry))
      .filter((value): value is string => Boolean(value)),
  )].sort(compareText);
  return {
    token,
    ...(name ? { name } : {}),
    spaces,
    ...(maximumPresets !== undefined ? { maximumPresets } : {}),
    ...(homeSupported !== undefined ? { homeSupported } : {}),
    auxiliaryCommands,
  };
}

function parseNodesResponse(response: XmlElement): PtzNode[] {
  const nodes: PtzNode[] = [];
  for (const element of childElements(response, PTZ_NS, 'PTZNode')) {
    const node = parseNode(element);
    if (!node) throw new PtzResponseError('invalid', 'invalid PTZ GetNodes response');
    nodes.push(node);
  }
  nodes.sort((left, right) => compareText(left.token, right.token));
  return nodes;
}

function findServiceXAddr(response: XmlElement, namespace: string): string | undefined {
  for (const service of childElements(response, DEV_NS, 'Service')) {
    const serviceNamespace = textOf(firstChild(service, DEV_NS, 'Namespace'));
    if (serviceNamespace !== namespace) continue;
    const xaddr = textOf(firstChild(service, DEV_NS, 'XAddr'));
    if (xaddr) return xaddr;
  }
  return undefined;
}

function findMedia1ProfileToken(response: XmlElement): string | undefined {
  for (const profile of childElements(response, MEDIA1_NS, 'Profiles')) {
    const token = attribute(profile, '', 'token');
    const hasPtzConfiguration = Boolean(firstChild(profile, SCHEMA_NS, 'PTZConfiguration'));
    if (token && hasPtzConfiguration) return token;
  }
  return undefined;
}

// Mirrors capabilities.ts's parseProfile('media2'): PTZ binding lives at
// Profiles/Configurations/PTZ, not directly on Profiles like Media1.
function findMedia2ProfileToken(response: XmlElement): string | undefined {
  for (const profile of childElements(response, MEDIA2_NS, 'Profiles')) {
    const token = attribute(profile, '', 'token');
    const configurations = firstChild(profile, MEDIA2_NS, 'Configurations');
    const hasPtz = Boolean(configurations && firstChild(configurations, MEDIA2_NS, 'PTZ'));
    if (token && hasPtz) return token;
  }
  return undefined;
}

function numberAttribute(node: XmlElement | undefined, name: string): number | undefined {
  const raw = node ? attribute(node, '', name) : undefined;
  if (raw === undefined) return undefined;
  const value = Number(raw.trim());
  return Number.isFinite(value) ? value : undefined;
}

function parseStatusResponse(response: XmlElement): PtzStatus {
  const status = firstChild(response, PTZ_NS, 'PTZStatus');
  if (!status) return {};
  const position = firstChild(status, SCHEMA_NS, 'Position');
  const panTiltPosition = position && firstChild(position, SCHEMA_NS, 'PanTilt');
  const zoomPosition = position && firstChild(position, SCHEMA_NS, 'Zoom');
  const x = numberAttribute(panTiltPosition, 'x');
  const y = numberAttribute(panTiltPosition, 'y');
  const zoom = numberAttribute(zoomPosition, 'x');
  const moveStatus = firstChild(status, SCHEMA_NS, 'MoveStatus');
  const panTiltMoveStatus = textOf(moveStatus && firstChild(moveStatus, SCHEMA_NS, 'PanTilt'));
  const zoomMoveStatus = textOf(moveStatus && firstChild(moveStatus, SCHEMA_NS, 'Zoom'));
  const utcTime = textOf(firstChild(status, SCHEMA_NS, 'UtcTime'));
  // Deliberately not read: <Error> — GetStatusResponse carries a
  // camera-supplied error string; it must never surface in PtzStatus.
  return {
    ...(x !== undefined && y !== undefined ? { panTilt: { x, y } } : {}),
    ...(zoom !== undefined ? { zoom } : {}),
    ...(panTiltMoveStatus ? { panTiltMoveStatus } : {}),
    ...(zoomMoveStatus ? { zoomMoveStatus } : {}),
    ...(utcTime ? { utcTime } : {}),
  };
}

// Deliberately public, not doc-tagged as internal-only: openPtzSession is
// fully public and takes a PtzSessionDependencies directly, so both this
// interface and PtzSessionDevice must remain in the emitted declarations, or
// the .d.ts would reference a name that no longer exists. The `serviceCall`
// return type is spelled out structurally (matching deviceClient.ts's
// internal-only OnvifRawResponse) so this module never needs to import that
// type just to name it here.
export interface PtzSessionDevice {
  connect(): Promise<DeviceInfo>;
  connectedMediaUrl(): string;
  serviceCall(body: string, endpoint?: string): Promise<{ statusCode: number; xml: string }>;
}

export interface PtzSessionDependencies {
  createDevice(
    host: string,
    user: string,
    pass: string,
    options: OnvifOptions,
  ): PtzSessionDevice;
}

const defaultDependencies: PtzSessionDependencies = {
  createDevice: (host, user, pass, options) => new OnvifDevice(host, user, pass, options),
};

const GET_SERVICES = `<GetServices xmlns="${DEV_NS}"/>`;
const GET_NODES = `<GetNodes xmlns="${PTZ_NS}"/>`;
const MEDIA1_GET_PROFILES = `<GetProfiles xmlns="${MEDIA1_NS}"/>`;
// Same body capabilities.ts sends for Media2 GetProfiles — not a second dialect.
const MEDIA2_GET_PROFILES = `<GetProfiles xmlns="${MEDIA2_NS}"><Type>All</Type></GetProfiles>`;

class PtzSessionImpl implements PtzSession {
  private closed = false;

  constructor(
    private readonly device: PtzSessionDevice,
    private readonly ptzXAddr: string,
    readonly node: PtzNode,
    readonly profileToken: string,
    private readonly defaultMoveTimeoutMs: number,
  ) {}

  private ensureOpen(): void {
    if (this.closed) throw new Error('PTZ session is closed');
  }

  private profileTokenXml(): string {
    return encodeXml(this.profileToken);
  }

  private async call(
    body: string,
    responseName: string,
    operation: string,
  ): Promise<XmlElement> {
    const raw = await this.device.serviceCall(body, this.ptzXAddr);
    return operationResponse(raw.xml, PTZ_NS, responseName, operation);
  }

  async continuousMove(move: PtzContinuousMoveOptions = {}): Promise<void> {
    this.ensureOpen();
    assertMoveShape(move);
    if (move.panTilt !== undefined && !this.node.spaces.continuousPanTilt) {
      throw new Error('PTZ continuous pan/tilt is not supported');
    }
    if (move.zoom !== undefined && !this.node.spaces.continuousZoom) {
      throw new Error('PTZ continuous zoom is not supported');
    }
    const panTilt = validatedVector(move.panTilt, PAN_TILT_RANGE);
    const zoom = validatedZoom(move.zoom, ZOOM_GENERIC_RANGE);
    const timeout = formatPtzDuration(move.timeoutMs ?? this.defaultMoveTimeoutMs);
    const body = `<ContinuousMove xmlns="${PTZ_NS}"><ProfileToken>${this.profileTokenXml()}</ProfileToken>`
      + vectorXml('Velocity', panTilt, zoom)
      + `<Timeout>${timeout}</Timeout></ContinuousMove>`;
    await this.call(body, 'ContinuousMoveResponse', 'PTZ ContinuousMove');
  }

  async absoluteMove(move: PtzAbsoluteMoveOptions = {}): Promise<void> {
    this.ensureOpen();
    assertMoveShape(move);
    if (move.panTilt !== undefined && !this.node.spaces.absolutePanTilt) {
      throw new Error('PTZ absolute pan/tilt is not supported');
    }
    if (move.zoom !== undefined && !this.node.spaces.absoluteZoom) {
      throw new Error('PTZ absolute zoom is not supported');
    }
    const panTilt = validatedVector(move.panTilt, PAN_TILT_RANGE);
    const zoom = validatedZoom(move.zoom, ZOOM_POSITION_RANGE);
    const speedPanTilt = validatedVector(move.speed?.panTilt, PAN_TILT_RANGE);
    const speedZoom = validatedZoom(move.speed?.zoom, ZOOM_GENERIC_RANGE);
    const speedXml = move.speed === undefined ? '' : vectorXml('Speed', speedPanTilt, speedZoom);
    const body = `<AbsoluteMove xmlns="${PTZ_NS}"><ProfileToken>${this.profileTokenXml()}</ProfileToken>`
      + vectorXml('Position', panTilt, zoom)
      + speedXml
      + '</AbsoluteMove>';
    await this.call(body, 'AbsoluteMoveResponse', 'PTZ AbsoluteMove');
  }

  async relativeMove(move: PtzRelativeMoveOptions = {}): Promise<void> {
    this.ensureOpen();
    assertMoveShape(move);
    if (move.panTilt !== undefined && !this.node.spaces.relativePanTilt) {
      throw new Error('PTZ relative pan/tilt is not supported');
    }
    if (move.zoom !== undefined && !this.node.spaces.relativeZoom) {
      throw new Error('PTZ relative zoom is not supported');
    }
    const panTilt = validatedVector(move.panTilt, PAN_TILT_RANGE);
    const zoom = validatedZoom(move.zoom, ZOOM_GENERIC_RANGE);
    const speedPanTilt = validatedVector(move.speed?.panTilt, PAN_TILT_RANGE);
    const speedZoom = validatedZoom(move.speed?.zoom, ZOOM_GENERIC_RANGE);
    const speedXml = move.speed === undefined ? '' : vectorXml('Speed', speedPanTilt, speedZoom);
    const body = `<RelativeMove xmlns="${PTZ_NS}"><ProfileToken>${this.profileTokenXml()}</ProfileToken>`
      + vectorXml('Translation', panTilt, zoom)
      + speedXml
      + '</RelativeMove>';
    await this.call(body, 'RelativeMoveResponse', 'PTZ RelativeMove');
  }

  async stop(options: PtzStopOptions = {}): Promise<void> {
    this.ensureOpen();
    const panTilt = options.panTilt ?? true;
    const zoom = options.zoom ?? true;
    const body = `<Stop xmlns="${PTZ_NS}"><ProfileToken>${this.profileTokenXml()}</ProfileToken>`
      + `<PanTilt>${panTilt}</PanTilt><Zoom>${zoom}</Zoom></Stop>`;
    await this.call(body, 'StopResponse', 'PTZ Stop');
  }

  async getStatus(): Promise<PtzStatus> {
    this.ensureOpen();
    const body = `<GetStatus xmlns="${PTZ_NS}"><ProfileToken>${this.profileTokenXml()}</ProfileToken></GetStatus>`;
    const response = await this.call(body, 'GetStatusResponse', 'PTZ GetStatus');
    return parseStatusResponse(response);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    try {
      await this.stop({ panTilt: true, zoom: true });
    } catch {
      // A failing Stop during close must never replace an error the caller
      // is already handling; best-effort only.
    }
    this.closed = true;
  }
}

/**
 * Open a PTZ control session.
 *
 * @experimental Physical movement is unverified against real PTZ hardware.
 * Request construction, capability guarding, the device-side move timeout,
 * and stop-on-close are covered by tests; that a camera actually moves as
 * intended is not.
 */
export async function openPtzSession(
  options: PtzSessionOptions,
  dependencies: PtzSessionDependencies = defaultDependencies,
): Promise<PtzSession> {
  const user = options.user ?? '';
  const pass = options.pass ?? '';
  const clientOptions: OnvifOptions = {
    ...(options.deviceUrls ? { deviceUrls: options.deviceUrls } : {}),
    ...(options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : {}),
  };
  const device = dependencies.createDevice(options.host, user, pass, clientOptions);
  await device.connect();

  const servicesRaw = await device.serviceCall(GET_SERVICES);
  const servicesResponse = operationResponse(
    servicesRaw.xml,
    DEV_NS,
    'GetServicesResponse',
    'GetServices',
  );
  const ptzXAddr = findServiceXAddr(servicesResponse, PTZ_NS);
  if (!ptzXAddr) throw new Error('no ONVIF PTZ service');

  const nodesRaw = await device.serviceCall(GET_NODES, ptzXAddr);
  const nodesResponse = operationResponse(nodesRaw.xml, PTZ_NS, 'GetNodesResponse', 'PTZ GetNodes');
  const nodes = parseNodesResponse(nodesResponse);
  if (nodes.length === 0) throw new Error('no ONVIF PTZ node');
  const node = nodes[0];

  let profileToken = options.profileToken;
  if (profileToken === undefined) {
    const media1Raw = await device.serviceCall(MEDIA1_GET_PROFILES, device.connectedMediaUrl());
    const media1Response = operationResponse(
      media1Raw.xml,
      MEDIA1_NS,
      'GetProfilesResponse',
      'Media1 GetProfiles',
    );
    profileToken = findMedia1ProfileToken(media1Response);

    // Media1 came up empty: fall back to Media2, the same second source
    // capabilities.ts already draws PTZ-capable profiles from. Only costs a
    // round trip when Media1 had no PTZ profile, and only when Media2 is
    // advertised at all.
    if (profileToken === undefined) {
      const media2XAddr = findServiceXAddr(servicesResponse, MEDIA2_NS);
      if (media2XAddr) {
        const media2Raw = await device.serviceCall(MEDIA2_GET_PROFILES, media2XAddr);
        const media2Response = operationResponse(
          media2Raw.xml,
          MEDIA2_NS,
          'GetProfilesResponse',
          'Media2 GetProfiles',
        );
        profileToken = findMedia2ProfileToken(media2Response);
      }
    }

    if (profileToken === undefined) throw new Error('no ONVIF PTZ profile');
  }

  const defaultMoveTimeoutMs = options.defaultMoveTimeoutMs ?? DEFAULT_MOVE_TIMEOUT_MS;
  return new PtzSessionImpl(device, ptzXAddr, node, profileToken, defaultMoveTimeoutMs);
}
