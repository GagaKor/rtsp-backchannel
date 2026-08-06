"""Typed ONVIF capability reports and namespace-aware response parsers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields
from typing import Literal
from urllib.parse import unquote
from xml.etree import ElementTree

from .onvif import (
    DeviceInfo,
    OnvifDevice,
    _SoapResponse,
    _safe_xml_fromstring,
)


_SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"
_MEDIA1_NS = "http://www.onvif.org/ver10/media/wsdl"
_MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
_PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"
_WSTOP_NS = "http://docs.oasis-open.org/wsn/t-1"

_AUTH_FAULT_PATTERNS = (
    (
        "NotAuthorized",
        r"(?<![A-Za-z0-9_.-])not[ _-]?authorized(?![A-Za-z0-9_.-])",
    ),
    (
        "InvalidSecurity",
        r"(?<![A-Za-z0-9_.-])invalid[ _-]?security(?![A-Za-z0-9_.-])",
    ),
    (
        "FailedAuthentication",
        r"(?<![A-Za-z0-9_.-])failed[ _-]?authentication(?![A-Za-z0-9_.-])",
    ),
    (
        "Unauthorized",
        r"(?<![A-Za-z0-9_.-])unauthorized(?![A-Za-z0-9_.-])",
    ),
)

_GET_SCOPES = f'<GetScopes xmlns="{_DEVICE_NS}"/>'
_GET_SERVICES = (
    f'<GetServices xmlns="{_DEVICE_NS}">'
    "<IncludeCapability>true</IncludeCapability></GetServices>"
)
_GET_ALL_CAPABILITIES = (
    f'<GetCapabilities xmlns="{_DEVICE_NS}">'
    "<Category>All</Category></GetCapabilities>"
)
_MEDIA1_GET_PROFILES = f'<GetProfiles xmlns="{_MEDIA1_NS}"/>'
_MEDIA2_GET_PROFILES = (
    f'<GetProfiles xmlns="{_MEDIA2_NS}"><Type>All</Type></GetProfiles>'
)
_MEDIA2_GET_OPTIONS = (
    f'<GetVideoEncoderConfigurationOptions xmlns="{_MEDIA2_NS}"/>'
)
_PTZ_GET_CAPABILITIES = f'<GetServiceCapabilities xmlns="{_PTZ_NS}"/>'
_PTZ_GET_NODES = f'<GetNodes xmlns="{_PTZ_NS}"/>'
_EVENTS_GET_CAPABILITIES = (
    f'<GetServiceCapabilities xmlns="{_EVENTS_NS}"/>'
)
_EVENTS_GET_PROPERTIES = f'<GetEventProperties xmlns="{_EVENTS_NS}"/>'

_LEGACY_SERVICES = {
    "Analytics": "http://www.onvif.org/ver20/analytics/wsdl",
    "Device": _DEVICE_NS,
    "DeviceIO": "http://www.onvif.org/ver10/deviceIO/wsdl",
    "Display": "http://www.onvif.org/ver10/display/wsdl",
    "Events": _EVENTS_NS,
    "Imaging": "http://www.onvif.org/ver20/imaging/wsdl",
    "Media": _MEDIA1_NS,
    "PTZ": _PTZ_NS,
    "Receiver": "http://www.onvif.org/ver10/receiver/wsdl",
    "Recording": "http://www.onvif.org/ver10/recording/wsdl",
    "Replay": "http://www.onvif.org/ver10/replay/wsdl",
    "Search": "http://www.onvif.org/ver10/search/wsdl",
}


@dataclass(frozen=True)
class CameraCapabilityVersion:
    major: int
    minor: int


@dataclass(frozen=True)
class CameraCapabilityService:
    namespace: str
    xaddr: str
    version: CameraCapabilityVersion | None = None


@dataclass(frozen=True)
class CameraCapabilityWarning:
    operation: str
    message: str


@dataclass(frozen=True)
class CameraCapabilityProfile:
    token: str
    source: Literal["media1", "media2"]
    has_audio_encoder: bool
    has_audio_output: bool
    has_audio_source: bool
    name: str | None = None
    ptz_configuration_token: str | None = None
    ptz_node_token: str | None = None


@dataclass(frozen=True)
class PtzSpaces:
    absolute_pan_tilt: bool = False
    absolute_zoom: bool = False
    relative_pan_tilt: bool = False
    relative_zoom: bool = False
    continuous_pan_tilt: bool = False
    continuous_zoom: bool = False


@dataclass(frozen=True)
class PtzNode:
    token: str
    spaces: PtzSpaces
    name: str | None = None
    maximum_presets: int | None = None
    home_supported: bool | None = None
    auxiliary_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class PtzServiceCapabilities:
    e_flip: bool | None = None
    reverse: bool | None = None
    get_compatible_configurations: bool | None = None
    move_status: bool | None = None
    status_position: bool | None = None


@dataclass(frozen=True)
class PtzCapabilityReport:
    detected: bool | None
    pan_tilt_supported: bool | None
    zoom_supported: bool | None
    profile_tokens: tuple[str, ...]
    service_capabilities: PtzServiceCapabilities | None
    nodes: tuple[PtzNode, ...]


@dataclass(frozen=True)
class EventServiceCapabilities:
    ws_subscription_policy_support: bool | None = None
    ws_pull_point_support: bool | None = None
    ws_pausable_subscription_manager_interface_support: bool | None = None
    persistent_notification_storage: bool | None = None
    max_notification_producers: int | None = None
    max_pull_points: int | None = None
    event_broker_protocols: tuple[str, ...] | None = None
    max_event_brokers: int | None = None


@dataclass(frozen=True)
class EventTopic:
    namespace: str | None
    path: str


@dataclass(frozen=True)
class EventCapabilityReport:
    detected: bool | None
    service_capabilities: EventServiceCapabilities | None
    topics: tuple[EventTopic, ...]


@dataclass(frozen=True)
class Media2CapabilityReport:
    detected: bool | None
    encodings: tuple[str, ...]
    h265_supported: bool | None


@dataclass(frozen=True)
class CameraCapabilityReport:
    device: DeviceInfo
    scopes: tuple[str, ...]
    declared_profiles: tuple[str, ...]
    service_discovery: Literal[
        "getServices", "getCapabilities", "unavailable"
    ]
    services: tuple[CameraCapabilityService, ...]
    profiles: tuple[CameraCapabilityProfile, ...]
    ptz: PtzCapabilityReport
    events: EventCapabilityReport
    media2: Media2CapabilityReport
    warnings: tuple[CameraCapabilityWarning, ...]


@dataclass(frozen=True)
class _ScopesResult:
    scopes: tuple[str, ...]
    declared_profiles: tuple[str, ...]


@dataclass(frozen=True)
class _ServicesResult:
    services: tuple[CameraCapabilityService, ...]
    event_service_capabilities: EventServiceCapabilities | None = None


@dataclass(frozen=True)
class _PtzNodesResult:
    nodes: tuple[PtzNode, ...]
    pan_tilt_supported: bool
    zoom_supported: bool


class _OnvifResponseError(ValueError):
    """A sanitized invalid-response or SOAP-fault error."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        fault_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.fault_code = fault_code


