"""ONVIF PTZ movement control.

A session opens once (connect -> GetServices -> GetNodes -> resolve the
profile token), caches the node's supported spaces, and rejects any
operation the camera did not advertise before building a request. Control
reuses the same authenticated transport the read-only capability report
uses; it is a different SOAP body on the existing pipe, not a new one.

This module intentionally does not import ``capabilities.py`` (and vice
versa) so the two stay independent; shared value types live in
``ptz_types.py``. PTZ SOAP fault classification mirrors what
``capabilities.py`` already does for its own responses, but is duplicated
here rather than imported, for the same reason.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from xml.etree import ElementTree

from .onvif import OnvifDevice, _safe_xml_fromstring
from .ptz_types import PtzNode, PtzSpaces

_SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"
_MEDIA1_NS = "http://www.onvif.org/ver10/media/wsdl"
_MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
_PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"

_DEFAULT_MOVE_TIMEOUT_MS = 1000.0
_PAN_TILT_RANGE = (-1.0, 1.0)
# Every zoom quantity is -1..1 except an absolute zoom *position*, which is 0..1.
_ZOOM_GENERIC_RANGE = (-1.0, 1.0)
_ZOOM_POSITION_RANGE = (0.0, 1.0)

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

_GET_SERVICES = f'<GetServices xmlns="{_DEVICE_NS}"/>'
_GET_NODES = f'<GetNodes xmlns="{_PTZ_NS}"/>'
_MEDIA1_GET_PROFILES = f'<GetProfiles xmlns="{_MEDIA1_NS}"/>'
_MEDIA2_GET_PROFILES = (
    f'<GetProfiles xmlns="{_MEDIA2_NS}"><Type>All</Type></GetProfiles>'
)


def _encode_xml(value: str) -> str:
    """Escape text for use inside an XML element.

    Matches the TypeScript reference's ``encodeXml`` (src/onvif/deviceClient.ts)
    byte for byte: unlike ``xml.sax.saxutils.escape`` (which only encodes
    ``&``, ``<``, ``>``), this also encodes ``"`` and ``'`` so a profile
    token carrying either character produces identical request bytes across
    languages. Defined locally rather than in onvif.py: onvif.py's own
    ``escape()`` calls are out of scope for this task.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@dataclass(frozen=True)
class PtzVector:
    x: float
    y: float


@dataclass(frozen=True)
class PtzSessionOptions:
    host: str
    user: str = ""
    password: str = ""
    profile_token: str | None = None
    device_urls: list[str] | None = None
    timeout: float = 8.0
    default_move_timeout_ms: float = _DEFAULT_MOVE_TIMEOUT_MS


@dataclass(frozen=True)
class PtzStatus:
    pan_tilt: PtzVector | None = None
    zoom: float | None = None
    pan_tilt_move_status: str | None = None
    zoom_move_status: str | None = None
    utc_time: str | None = None
    # Deliberately no error field: GetStatusResponse carries a camera-supplied
    # <Error> string; it must never surface in PtzStatus, in any form.