def _tag_parts(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return "", tag


def _child(
    parent: ElementTree.Element, namespace: str, local: str
) -> ElementTree.Element | None:
    wanted = (namespace, local)
    return next(
        (item for item in list(parent) if _tag_parts(item.tag) == wanted),
        None,
    )


def _children(
    parent: ElementTree.Element, namespace: str, local: str
) -> tuple[ElementTree.Element, ...]:
    wanted = (namespace, local)
    return tuple(item for item in list(parent) if _tag_parts(item.tag) == wanted)


def _plain_text(element: ElementTree.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _xml_scalar(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[ \t\r\n]+", " ", value).strip(" ")
    return normalized or None


def _strict_bool(value: str | None) -> bool | None:
    normalized = _xml_scalar(value)
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0"):
        return False
    return None


def _strict_nonnegative_int32(value: str | None) -> int | None:
    normalized = _xml_scalar(value)
    if normalized is None or re.fullmatch(r"[+-]?[0-9]+", normalized) is None:
        return None
    digits = normalized[1:] if normalized[:1] in ("+", "-") else normalized
    significant = digits.lstrip("0") or "0"
    if len(significant) > 10:
        return None
    parsed = int(significant)
    if normalized.startswith("-"):
        parsed = -parsed
    if not -(2**31) <= parsed <= 2**31 - 1 or parsed < 0:
        return None
    return parsed


def _safe_fault_code(value: str | None) -> str | None:
    normalized = _xml_scalar(value)
    if not normalized:
        return None
    local = normalized.rsplit(":", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", local):
        return local
    return None


def _canonical_fault_code(
    values: tuple[str | None, ...], fallback: str | None
) -> str:
    joined = " ".join(_xml_scalar(value) or "" for value in values)
    for canonical, pattern in _AUTH_FAULT_PATTERNS:
        if re.search(pattern, joined, re.IGNORECASE):
            return canonical
    return fallback or "Fault"


def _raise_fault(fault: ElementTree.Element, soap_ns: str) -> None:
    values: list[str | None] = []
    fallback: str | None = None
    if soap_ns == _SOAP12_NS:
        code = _child(fault, soap_ns, "Code")
        while code is not None:
            value = _child(code, soap_ns, "Value")
            text = value.text if value is not None else None
            values.append(text)
            safe = _safe_fault_code(text)
            if safe:
                fallback = safe
            code = _child(code, soap_ns, "Subcode")
        reason = _child(fault, soap_ns, "Reason")
        if reason is not None:
            values.extend(
                item.text for item in _children(reason, soap_ns, "Text")
            )
    else:
        code = _child(fault, "", "faultcode")
        reason = _child(fault, "", "faultstring")
        values.extend(
            (
                code.text if code is not None else None,
                reason.text if reason is not None else None,
            )
        )
        fallback = _safe_fault_code(values[0])
    canonical = _canonical_fault_code(tuple(values), fallback)
    raise _OnvifResponseError(
        "fault", f"SOAP Fault: {canonical}", fault_code=canonical
    )


def _soap_operation(
    xml: bytes | str, namespace: str, operation: str, description: str
) -> ElementTree.Element:
    try:
        root = _safe_xml_fromstring(xml)
    except (ElementTree.ParseError, TypeError, ValueError) as error:
        raise _OnvifResponseError("invalid", "invalid XML document") from error
    soap_ns, root_name = _tag_parts(root.tag)
    if root_name != "Envelope" or soap_ns not in (_SOAP11_NS, _SOAP12_NS):
        raise _OnvifResponseError("invalid", f"invalid {description} response")
    body = _child(root, soap_ns, "Body")
    if body is None:
        raise _OnvifResponseError("invalid", f"invalid {description} response")
    fault = _child(body, soap_ns, "Fault")
    if fault is not None:
        _raise_fault(fault, soap_ns)
    result = _child(body, namespace, operation)
    if result is None:
        raise _OnvifResponseError("invalid", f"invalid {description} response")
    return result


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _decode_profile_name(scope: str) -> str | None:
    prefix = "onvif://www.onvif.org/Profile/"
    if not scope.startswith(prefix):
        return None
    encoded = re.split(r"[/?#]", scope[len(prefix) :], maxsplit=1)[0]
    if not encoded or re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
        return None
    try:
        decoded = unquote(encoded, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if decoded.lower() == "streaming":
        return "S"
    return decoded.upper()


def _parse_scopes_response(xml: bytes | str) -> _ScopesResult:
    response = _soap_operation(
        xml, _DEVICE_NS, "GetScopesResponse", "GetScopes"
    )
    scopes: list[str] = []
    profiles: list[str] = []
    for scope in _children(response, _DEVICE_NS, "Scopes"):
        value = _plain_text(_child(scope, _SCHEMA_NS, "ScopeItem"))
        if not value:
            continue
        scopes.append(value)
        profile = _decode_profile_name(value)
        if profile:
            profiles.append(profile)
    return _ScopesResult(_deduplicate(scopes), _deduplicate(profiles))


def _version(parent: ElementTree.Element) -> CameraCapabilityVersion | None:
    version = _child(parent, _DEVICE_NS, "Version")
    if version is None:
        return None
    major_element = _child(version, _SCHEMA_NS, "Major")
    minor_element = _child(version, _SCHEMA_NS, "Minor")
    major = _strict_nonnegative_int32(
        major_element.text if major_element is not None else None
    )
    minor = _strict_nonnegative_int32(
        minor_element.text if minor_element is not None else None
    )
    if major is None or minor is None:
        return None
    return CameraCapabilityVersion(major, minor)


def _service_sort_key(
    service: CameraCapabilityService,
) -> tuple[str, str, int, int]:
    version = service.version or CameraCapabilityVersion(-1, -1)
    return service.namespace, service.xaddr, version.major, version.minor


def _select_service(
    services: tuple[CameraCapabilityService, ...], namespace: str
) -> CameraCapabilityService | None:
    matching = [service for service in services if service.namespace == namespace]
    if not matching:
        return None
    return min(
        matching,
        key=lambda service: (
            -(service.version.major if service.version else -1),
            -(service.version.minor if service.version else -1),
            service.xaddr,
        ),
    )


def _event_capabilities_from_element(
    element: ElementTree.Element,
) -> EventServiceCapabilities:
    return EventServiceCapabilities(
        ws_subscription_policy_support=_strict_bool(
            element.attrib.get("WSSubscriptionPolicySupport")
        ),
        ws_pull_point_support=_strict_bool(
            element.attrib.get("WSPullPointSupport")
        ),
        ws_pausable_subscription_manager_interface_support=_strict_bool(
            element.attrib.get(
                "WSPausableSubscriptionManagerInterfaceSupport"
            )
        ),
        persistent_notification_storage=_strict_bool(
            element.attrib.get("PersistentNotificationStorage")
        ),
        max_notification_producers=_strict_nonnegative_int32(
            element.attrib.get("MaxNotificationProducers")
        ),
        max_pull_points=_strict_nonnegative_int32(
            element.attrib.get("MaxPullPoints")
        ),
        max_event_brokers=_strict_nonnegative_int32(
            element.attrib.get("MaxEventBrokers")
        ),
        event_broker_protocols=_parse_protocols(
            element.attrib.get("EventBrokerProtocols")
        ),
    )


def _parse_protocols(value: str | None) -> tuple[str, ...] | None:
    normalized = _xml_scalar(value)
    if not normalized:
        return None
    return tuple(sorted(set(normalized.split(" "))))


def _parse_services_response(xml: bytes | str) -> _ServicesResult:
    response = _soap_operation(
        xml, _DEVICE_NS, "GetServicesResponse", "GetServices"
    )
    services: list[CameraCapabilityService] = []
    embedded: dict[
        tuple[str, str, CameraCapabilityVersion | None], EventServiceCapabilities
    ] = {}
    for element in _children(response, _DEVICE_NS, "Service"):
        namespace = _plain_text(_child(element, _DEVICE_NS, "Namespace"))
        xaddr = _plain_text(_child(element, _DEVICE_NS, "XAddr"))
        if not namespace or not xaddr:
            raise _OnvifResponseError("invalid", "invalid GetServices response")
        service = CameraCapabilityService(namespace, xaddr, _version(element))
        services.append(service)
        if namespace == _EVENTS_NS:
            wrapper = _child(element, _DEVICE_NS, "Capabilities")
            caps = (
                _child(wrapper, _EVENTS_NS, "Capabilities")
                if wrapper is not None
                else None
            )
            if caps is not None:
                embedded.setdefault(
                    (service.namespace, service.xaddr, service.version),
                    _event_capabilities_from_element(caps),
                )
    if not services:
        raise _OnvifResponseError("invalid", "no services in GetServices response")
    ordered = tuple(sorted(services, key=_service_sort_key))
    event = _select_service(ordered, _EVENTS_NS)
    event_capabilities = (
        embedded.get((event.namespace, event.xaddr, event.version))
        if event is not None
        else None
    )
    return _ServicesResult(ordered, event_capabilities)


def _legacy_event_capabilities(
    element: ElementTree.Element,
) -> EventServiceCapabilities:
    def child_bool(name: str) -> bool | None:
        item = _child(element, _SCHEMA_NS, name)
        return _strict_bool(item.text if item is not None else None)

    return EventServiceCapabilities(
        ws_subscription_policy_support=child_bool(
            "WSSubscriptionPolicySupport"
        ),
        ws_pull_point_support=child_bool("WSPullPointSupport"),
        ws_pausable_subscription_manager_interface_support=child_bool(
            "WSPausableSubscriptionManagerInterfaceSupport"
        ),
        persistent_notification_storage=child_bool(
            "PersistentNotificationStorage"
        ),
    )


def _parse_capabilities_response(xml: bytes | str) -> _ServicesResult:
    response = _soap_operation(
        xml, _DEVICE_NS, "GetCapabilitiesResponse", "GetCapabilities"
    )
    capabilities = _child(response, _DEVICE_NS, "Capabilities")
    if capabilities is None:
        raise _OnvifResponseError(
            "invalid", "invalid GetCapabilities response"
        )
    containers = [capabilities]
    extension = _child(capabilities, _SCHEMA_NS, "Extension")
    if extension is not None:
        containers.append(extension)
    services: list[CameraCapabilityService] = []
    event_capabilities_by_service: dict[
        tuple[str, str, CameraCapabilityVersion | None],
        EventServiceCapabilities,
    ] = {}
    for container in containers:
        for item in list(container):
            namespace, local = _tag_parts(item.tag)
            if namespace != _SCHEMA_NS or local not in _LEGACY_SERVICES:
                continue
            xaddr = _plain_text(_child(item, _SCHEMA_NS, "XAddr"))
            if not xaddr:
                continue
            service = CameraCapabilityService(_LEGACY_SERVICES[local], xaddr)
            services.append(service)
            if local == "Events":
                event_capabilities_by_service.setdefault(
                    (service.namespace, service.xaddr, service.version),
                    _legacy_event_capabilities(item),
                )
    if not services:
        raise _OnvifResponseError(
            "invalid", "no services in GetCapabilities response"
        )
    ordered = tuple(sorted(services, key=_service_sort_key))
    event_service = _select_service(ordered, _EVENTS_NS)
    event_capabilities = (
        event_capabilities_by_service.get(
            (
                event_service.namespace,
                event_service.xaddr,
                event_service.version,
            )
        )
        if event_service is not None
        else None
    )
    return _ServicesResult(ordered, event_capabilities)


def _required_token(element: ElementTree.Element, description: str) -> str:
    token = element.attrib.get("token")
    if not token:
        raise _OnvifResponseError("invalid", f"invalid {description} response")
    return token


def _parse_media1_profiles_response(
    xml: bytes | str,
) -> tuple[CameraCapabilityProfile, ...]:
    response = _soap_operation(
        xml, _MEDIA1_NS, "GetProfilesResponse", "Media1 GetProfiles"
    )
    profiles: list[CameraCapabilityProfile] = []
    for element in _children(response, _MEDIA1_NS, "Profiles"):
        token = _required_token(element, "Media1 GetProfiles")
        ptz = _child(element, _SCHEMA_NS, "PTZConfiguration")
        profiles.append(
            CameraCapabilityProfile(
                token=token,
                source="media1",
                has_audio_encoder=(
                    _child(element, _SCHEMA_NS, "AudioEncoderConfiguration")
                    is not None
                ),
                has_audio_output=(
                    _child(element, _SCHEMA_NS, "AudioOutputConfiguration")
                    is not None
                ),
                has_audio_source=(
                    _child(element, _SCHEMA_NS, "AudioSourceConfiguration")
                    is not None
                ),
                name=_plain_text(_child(element, _SCHEMA_NS, "Name")),
                ptz_configuration_token=(
                    (ptz.attrib.get("token") or None)
                    if ptz is not None
                    else None
                ),
                ptz_node_token=(
                    _plain_text(_child(ptz, _SCHEMA_NS, "NodeToken"))
                    if ptz is not None
                    else None
                ),
            )
        )
    return tuple(sorted(profiles, key=lambda profile: profile.token))


def _parse_media2_profiles_response(
    xml: bytes | str,
) -> tuple[CameraCapabilityProfile, ...]:
    response = _soap_operation(
        xml, _MEDIA2_NS, "GetProfilesResponse", "Media2 GetProfiles"
    )
    profiles: list[CameraCapabilityProfile] = []
    for element in _children(response, _MEDIA2_NS, "Profiles"):
        token = _required_token(element, "Media2 GetProfiles")
        configurations = _child(element, _MEDIA2_NS, "Configurations")
        ptz = (
            _child(configurations, _MEDIA2_NS, "PTZ")
            if configurations is not None
            else None
        )
        profiles.append(
            CameraCapabilityProfile(
                token=token,
                source="media2",
                has_audio_encoder=(
                    configurations is not None
                    and _child(configurations, _MEDIA2_NS, "AudioEncoder")
                    is not None
                ),
                has_audio_output=(
                    configurations is not None
                    and _child(configurations, _MEDIA2_NS, "AudioOutput")
                    is not None
                ),
                has_audio_source=(
                    configurations is not None
                    and _child(configurations, _MEDIA2_NS, "AudioSource")
                    is not None
                ),
                name=_plain_text(_child(element, _MEDIA2_NS, "Name")),
                ptz_configuration_token=(
                    (ptz.attrib.get("token") or None)
                    if ptz is not None
                    else None
                ),
                ptz_node_token=(
                    _plain_text(_child(ptz, _SCHEMA_NS, "NodeToken"))
                    if ptz is not None
                    else None
                ),
            )
        )
    return tuple(sorted(profiles, key=lambda profile: profile.token))


def _parse_ptz_service_capabilities_response(
    xml: bytes | str,
) -> PtzServiceCapabilities:
    response = _soap_operation(
        xml,
        _PTZ_NS,
        "GetServiceCapabilitiesResponse",
        "PTZ GetServiceCapabilities",
    )
    capabilities = _child(response, _PTZ_NS, "Capabilities")
    if capabilities is None:
        raise _OnvifResponseError(
            "invalid", "invalid PTZ GetServiceCapabilities response"
        )
    return PtzServiceCapabilities(
        e_flip=_strict_bool(capabilities.attrib.get("EFlip")),
        reverse=_strict_bool(capabilities.attrib.get("Reverse")),
        get_compatible_configurations=_strict_bool(
            capabilities.attrib.get("GetCompatibleConfigurations")
        ),
        move_status=_strict_bool(capabilities.attrib.get("MoveStatus")),
        status_position=_strict_bool(
            capabilities.attrib.get("StatusPosition")
        ),
    )


def _ptz_spaces(element: ElementTree.Element) -> PtzSpaces:
    names = {_tag_parts(item.tag) for item in list(element)}
    return PtzSpaces(
        absolute_pan_tilt=(_SCHEMA_NS, "AbsolutePanTiltPositionSpace") in names,
        absolute_zoom=(_SCHEMA_NS, "AbsoluteZoomPositionSpace") in names,
        relative_pan_tilt=(_SCHEMA_NS, "RelativePanTiltTranslationSpace") in names,
        relative_zoom=(_SCHEMA_NS, "RelativeZoomTranslationSpace") in names,
        continuous_pan_tilt=(_SCHEMA_NS, "ContinuousPanTiltVelocitySpace") in names,
        continuous_zoom=(_SCHEMA_NS, "ContinuousZoomVelocitySpace") in names,
    )


def _parse_ptz_nodes_response(xml: bytes | str) -> _PtzNodesResult:
    response = _soap_operation(xml, _PTZ_NS, "GetNodesResponse", "PTZ GetNodes")
    nodes: list[PtzNode] = []
    for element in _children(response, _PTZ_NS, "PTZNode"):
        token = _required_token(element, "PTZ GetNodes")
        spaces_element = _child(element, _SCHEMA_NS, "SupportedPTZSpaces")
        if spaces_element is None:
            raise _OnvifResponseError("invalid", "invalid PTZ GetNodes response")
        spaces = _ptz_spaces(spaces_element)
        maximum = _child(element, _SCHEMA_NS, "MaximumNumberOfPresets")
        home = _child(element, _SCHEMA_NS, "HomeSupported")
        auxiliary = sorted(
            {
                value
                for value in (
                    _plain_text(item)
                    for item in _children(
                        element, _SCHEMA_NS, "AuxiliaryCommands"
                    )
                )
                if value
            }
        )
        nodes.append(
            PtzNode(
                token=token,
                spaces=spaces,
                name=_plain_text(_child(element, _SCHEMA_NS, "Name")),
                maximum_presets=_strict_nonnegative_int32(
                    maximum.text if maximum is not None else None
                ),
                home_supported=_strict_bool(
                    home.text if home is not None else None
                ),
                auxiliary_commands=tuple(auxiliary),
            )
        )
    ordered = tuple(sorted(nodes, key=lambda node: node.token))
    pan_tilt = any(
        node.spaces.absolute_pan_tilt
        or node.spaces.relative_pan_tilt
        or node.spaces.continuous_pan_tilt
        for node in ordered
    )
    zoom = any(
        node.spaces.absolute_zoom
        or node.spaces.relative_zoom
        or node.spaces.continuous_zoom
        for node in ordered
    )
    return _PtzNodesResult(ordered, pan_tilt, zoom)


def _parse_event_service_capabilities_response(
    xml: bytes | str,
) -> EventServiceCapabilities:
    response = _soap_operation(
        xml,
        _EVENTS_NS,
        "GetServiceCapabilitiesResponse",
        "Events GetServiceCapabilities",
    )
    capabilities = _child(response, _EVENTS_NS, "Capabilities")
    if capabilities is None:
        raise _OnvifResponseError(
            "invalid", "invalid Events GetServiceCapabilities response"
        )
    return _event_capabilities_from_element(capabilities)


def _merge_event_service_capabilities(
    legacy: EventServiceCapabilities | None,
    current: EventServiceCapabilities | None,
) -> EventServiceCapabilities | None:
    if legacy is None:
        return current
    if current is None:
        return legacy
    values = {
        field.name: (
            getattr(current, field.name)
            if getattr(current, field.name) is not None
            else getattr(legacy, field.name)
        )
        for field in fields(EventServiceCapabilities)
    }
    return EventServiceCapabilities(**values)


def _parse_event_properties_response(
    xml: bytes | str,
) -> tuple[EventTopic, ...]:
    response = _soap_operation(
        xml,
        _EVENTS_NS,
        "GetEventPropertiesResponse",
        "Events GetEventProperties",
    )
    topic_set = _child(response, _WSTOP_NS, "TopicSet")
    if topic_set is None:
        raise _OnvifResponseError(
            "invalid", "invalid Events GetEventProperties response"
        )
    topics: list[EventTopic] = []
    path: list[str] = []
    stack: list[tuple[ElementTree.Element, bool]] = [
        (item, False) for item in reversed(list(topic_set))
    ]
    while stack:
        element, exiting = stack.pop()
        if exiting:
            path.pop()
            continue
        namespace, local = _tag_parts(element.tag)
        path.append(local)
        if _strict_bool(element.attrib.get(f"{{{_WSTOP_NS}}}topic")) is True:
            topics.append(
                EventTopic(namespace=namespace or None, path="/".join(path))
            )
        stack.append((element, True))
        stack.extend((item, False) for item in reversed(list(element)))
    unique = {
        (topic.path, topic.namespace): topic
        for topic in topics
    }
    return tuple(
        sorted(
            unique.values(),
            key=lambda topic: (topic.path, topic.namespace or ""),
        )
    )


def _parse_media2_options_response(xml: bytes | str) -> tuple[str, ...]:
    response = _soap_operation(
        xml,
        _MEDIA2_NS,
        "GetVideoEncoderConfigurationOptionsResponse",
        "Media2 GetVideoEncoderConfigurationOptions",
    )
    encodings: set[str] = set()
    standard_encoding = False
    for options in _children(response, _MEDIA2_NS, "Options"):
        for encoding in _children(options, _SCHEMA_NS, "Encoding"):
            value = _xml_scalar(encoding.text)
            if value:
                standard_encoding = True
                encodings.add(value.upper())
        attribute = _xml_scalar(options.attrib.get("Encoding"))
        if attribute:
            encodings.add(attribute.upper())
    if not standard_encoding:
        raise _OnvifResponseError(
            "invalid",
            "invalid Media2 GetVideoEncoderConfigurationOptions response",
        )
    return tuple(sorted(encodings))


class _OnvifHttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


_EXPECTED_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    ElementTree.ParseError,
    _OnvifResponseError,
)


def _parse_read_only_response(response: _SoapResponse, parser):
    if response.status_code in (401, 403):
        raise _OnvifHttpError(response.status_code)
    try:
        result = parser(response.xml)
    except _EXPECTED_OPERATION_ERRORS as error:
        if isinstance(error, _OnvifResponseError) and error.kind == "fault":
            raise
        if not 200 <= response.status_code < 300:
            raise _OnvifHttpError(response.status_code) from None
        raise
    if not 200 <= response.status_code < 300:
        raise _OnvifHttpError(response.status_code)
    return result


def _is_authentication_failure(error: BaseException) -> bool:
    if isinstance(error, _OnvifHttpError):
        return error.status_code in (401, 403)
    if isinstance(error, _OnvifResponseError):
        return error.fault_code in {
            canonical for canonical, _ in _AUTH_FAULT_PATTERNS
        }
    message = _xml_scalar(str(error)) or ""
    return any(
        re.search(pattern, message, re.IGNORECASE) is not None
        for _, pattern in _AUTH_FAULT_PATTERNS
    )


def _sanitized_warning_message(error: BaseException) -> str:
    if isinstance(error, _OnvifHttpError):
        return str(error)
    if isinstance(error, _OnvifResponseError):
        return str(error)
    message = str(error)
    if re.search(r"timeout|timed out|deadline", message, re.IGNORECASE):
        return "request timeout"
    if re.search(r"response body exceeds", message, re.IGNORECASE):
        return "response body exceeds limit"
    if re.search(r"header", message, re.IGNORECASE):
        return "response headers exceed limit"
    if re.search(r"aborted", message, re.IGNORECASE):
        return "response aborted before completion"
    if re.search(r"closed before completion", message, re.IGNORECASE):
        return "response closed before completion"
    network_code = re.search(
        r"\b(E(?:CONNRESET|CONNREFUSED|HOSTUNREACH|NETUNREACH|NOTFOUND))\b",
        message,
        re.IGNORECASE,
    )
    if network_code:
        return f"network request failed ({network_code.group(1).upper()})"
    return "request failed"


def _has_reported_fields(value) -> bool:
    return value is not None and any(
        getattr(value, field.name) is not None for field in fields(value)
    )


def get_camera_capabilities(
    *,
    host: str,
    user: str = "",
    password: str = "",
    device_urls: list[str] | None = None,
    timeout: float = 8.0,
) -> CameraCapabilityReport:
    """Return read-only, best-effort ONVIF capability evidence."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than 0")
    device_client = OnvifDevice(
        host,
        user,
        password,
        device_urls=device_urls,
        timeout=timeout,
    )
    device = device_client.connect()
    warnings: list[CameraCapabilityWarning] = []

    def warn(operation: str, error: BaseException) -> None:
        if _is_authentication_failure(error):
            if isinstance(error, (_OnvifHttpError, _OnvifResponseError)):
                raise error
            raise RuntimeError("ONVIF authentication failed") from None
        warnings.append(
            CameraCapabilityWarning(
                operation=operation,
                message=_sanitized_warning_message(error),
            )
        )

    def call(body: str, parser, endpoint: str | None = None):
        return _parse_read_only_response(
            device_client.read_only_call(body, endpoint), parser
        )

    scopes: tuple[str, ...] = ()
    declared_profiles: tuple[str, ...] = ()
    try:
        parsed_scopes = call(_GET_SCOPES, _parse_scopes_response)
        scopes = parsed_scopes.scopes
        declared_profiles = parsed_scopes.declared_profiles
    except _EXPECTED_OPERATION_ERRORS as error:
        warn("GetScopes", error)

    service_discovery = "unavailable"
    services: tuple[CameraCapabilityService, ...] = ()
    discovered_event_capabilities: EventServiceCapabilities | None = None
    try:
        parsed_services = call(_GET_SERVICES, _parse_services_response)
        service_discovery = "getServices"
        services = parsed_services.services
        discovered_event_capabilities = (
            parsed_services.event_service_capabilities
        )
    except _EXPECTED_OPERATION_ERRORS as get_services_error:
        warn("GetServices", get_services_error)
        try:
            parsed_services = call(
                _GET_ALL_CAPABILITIES, _parse_capabilities_response
            )
            service_discovery = "getCapabilities"
            services = parsed_services.services
            discovered_event_capabilities = (
                parsed_services.event_service_capabilities
            )
        except _EXPECTED_OPERATION_ERRORS as get_capabilities_error:
            warn("GetCapabilities", get_capabilities_error)

    profiles: list[CameraCapabilityProfile] = []
    media1_service = _select_service(services, _MEDIA1_NS)
    try:
        media1_endpoint = (
            media1_service.xaddr
            if media1_service is not None
            else device_client._required_media_url()
        )
        profiles.extend(
            call(
                _MEDIA1_GET_PROFILES,
                _parse_media1_profiles_response,
                media1_endpoint,
            )
        )
    except _EXPECTED_OPERATION_ERRORS as error:
        warn("Media1 GetProfiles", error)

    discovery_available = service_discovery != "unavailable"
    ptz_service = _select_service(services, _PTZ_NS)
    ptz_detected = bool(ptz_service) if discovery_available else None
    ptz_service_capabilities: PtzServiceCapabilities | None = None
    ptz_nodes: tuple[PtzNode, ...] = ()
    pan_tilt_supported: bool | None = None
    zoom_supported: bool | None = None
    if ptz_service is not None:
        try:
            parsed_ptz_capabilities = call(
                _PTZ_GET_CAPABILITIES,
                _parse_ptz_service_capabilities_response,
                ptz_service.xaddr,
            )
            if _has_reported_fields(parsed_ptz_capabilities):
                ptz_service_capabilities = parsed_ptz_capabilities
        except _EXPECTED_OPERATION_ERRORS as error:
            warn("PTZ GetServiceCapabilities", error)
        try:
            parsed_nodes = call(
                _PTZ_GET_NODES,
                _parse_ptz_nodes_response,
                ptz_service.xaddr,
            )
            ptz_nodes = parsed_nodes.nodes
            pan_tilt_supported = parsed_nodes.pan_tilt_supported
            zoom_supported = parsed_nodes.zoom_supported
        except _EXPECTED_OPERATION_ERRORS as error:
            warn("PTZ GetNodes", error)

    event_service = _select_service(services, _EVENTS_NS)
    events_detected = bool(event_service) if discovery_available else None
    current_event_capabilities: EventServiceCapabilities | None = None
    topics: tuple[EventTopic, ...] = ()
    if event_service is not None:
        try:
            current_event_capabilities = call(
                _EVENTS_GET_CAPABILITIES,
                _parse_event_service_capabilities_response,
                event_service.xaddr,
            )
        except _EXPECTED_OPERATION_ERRORS as error:
            warn("Events GetServiceCapabilities", error)
        try:
            topics = call(
                _EVENTS_GET_PROPERTIES,
                _parse_event_properties_response,
                event_service.xaddr,
            )
        except _EXPECTED_OPERATION_ERRORS as error:
            warn("Events GetEventProperties", error)
    event_service_capabilities = _merge_event_service_capabilities(
        discovered_event_capabilities, current_event_capabilities
    )
    if not _has_reported_fields(event_service_capabilities):
        event_service_capabilities = None

    media2_service = _select_service(services, _MEDIA2_NS)
    media2_detected = (
        bool(media2_service) if service_discovery == "getServices" else None
    )
    encodings: tuple[str, ...] = ()
    h265_supported: bool | None = None
    if media2_service is not None:
        try:
            profiles.extend(
                call(
                    _MEDIA2_GET_PROFILES,
                    _parse_media2_profiles_response,
                    media2_service.xaddr,
                )
            )
        except _EXPECTED_OPERATION_ERRORS as error:
            warn("Media2 GetProfiles", error)
        try:
            encodings = call(
                _MEDIA2_GET_OPTIONS,
                _parse_media2_options_response,
                media2_service.xaddr,
            )
            h265_supported = "H265" in {
                encoding.upper() for encoding in encodings
            }
        except _EXPECTED_OPERATION_ERRORS as error:
            warn("Media2 GetVideoEncoderConfigurationOptions", error)

    profiles.sort(key=lambda profile: (profile.token, profile.source))
    profile_tokens = tuple(
        sorted(
            {
                profile.token
                for profile in profiles
                if profile.ptz_configuration_token
            }
        )
    )
    return CameraCapabilityReport(
        device=device,
        scopes=scopes,
        declared_profiles=declared_profiles,
        service_discovery=service_discovery,
        services=services,
        profiles=tuple(profiles),
        ptz=PtzCapabilityReport(
            detected=ptz_detected,
            pan_tilt_supported=pan_tilt_supported,
            zoom_supported=zoom_supported,
            profile_tokens=profile_tokens,
            service_capabilities=ptz_service_capabilities,
            nodes=ptz_nodes,
        ),
        events=EventCapabilityReport(
            detected=events_detected,
            service_capabilities=event_service_capabilities,
            topics=topics,
        ),
        media2=Media2CapabilityReport(
            detected=media2_detected,
            encodings=encodings,
            h265_supported=h265_supported,
        ),
        warnings=tuple(warnings),
    )


__all__ = [
    "CameraCapabilityProfile",
    "CameraCapabilityReport",
    "CameraCapabilityService",
    "CameraCapabilityVersion",
    "CameraCapabilityWarning",
    "EventCapabilityReport",
    "EventServiceCapabilities",
    "EventTopic",
    "Media2CapabilityReport",
    "PtzCapabilityReport",
    "PtzNode",
    "PtzServiceCapabilities",
    "PtzSpaces",
    "get_camera_capabilities",
]