class _PtzResponseError(ValueError):
    """A sanitized invalid-response or SOAP-fault error."""

    def __init__(
        self, kind: str, message: str, *, fault_code: str | None = None
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


def _canonical_protocol_fault_code(
    value: str | None, soap_ns: str
) -> str | None:
    normalized = _xml_scalar(value)
    if not normalized:
        return None
    local = normalized.rsplit(":", 1)[-1]
    allowed = ["ActionNotSupported"]
    if soap_ns == _SOAP11_NS:
        allowed.extend(("VersionMismatch", "MustUnderstand", "Client", "Server"))
    elif soap_ns == _SOAP12_NS:
        allowed.extend(
            (
                "VersionMismatch",
                "MustUnderstand",
                "DataEncodingUnknown",
                "Sender",
                "Receiver",
            )
        )
    return next(
        (
            canonical
            for canonical in allowed
            if local.casefold() == canonical.casefold()
        ),
        None,
    )


def _canonical_fault_code(
    values: tuple[str | None, ...], deepest_code: str | None, soap_ns: str
) -> str:
    joined = " ".join(_xml_scalar(value) or "" for value in values)
    for canonical, pattern in _AUTH_FAULT_PATTERNS:
        if re.search(pattern, joined, re.IGNORECASE):
            return canonical
    return _canonical_protocol_fault_code(deepest_code, soap_ns) or "Fault"


def _raise_fault(fault: ElementTree.Element, soap_ns: str) -> None:
    values: list[str | None] = []
    deepest_code: str | None = None
    if soap_ns == _SOAP12_NS:
        code = _child(fault, soap_ns, "Code")
        while code is not None:
            value = _child(code, soap_ns, "Value")
            text = value.text if value is not None else None
            values.append(text)
            if text is not None:
                deepest_code = text
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
        deepest_code = values[0]
    canonical = _canonical_fault_code(tuple(values), deepest_code, soap_ns)
    raise _PtzResponseError(
        "fault", f"SOAP Fault: {canonical}", fault_code=canonical
    )


def _soap_operation(
    xml: bytes | str, namespace: str, operation: str, description: str
) -> ElementTree.Element:
    try:
        root = _safe_xml_fromstring(xml)
    except (ElementTree.ParseError, TypeError, ValueError) as error:
        raise _PtzResponseError(
            "invalid", f"invalid {description} response"
        ) from error
    soap_ns, root_name = _tag_parts(root.tag)
    if root_name != "Envelope" or soap_ns not in (_SOAP11_NS, _SOAP12_NS):
        raise _PtzResponseError("invalid", f"invalid {description} response")
    body = _child(root, soap_ns, "Body")
    if body is None:
        raise _PtzResponseError("invalid", f"invalid {description} response")
    fault = _child(body, soap_ns, "Fault")
    if fault is not None:
        _raise_fault(fault, soap_ns)
    result = _child(body, namespace, operation)
    if result is None:
        raise _PtzResponseError("invalid", f"invalid {description} response")
    return result


def format_ptz_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("PTZ value must be finite")
    text = f"{value:.6f}"
    return "0.000000" if text == "-0.000000" else text


def format_ptz_duration(milliseconds: float) -> str:
    if not math.isfinite(milliseconds) or milliseconds <= 0:
        raise ValueError("PTZ timeout must be finite and greater than 0")
    return f"PT{milliseconds / 1000:.3f}S"


def _require_finite_in_range(value: float, value_range: tuple[float, float]) -> float:
    if not math.isfinite(value):
        raise ValueError("PTZ value must be finite")
    low, high = value_range
    if value < low or value > high:
        raise ValueError("PTZ value must be within its valid range")
    return value


def _validated_vector(
    vector: PtzVector | None, value_range: tuple[float, float]
) -> PtzVector | None:
    if vector is None:
        return None
    return PtzVector(
        _require_finite_in_range(vector.x, value_range),
        _require_finite_in_range(vector.y, value_range),
    )


def _validated_zoom(
    zoom: float | None, value_range: tuple[float, float]
) -> float | None:
    if zoom is None:
        return None
    return _require_finite_in_range(zoom, value_range)


def _vector_xml(tag: str, pan_tilt: PtzVector | None, zoom: float | None) -> str:
    inner = ""
    if pan_tilt is not None:
        inner += (
            f'<PanTilt xmlns="{_SCHEMA_NS}" x="{format_ptz_number(pan_tilt.x)}"'
            f' y="{format_ptz_number(pan_tilt.y)}"/>'
        )
    if zoom is not None:
        inner += f'<Zoom xmlns="{_SCHEMA_NS}" x="{format_ptz_number(zoom)}"/>'
    return f"<{tag}>{inner}</{tag}>"


def _bool_xml(value: bool) -> str:
    return "true" if value else "false"


def _assert_move_shape(pan_tilt: PtzVector | None, zoom: float | None) -> None:
    if pan_tilt is None and zoom is None:
        raise ValueError("PTZ move requires pan/tilt or zoom")


_PTZ_SPACE_FIELDS: tuple[tuple[str, str], ...] = (
    ("AbsolutePanTiltPositionSpace", "absolute_pan_tilt"),
    ("AbsoluteZoomPositionSpace", "absolute_zoom"),
    ("RelativePanTiltTranslationSpace", "relative_pan_tilt"),
    ("RelativeZoomTranslationSpace", "relative_zoom"),
    ("ContinuousPanTiltVelocitySpace", "continuous_pan_tilt"),
    ("ContinuousZoomVelocitySpace", "continuous_zoom"),
)


def _ptz_spaces(element: ElementTree.Element) -> PtzSpaces:
    names = {_tag_parts(item.tag) for item in list(element)}
    return PtzSpaces(
        **{
            field: (_SCHEMA_NS, tag) in names
            for tag, field in _PTZ_SPACE_FIELDS
        }
    )


def _parse_node(element: ElementTree.Element) -> PtzNode | None:
    token = element.attrib.get("token")
    supported = _child(element, _SCHEMA_NS, "SupportedPTZSpaces")
    if not token or supported is None:
        return None
    spaces = _ptz_spaces(supported)
    maximum = _child(element, _SCHEMA_NS, "MaximumNumberOfPresets")
    home = _child(element, _SCHEMA_NS, "HomeSupported")
    auxiliary = sorted(
        {
            value
            for value in (
                _plain_text(item)
                for item in _children(element, _SCHEMA_NS, "AuxiliaryCommands")
            )
            if value
        }
    )
    return PtzNode(
        token=token,
        spaces=spaces,
        name=_plain_text(_child(element, _SCHEMA_NS, "Name")),
        maximum_presets=_strict_nonnegative_int32(
            maximum.text if maximum is not None else None
        ),
        home_supported=_strict_bool(home.text if home is not None else None),
        auxiliary_commands=tuple(auxiliary),
    )


def _parse_nodes_response(response: ElementTree.Element) -> tuple[PtzNode, ...]:
    nodes: list[PtzNode] = []
    for element in _children(response, _PTZ_NS, "PTZNode"):
        node = _parse_node(element)
        if node is None:
            raise _PtzResponseError("invalid", "invalid PTZ GetNodes response")
        nodes.append(node)
    return tuple(sorted(nodes, key=lambda node: node.token))


def _find_service_xaddr(
    response: ElementTree.Element, namespace: str
) -> str | None:
    for service in _children(response, _DEVICE_NS, "Service"):
        service_namespace = _plain_text(_child(service, _DEVICE_NS, "Namespace"))
        if service_namespace != namespace:
            continue
        xaddr = _plain_text(_child(service, _DEVICE_NS, "XAddr"))
        if xaddr:
            return xaddr
    return None


def _find_media1_profile_token(response: ElementTree.Element) -> str | None:
    for profile in _children(response, _MEDIA1_NS, "Profiles"):
        token = profile.attrib.get("token")
        has_ptz_configuration = (
            _child(profile, _SCHEMA_NS, "PTZConfiguration") is not None
        )
        if token and has_ptz_configuration:
            return token
    return None


# Mirrors capabilities.py's Media2 profile parsing: PTZ binding lives at
# Profiles/Configurations/PTZ, not directly on Profiles like Media1.
def _find_media2_profile_token(response: ElementTree.Element) -> str | None:
    for profile in _children(response, _MEDIA2_NS, "Profiles"):
        token = profile.attrib.get("token")
        configurations = _child(profile, _MEDIA2_NS, "Configurations")
        has_ptz = (
            configurations is not None
            and _child(configurations, _MEDIA2_NS, "PTZ") is not None
        )
        if token and has_ptz:
            return token
    return None


def _number_attribute(
    element: ElementTree.Element | None, name: str
) -> float | None:
    if element is None:
        return None
    raw = element.attrib.get(name)
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _parse_status_response(response: ElementTree.Element) -> PtzStatus:
    status = _child(response, _PTZ_NS, "PTZStatus")
    if status is None:
        return PtzStatus()
    position = _child(status, _SCHEMA_NS, "Position")
    pan_tilt_position = (
        _child(position, _SCHEMA_NS, "PanTilt") if position is not None else None
    )
    zoom_position = (
        _child(position, _SCHEMA_NS, "Zoom") if position is not None else None
    )
    x = _number_attribute(pan_tilt_position, "x")
    y = _number_attribute(pan_tilt_position, "y")
    zoom = _number_attribute(zoom_position, "x")
    move_status = _child(status, _SCHEMA_NS, "MoveStatus")
    pan_tilt_move_status = (
        _plain_text(_child(move_status, _SCHEMA_NS, "PanTilt"))
        if move_status is not None
        else None
    )
    zoom_move_status = (
        _plain_text(_child(move_status, _SCHEMA_NS, "Zoom"))
        if move_status is not None
        else None
    )
    utc_time = _plain_text(_child(status, _SCHEMA_NS, "UtcTime"))
    # Deliberately not read: <Error> — GetStatusResponse carries a
    # camera-supplied error string; it must never surface in PtzStatus.
    return PtzStatus(
        pan_tilt=PtzVector(x, y) if x is not None and y is not None else None,
        zoom=zoom,
        pan_tilt_move_status=pan_tilt_move_status,
        zoom_move_status=zoom_move_status,
        utc_time=utc_time,
    )


class PtzSession:
    """A guarded ONVIF PTZ movement control session.

    Opened once via :func:`open_ptz_session`, which caches the resolved
    profile token and the PTZ node's supported spaces. Every move method
    rejects an operation the camera did not advertise, and every method
    raises once the session is closed.
    """

    def __init__(
        self,
        device: OnvifDevice,
        ptz_xaddr: str,
        node: PtzNode,
        profile_token: str,
        default_move_timeout_ms: float,
    ) -> None:
        self._device = device
        self._ptz_xaddr = ptz_xaddr
        self._node = node
        self._profile_token = profile_token
        self._default_move_timeout_ms = default_move_timeout_ms
        self._closed = False

    @property
    def node(self) -> PtzNode:
        return self._node

    @property
    def profile_token(self) -> str:
        return self._profile_token

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("PTZ session is closed")

    def _profile_token_xml(self) -> str:
        return _encode_xml(self._profile_token)

    def _call(
        self, body: str, response_name: str, operation: str
    ) -> ElementTree.Element:
        raw = self._device.service_call(body, self._ptz_xaddr)
        return _soap_operation(raw.xml, _PTZ_NS, response_name, operation)

    def continuous_move(
        self,
        *,
        pan_tilt: PtzVector | None = None,
        zoom: float | None = None,
        timeout_ms: float | None = None,
    ) -> None:
        self._ensure_open()
        _assert_move_shape(pan_tilt, zoom)
        if pan_tilt is not None and not self._node.spaces.continuous_pan_tilt:
            raise RuntimeError("PTZ continuous pan/tilt is not supported")
        if zoom is not None and not self._node.spaces.continuous_zoom:
            raise RuntimeError("PTZ continuous zoom is not supported")
        validated_pan_tilt = _validated_vector(pan_tilt, _PAN_TILT_RANGE)
        validated_zoom = _validated_zoom(zoom, _ZOOM_GENERIC_RANGE)
        timeout = format_ptz_duration(
            self._default_move_timeout_ms if timeout_ms is None else timeout_ms
        )
        body = (
            f'<ContinuousMove xmlns="{_PTZ_NS}">'
            f"<ProfileToken>{self._profile_token_xml()}</ProfileToken>"
            + _vector_xml("Velocity", validated_pan_tilt, validated_zoom)
            + f"<Timeout>{timeout}</Timeout></ContinuousMove>"
        )
        self._call(body, "ContinuousMoveResponse", "PTZ ContinuousMove")

    def absolute_move(
        self,
        *,
        pan_tilt: PtzVector | None = None,
        zoom: float | None = None,
        speed_pan_tilt: PtzVector | None = None,
        speed_zoom: float | None = None,
    ) -> None:
        self._ensure_open()
        _assert_move_shape(pan_tilt, zoom)
        if pan_tilt is not None and not self._node.spaces.absolute_pan_tilt:
            raise RuntimeError("PTZ absolute pan/tilt is not supported")
        if zoom is not None and not self._node.spaces.absolute_zoom:
            raise RuntimeError("PTZ absolute zoom is not supported")
        validated_pan_tilt = _validated_vector(pan_tilt, _PAN_TILT_RANGE)
        validated_zoom = _validated_zoom(zoom, _ZOOM_POSITION_RANGE)
        has_speed = speed_pan_tilt is not None or speed_zoom is not None
        validated_speed_pan_tilt = _validated_vector(speed_pan_tilt, _PAN_TILT_RANGE)
        validated_speed_zoom = _validated_zoom(speed_zoom, _ZOOM_GENERIC_RANGE)
        speed_xml = (
            _vector_xml("Speed", validated_speed_pan_tilt, validated_speed_zoom)
            if has_speed
            else ""
        )
        body = (
            f'<AbsoluteMove xmlns="{_PTZ_NS}">'
            f"<ProfileToken>{self._profile_token_xml()}</ProfileToken>"
            + _vector_xml("Position", validated_pan_tilt, validated_zoom)
            + speed_xml
            + "</AbsoluteMove>"
        )
        self._call(body, "AbsoluteMoveResponse", "PTZ AbsoluteMove")

    def relative_move(
        self,
        *,
        pan_tilt: PtzVector | None = None,
        zoom: float | None = None,
        speed_pan_tilt: PtzVector | None = None,
        speed_zoom: float | None = None,
    ) -> None:
        self._ensure_open()
        _assert_move_shape(pan_tilt, zoom)
        if pan_tilt is not None and not self._node.spaces.relative_pan_tilt:
            raise RuntimeError("PTZ relative pan/tilt is not supported")
        if zoom is not None and not self._node.spaces.relative_zoom:
            raise RuntimeError("PTZ relative zoom is not supported")
        validated_pan_tilt = _validated_vector(pan_tilt, _PAN_TILT_RANGE)
        validated_zoom = _validated_zoom(zoom, _ZOOM_GENERIC_RANGE)
        has_speed = speed_pan_tilt is not None or speed_zoom is not None
        validated_speed_pan_tilt = _validated_vector(speed_pan_tilt, _PAN_TILT_RANGE)
        validated_speed_zoom = _validated_zoom(speed_zoom, _ZOOM_GENERIC_RANGE)
        speed_xml = (
            _vector_xml("Speed", validated_speed_pan_tilt, validated_speed_zoom)
            if has_speed
            else ""
        )
        body = (
            f'<RelativeMove xmlns="{_PTZ_NS}">'
            f"<ProfileToken>{self._profile_token_xml()}</ProfileToken>"
            + _vector_xml("Translation", validated_pan_tilt, validated_zoom)
            + speed_xml
            + "</RelativeMove>"
        )
        self._call(body, "RelativeMoveResponse", "PTZ RelativeMove")

    def stop(self, *, pan_tilt: bool = True, zoom: bool = True) -> None:
        self._ensure_open()
        body = (
            f'<Stop xmlns="{_PTZ_NS}">'
            f"<ProfileToken>{self._profile_token_xml()}</ProfileToken>"
            f"<PanTilt>{_bool_xml(pan_tilt)}</PanTilt>"
            f"<Zoom>{_bool_xml(zoom)}</Zoom></Stop>"
        )
        self._call(body, "StopResponse", "PTZ Stop")

    def get_status(self) -> PtzStatus:
        self._ensure_open()
        body = (
            f'<GetStatus xmlns="{_PTZ_NS}">'
            f"<ProfileToken>{self._profile_token_xml()}</ProfileToken></GetStatus>"
        )
        response = self._call(body, "GetStatusResponse", "PTZ GetStatus")
        return _parse_status_response(response)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.stop(pan_tilt=True, zoom=True)
        except Exception:
            # A failing Stop during close must never replace an error the
            # caller is already handling; best-effort only.
            pass
        self._closed = True


def open_ptz_session(options: PtzSessionOptions) -> PtzSession:
    """Open a PTZ control session.

    Experimental: physical movement is unverified against real PTZ
    hardware. Request construction, capability guarding, the device-side
    move timeout, and stop-on-close are covered by tests; that a camera
    actually moves as intended is not.
    """

    device = OnvifDevice(
        options.host,
        options.user,
        options.password,
        device_urls=options.device_urls,
        timeout=options.timeout,
    )
    device.connect()

    services_response = _soap_operation(
        device.service_call(_GET_SERVICES).xml,
        _DEVICE_NS,
        "GetServicesResponse",
        "GetServices",
    )
    ptz_xaddr = _find_service_xaddr(services_response, _PTZ_NS)
    if ptz_xaddr is None:
        raise RuntimeError("no ONVIF PTZ service")

    nodes_response = _soap_operation(
        device.service_call(_GET_NODES, ptz_xaddr).xml,
        _PTZ_NS,
        "GetNodesResponse",
        "PTZ GetNodes",
    )
    nodes = _parse_nodes_response(nodes_response)
    if not nodes:
        raise RuntimeError("no ONVIF PTZ node")
    node = nodes[0]

    profile_token = options.profile_token
    if profile_token is None:
        media1_response = _soap_operation(
            device.service_call(
                _MEDIA1_GET_PROFILES, device._required_media_url()
            ).xml,
            _MEDIA1_NS,
            "GetProfilesResponse",
            "Media1 GetProfiles",
        )
        profile_token = _find_media1_profile_token(media1_response)

        # Media1 came up empty: fall back to Media2, the same second source
        # capabilities.py already draws PTZ-capable profiles from. Only
        # costs a round trip when Media1 had no PTZ profile, and only when
        # Media2 is advertised at all.
        if profile_token is None:
            media2_xaddr = _find_service_xaddr(services_response, _MEDIA2_NS)
            if media2_xaddr is not None:
                media2_response = _soap_operation(
                    device.service_call(_MEDIA2_GET_PROFILES, media2_xaddr).xml,
                    _MEDIA2_NS,
                    "GetProfilesResponse",
                    "Media2 GetProfiles",
                )
                profile_token = _find_media2_profile_token(media2_response)

        if profile_token is None:
            raise RuntimeError("no ONVIF PTZ profile")

    return PtzSession(
        device,
        ptz_xaddr,
        node,
        profile_token,
        options.default_move_timeout_ms,
    )


__all__ = [
    "PtzSession",
    "PtzSessionOptions",
    "PtzStatus",
    "PtzVector",
    "format_ptz_duration",
    "format_ptz_number",
    "open_ptz_session",
]
