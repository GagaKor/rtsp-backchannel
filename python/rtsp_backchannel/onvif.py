"""ONVIF WS-Discovery and media profile stream URI lookup."""

from __future__ import annotations

import base64
import codecs
import datetime
import hashlib
import ipaddress
import math
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape


_MULTICAST_ADDRESS = "239.255.255.250"
_MULTICAST_PORT = 3702
_DEFAULT_CIDR_PORTS = (80, 8000, 443)
_DEFAULT_CIDR_CONCURRENCY = 64
_MAX_CIDR_HOSTS = 4096
_MAX_DISCOVERY_RESPONSE_BYTES = 1024 * 1024
_MAX_SOAP_RESPONSE_BYTES = 1024 * 1024
_DISCOVERY_READ_CHUNK_BYTES = 64 * 1024
_SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"
_WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
_WSU_NS = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
_PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_TLS_CONTEXT = ssl._create_unverified_context()
_EXPECTED_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    OverflowError,
    ElementTree.ParseError,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


@dataclass
class DiscoveredDevice:
    ip: str
    xaddrs: list[str]
    scopes: list[str]
    name: str | None = None
    hardware: str | None = None
    endpoint_reference: str | None = None


@dataclass(frozen=True)
class OnvifProfile:
    token: str
    name: str | None
    has_audio_encoder: bool
    has_audio_output: bool
    has_audio_source: bool


@dataclass(frozen=True)
class DeviceInfo:
    manufacturer: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None


@dataclass(frozen=True)
class StreamUri:
    profile_token: str
    profile_name: str | None
    uri: str


@dataclass(frozen=True)
class _SoapResponse:
    status_code: int
    xml: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _has_wide_xml_markup_prefix(
    xml: bytes, unit_size: int, byte_order: str
) -> bool:
    offset = 0
    while offset + unit_size <= len(xml):
        value = int.from_bytes(
            xml[offset : offset + unit_size], byte_order
        )
        if value in (0x09, 0x0A, 0x0D, 0x20):
            offset += unit_size
            continue
        return value == ord("<")
    return False


def _declaration_scan_text(xml: bytes | str) -> str:
    if not isinstance(xml, bytes):
        return xml
    if xml.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif xml.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif xml.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif _has_wide_xml_markup_prefix(xml, 4, "big"):
        encoding = "utf-32-be"
    elif _has_wide_xml_markup_prefix(xml, 4, "little"):
        encoding = "utf-32-le"
    elif _has_wide_xml_markup_prefix(xml, 2, "big"):
        encoding = "utf-16-be"
    elif _has_wide_xml_markup_prefix(xml, 2, "little"):
        encoding = "utf-16-le"
    else:
        encoding = "latin-1"
    try:
        return xml.decode(encoding)
    except UnicodeDecodeError:
        raise ElementTree.ParseError("invalid XML document") from None


def _contains_forbidden_declaration(xml: bytes | str) -> bool:
    text = _declaration_scan_text(xml)
    cursor = 0
    while True:
        markup = text.find("<", cursor)
        if markup < 0:
            return False
        if text.startswith("<!--", markup):
            end = text.find("-->", markup + 4)
            if end < 0:
                return False
            cursor = end + 3
            continue
        if text.startswith("<![CDATA[", markup):
            end = text.find("]]>", markup + 9)
            if end < 0:
                return False
            cursor = end + 3
            continue
        if text.startswith("<?", markup):
            end = text.find("?>", markup + 2)
            if end < 0:
                return False
            cursor = end + 2
            continue
        if text.startswith("<!", markup):
            name_start = markup + 2
            while name_start < len(text) and text[name_start] in " \t\r\n":
                name_start += 1
            name_end = name_start
            while name_end < len(text):
                character = text[name_end]
                if not (
                    "0" <= character <= "9"
                    or "A" <= character <= "Z"
                    or character == "_"
                    or "a" <= character <= "z"
                ):
                    break
                name_end += 1
            if text[name_start:name_end].upper() in ("DOCTYPE", "ENTITY"):
                return True
        cursor = markup + 1


def _safe_xml_fromstring(xml: bytes | str) -> ElementTree.Element:
    if _contains_forbidden_declaration(xml):
        raise ElementTree.ParseError(
            "DTD and entity declarations are not allowed"
        )
    return ElementTree.fromstring(xml)


def _first_text(element: ElementTree.Element, name: str) -> str | None:
    for candidate in element.iter():
        if _local_name(candidate.tag) == name and candidate.text is not None:
            return candidate.text.strip()
    return None


def _tag_parts(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return "", tag


def _direct_child(
    element: ElementTree.Element, namespace: str, name: str
) -> ElementTree.Element | None:
    expected = (namespace, name)
    return next(
        (child for child in list(element) if _tag_parts(child.tag) == expected),
        None,
    )


def _parse_device_information(xml: bytes | str) -> DeviceInfo:
    root = _safe_xml_fromstring(xml)
    soap_namespace, root_name = _tag_parts(root.tag)
    if root_name != "Envelope" or soap_namespace not in (
        _SOAP11_NS,
        _SOAP12_NS,
    ):
        raise RuntimeError("ONVIF authentication failed")
    body = _direct_child(root, soap_namespace, "Body")
    response = (
        _direct_child(body, _DEVICE_NS, "GetDeviceInformationResponse")
        if body is not None
        else None
    )
    if response is None:
        raise RuntimeError("ONVIF authentication failed")

    def value(name: str) -> str | None:
        child = _direct_child(response, _DEVICE_NS, name)
        if child is None or child.text is None:
            return None
        parsed = child.text.strip()
        return parsed or None

    return DeviceInfo(
        manufacturer=value("Manufacturer"),
        model=value("Model"),
        firmware=value("FirmwareVersion"),
        serial=value("SerialNumber"),
    )


def _sanitize_stream_uri(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise RuntimeError("invalid ONVIF stream URI") from error
    if parsed.scheme.lower() == "rtsp" and not hostname:
        raise RuntimeError("invalid ONVIF stream URI")
    if parsed.netloc and hostname:
        if port == 0:
            raise RuntimeError("invalid ONVIF stream URI")
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname
        if port is not None:
            netloc += f":{port}"
    else:
        netloc = parsed.netloc
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _scope_value(scopes: list[str], key: str) -> str | None:
    prefix = f"onvif://www.onvif.org/{key}/"
    for scope in scopes:
        if scope.lower().startswith(prefix):
            return unquote(scope[len(prefix) :])
    return None


def parse_probe_matches(
    xml: bytes | str, source_ip: str
) -> list[DiscoveredDevice]:
    """Parse every ONVIF ProbeMatch in a WS-Discovery datagram."""

    root = _safe_xml_fromstring(xml)
    devices = []
    for match in root.iter():
        if _local_name(match.tag) != "ProbeMatch":
            continue
        types = _first_text(match, "Types") or ""
        xaddrs = (_first_text(match, "XAddrs") or "").split()
        scopes = (_first_text(match, "Scopes") or "").split()
        is_onvif = (
            "NetworkVideoTransmitter" in types
            or any(scope.lower().startswith("onvif://") for scope in scopes)
            or any("/onvif/" in address.lower() for address in xaddrs)
        )
        if not is_onvif:
            continue
        devices.append(
            DiscoveredDevice(
                ip=source_ip,
                xaddrs=xaddrs,
                scopes=scopes,
                name=_scope_value(scopes, "name"),
                hardware=_scope_value(scopes, "hardware"),
                endpoint_reference=_first_text(match, "Address"),
            )
        )
    return devices


def _probe_message(message_id: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{message_id}</w:MessageID>"
        '<w:To e:mustUnderstand="true">'
        "urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        '<w:Action e:mustUnderstand="true">'
        "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></e:Body></e:Envelope>"
    ).encode("utf-8")


def _local_ipv4() -> list[str]:
    addresses: set[str] = set()
    try:
        results = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
        )
        addresses.update(result[4][0] for result in results)
    except OSError:
        pass

    route_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route_socket.connect((_MULTICAST_ADDRESS, _MULTICAST_PORT))
        addresses.add(route_socket.getsockname()[0])
    except OSError:
        pass
    finally:
        route_socket.close()

    usable = []
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if not parsed.is_loopback and not parsed.is_unspecified:
            usable.append(address)
    return sorted(usable, key=ipaddress.ip_address) or ["0.0.0.0"]


def _probe_interface(source, payload, deadline, on_message):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((source, 0))
        if source != "0.0.0.0":
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(source),
            )
        for _ in range(3):
            sock.sendto(payload, (_MULTICAST_ADDRESS, _MULTICAST_PORT))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                response, remote = sock.recvfrom(65_535)
            except socket.timeout:
                break
            on_message(response, remote[0])
    finally:
        sock.close()


def _validated_interfaces(interfaces) -> list[str]:
    candidates = _local_ipv4() if interfaces is None else interfaces
    result = []
    for candidate in candidates:
        parsed = ipaddress.ip_address(candidate)
        if not isinstance(parsed, ipaddress.IPv4Address):
            raise ValueError(f"interface must be an IPv4 address: {candidate}")
        normalized = str(parsed)
        if normalized not in result:
            result.append(normalized)
    return result


def _merge_device(target: DiscoveredDevice, incoming: DiscoveredDevice) -> None:
    for xaddr in incoming.xaddrs:
        if xaddr not in target.xaddrs:
            target.xaddrs.append(xaddr)
    for scope in incoming.scopes:
        if scope not in target.scopes:
            target.scopes.append(scope)
    target.name = target.name or incoming.name
    target.hardware = target.hardware or incoming.hardware
    target.endpoint_reference = (
        target.endpoint_reference or incoming.endpoint_reference
    )


def _cidr_hosts(cidrs: list[str]) -> list[str]:
    addresses: set[ipaddress.IPv4Address] = set()
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError as error:
            raise ValueError(f"invalid IPv4 CIDR: {cidr}") from error
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError(f"invalid IPv4 CIDR: {cidr}")
        host_count = (
            network.num_addresses - 2
            if network.prefixlen <= 30
            else network.num_addresses
        )
        if host_count > _MAX_CIDR_HOSTS:
            raise ValueError(
                f"CIDR discovery is limited to {_MAX_CIDR_HOSTS} IPv4 hosts"
            )
        addresses.update(network.hosts())
        if len(addresses) > _MAX_CIDR_HOSTS:
            raise ValueError(
                f"CIDR discovery is limited to {_MAX_CIDR_HOSTS} IPv4 hosts"
            )
    return [str(address) for address in sorted(addresses)]


def _validated_ports(ports: list[int] | None) -> list[int]:
    values = list(dict.fromkeys(_DEFAULT_CIDR_PORTS if ports is None else ports))
    if not values or any(
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
        for port in values
    ):
        raise ValueError("ports must contain integers between 1 and 65535")
    return values


def _device_service_url(ip: str, port: int) -> str:
    secure = port == 443
    default_port = 443 if secure else 80
    authority = ip if port == default_port else f"{ip}:{port}"
    return (
        f"{'https' if secure else 'http'}://{authority}"
        "/onvif/device_service"
    )


def _discovery_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_TLS_CONTEXT),
        _NoRedirectHandler(),
    )


def _set_response_timeout(response, timeout: float) -> None:
    try:
        response.fp.raw._sock.settimeout(timeout)
    except (AttributeError, OSError):
        pass


def _read_response_chunk(response, size: int) -> bytes:
    read_one = getattr(response, "read1", None)
    if callable(read_one):
        return read_one(size)
    return response.read(size)


def _bounded_response_body(
    response,
    deadline: float,
    *,
    max_bytes: int | None = None,
    context: str = "ONVIF discovery",
) -> bytes:
    if max_bytes is None:
        max_bytes = _MAX_DISCOVERY_RESPONSE_BYTES
    content_length_context = (
        "discovery" if context == "ONVIF discovery" else context
    )
    headers = getattr(response, "headers", None)
    if headers is None:
        raw_lengths = []
    elif hasattr(headers, "get_all"):
        raw_lengths = headers.get_all("Content-Length", [])
    else:
        raw_lengths = [
            value
            for key, value in headers.items()
            if str(key).lower() == "content-length"
        ]
    if any(not isinstance(value, str) for value in raw_lengths):
        raise RuntimeError(
            f"invalid {content_length_context} response Content-Length"
        )
    lengths = [
        value.strip()
        for raw_value in raw_lengths
        for value in raw_value.split(",")
    ]
    if lengths:
        if any(not value.isdecimal() for value in lengths):
            raise RuntimeError(
                f"invalid {content_length_context} response Content-Length"
            )
        parsed_lengths = {int(value) for value in lengths}
        if len(parsed_lengths) != 1:
            raise RuntimeError(
                f"conflicting {content_length_context} response Content-Length"
            )
        if parsed_lengths.pop() > max_bytes:
            raise RuntimeError(f"{context} response is too large")

    chunks = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{context} deadline exceeded")
        _set_response_timeout(response, remaining)
        try:
            chunk = _read_response_chunk(
                response,
                min(
                    _DISCOVERY_READ_CHUNK_BYTES,
                    max_bytes + 1 - total,
                )
            )
        except TimeoutError as error:
            raise TimeoutError(f"{context} deadline exceeded") from error
        if time.monotonic() > deadline:
            raise TimeoutError(f"{context} deadline exceeded")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError(f"{context} response is too large")


def _probe_device_service(url: str, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("ONVIF discovery deadline exceeded")
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f'<s:Header></s:Header><s:Body><GetSystemDateAndTime xmlns="{_DEVICE_NS}"/>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=envelope,
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        response = _discovery_opener().open(request, timeout=remaining)
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(
            f"ONVIF discovery returned HTTP {error.code}"
        ) from error
    with response:
        status = response.getcode()
        if not 200 <= status < 300:
            raise RuntimeError(f"ONVIF discovery returned HTTP {status}")
        root = _safe_xml_fromstring(_bounded_response_body(response, deadline))
    if not any(
        _local_name(candidate.tag) == "UTCDateTime" for candidate in root.iter()
    ):
        raise RuntimeError("no UTCDateTime in ONVIF discovery response")


def _probe_cidr_host(
    ip: str, ports: list[int], deadline: float
) -> DiscoveredDevice | None:
    xaddrs = []
    for port in ports:
        if time.monotonic() >= deadline:
            break
        url = _device_service_url(ip, port)
        try:
            _probe_device_service(url, deadline)
            xaddrs.append(url)
        except _EXPECTED_OPERATION_ERRORS:
            continue
    if not xaddrs:
        return None
    return DiscoveredDevice(ip=ip, xaddrs=xaddrs, scopes=[])


def _discover_cidrs(
    *,
    cidrs: list[str],
    ports: list[int] | None,
    timeout: float,
    concurrency: int,
) -> list[DiscoveredDevice]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            "timeout must be finite and greater than 0 for CIDR discovery"
        )
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 256
    ):
        raise ValueError("concurrency must be an integer between 1 and 256")
    hosts = _cidr_hosts(cidrs)
    selected_ports = _validated_ports(ports)
    if not hosts:
        return []
    deadline = time.monotonic() + timeout
    with ThreadPoolExecutor(
        max_workers=min(concurrency, len(hosts))
    ) as executor:
        devices = executor.map(
            lambda host: _probe_cidr_host(host, selected_ports, deadline), hosts
        )
        return [device for device in devices if device is not None]


def discover_devices(
    *,
    timeout: float = 3.0,
    interfaces: list[str] | None = None,
    cidrs: list[str] | None = None,
    ports: list[int] | None = None,
    concurrency: int = _DEFAULT_CIDR_CONCURRENCY,
) -> list[DiscoveredDevice]:
    """Discover local ONVIF devices or actively scan selected targets.

    Without ``cidrs``, WS-Discovery runs on detected or selected local
    interfaces. Each ``cidrs`` entry may be an IPv4 CIDR or one IPv4 address;
    all entries are merged and overlapping hosts are probed once.
    """

    if cidrs:
        if interfaces:
            raise ValueError("interfaces cannot be combined with cidrs")
        return _discover_cidrs(
            cidrs=cidrs,
            ports=ports,
            timeout=timeout,
            concurrency=concurrency,
        )

    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and 0 or greater")
    sources = _validated_interfaces(interfaces)
    if not sources:
        return []

    deadline = time.monotonic() + timeout
    payload = _probe_message(str(uuid.uuid4()))
    found: dict[str, DiscoveredDevice] = {}
    lock = threading.Lock()

    def on_message(response, source_ip):
        try:
            matches = parse_probe_matches(response, source_ip)
        except ElementTree.ParseError:
            return
        with lock:
            for incoming in matches:
                current = found.get(incoming.ip)
                if current is None:
                    found[incoming.ip] = incoming
                else:
                    _merge_device(current, incoming)

    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = [
            executor.submit(
                _probe_interface, source, payload, deadline, on_message
            )
            for source in sources
        ]
        for future in futures:
            try:
                future.result()
            except OSError:
                continue

    return sorted(found.values(), key=lambda device: ipaddress.ip_address(device.ip))


def parse_profiles(xml: bytes | str) -> list[OnvifProfile]:
    root = _safe_xml_fromstring(xml)
    profiles = []
    for candidate in root.iter():
        if _local_name(candidate.tag) != "Profiles":
            continue
        token = candidate.attrib.get("token")
        if not token:
            continue
        child_names = {_local_name(child.tag) for child in candidate.iter()}
        profiles.append(
            OnvifProfile(
                token=token,
                name=_first_text(candidate, "Name"),
                has_audio_encoder="AudioEncoderConfiguration" in child_names,
                has_audio_output="AudioOutputConfiguration" in child_names,
                has_audio_source="AudioSourceConfiguration" in child_names,
            )
        )
    return profiles


def _validate_service_url(value: str) -> None:
    if not isinstance(value, str) or any(
        ord(character) <= 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise RuntimeError("invalid ONVIF service URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("invalid ONVIF service URL") from None
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise RuntimeError("invalid ONVIF service URL")


def _soap_response(
    url: str,
    body: str,
    header: str,
    timeout: float,
    *,
    stop_on_auth_error: bool = False,
) -> _SoapResponse:
    _validate_service_url(url)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than 0")
    deadline = time.monotonic() + timeout
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body>"
        "</s:Envelope>"
    )
    request = urllib.request.Request(
        url,
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("ONVIF SOAP deadline exceeded")
    try:
        response = urllib.request.urlopen(
            request, timeout=remaining, context=_TLS_CONTEXT
        )
    except urllib.error.HTTPError as error:
        if stop_on_auth_error and error.code in (401, 403):
            error.close()
            return _SoapResponse(error.code, "")
        with error:
            xml = _bounded_response_body(
                error,
                deadline,
                max_bytes=_MAX_SOAP_RESPONSE_BYTES,
                context="ONVIF SOAP",
            ).decode("utf-8", "replace")
        return _SoapResponse(error.code, xml)
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise TimeoutError("ONVIF SOAP deadline exceeded") from error
        raise
    except TimeoutError as error:
        raise TimeoutError("ONVIF SOAP deadline exceeded") from error
    getcode = getattr(response, "getcode", None)
    status = (
        getcode()
        if callable(getcode)
        else getattr(response, "status", 200)
    )
    status = status if isinstance(status, int) else 200
    if stop_on_auth_error and status in (401, 403):
        response.close()
        return _SoapResponse(status, "")
    with response:
        xml = _bounded_response_body(
            response,
            deadline,
            max_bytes=_MAX_SOAP_RESPONSE_BYTES,
            context="ONVIF SOAP",
        ).decode("utf-8", "replace")
    return _SoapResponse(status, xml)


def _soap_request(url: str, body: str, header: str, timeout: float) -> str:
    return _soap_response(url, body, header, timeout).xml


def _wsse_header(user: str, password: str, when: datetime.datetime) -> str:
    nonce = os.urandom(16)
    created = when.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    digest = hashlib.sha1(
        nonce + created.encode("utf-8") + password.encode("utf-8")
    ).digest()
    return (
        f'<wsse:Security xmlns:wsse="{_WSSE_NS}" xmlns:wsu="{_WSU_NS}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{escape(user)}</wsse:Username>"
        f'<wsse:Password Type="{_PASSWORD_DIGEST}">'
        f"{base64.b64encode(digest).decode('ascii')}</wsse:Password>"
        f"<wsse:Nonce>{base64.b64encode(nonce).decode('ascii')}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


class OnvifDevice:
    def __init__(
        self,
        host: str,
        user: str = "",
        password: str = "",
        *,
        device_urls: list[str] | None = None,
        timeout: float = 8.0,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.device_urls = list(device_urls) if device_urls else None
        self.timeout = timeout
        self.device_url: str | None = None
        self.media_url: str | None = None
        self.clock_offset = datetime.timedelta()

    def _candidates(self) -> list[str]:
        if self.device_urls:
            return self.device_urls
        return [
            f"http://{self.host}/onvif/device_service",
            f"https://{self.host}/onvif/device_service",
            f"http://{self.host}:8000/onvif/device_service",
        ]

    def _request(
        self,
        url: str,
        body: str,
        *,
        authenticated: bool,
        include_status: bool,
    ) -> str | _SoapResponse:
        _validate_service_url(url)
        header = ""
        if authenticated and (self.user or self.password):
            now = datetime.datetime.now(datetime.timezone.utc) + self.clock_offset
            header = _wsse_header(self.user, self.password, now)
        if include_status:
            return _soap_response(
                url,
                body,
                header,
                self.timeout,
                stop_on_auth_error=True,
            )
        return _soap_request(url, body, header, self.timeout)

    def _call(self, url: str, body: str, *, authenticated: bool = True) -> str:
        response = self._request(
            url,
            body,
            authenticated=authenticated,
            include_status=False,
        )
        assert isinstance(response, str)
        return response

    def _call_response(
        self, url: str, body: str, *, authenticated: bool = True
    ) -> _SoapResponse:
        response = self._request(
            url,
            body,
            authenticated=authenticated,
            include_status=True,
        )
        assert isinstance(response, _SoapResponse)
        if response.status_code in (401, 403):
            raise RuntimeError("ONVIF authentication failed")
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response

    def _system_time(self, url: str) -> datetime.datetime:
        xml = self._call_response(
            url,
            f'<GetSystemDateAndTime xmlns="{_DEVICE_NS}"/>',
            authenticated=False,
        ).xml
        root = _safe_xml_fromstring(xml)
        utc = next(
            (
                candidate
                for candidate in root.iter()
                if _local_name(candidate.tag) == "UTCDateTime"
            ),
            None,
        )
        if utc is None:
            raise RuntimeError("no UTCDateTime in ONVIF response")

        def number(name, default=None):
            value = _first_text(utc, name)
            return int(value) if value is not None else default

        year = number("Year")
        month = number("Month")
        day = number("Day")
        if year is None or month is None or day is None:
            raise RuntimeError("incomplete UTCDateTime in ONVIF response")
        return datetime.datetime(
            year,
            month,
            day,
            number("Hour", 0),
            number("Minute", 0),
            number("Second", 0),
            tzinfo=datetime.timezone.utc,
        )

    def connect(self) -> DeviceInfo:
        for url in self._candidates():
            try:
                camera_time = self._system_time(url)
                local_time = datetime.datetime.now(datetime.timezone.utc)
                self.clock_offset = camera_time - local_time
                info = self._call_response(
                    url,
                    f'<GetDeviceInformation xmlns="{_DEVICE_NS}"/>',
                ).xml
                device_info = _parse_device_information(info)
                self.device_url = url
                self.media_url = self._media_service_url(url)
                return device_info
            except _EXPECTED_OPERATION_ERRORS:
                continue
        raise RuntimeError("ONVIF connect failed") from None

    def _media_service_url(self, device_url: str) -> str:
        xml = self._call_response(
            device_url,
            f'<GetCapabilities xmlns="{_DEVICE_NS}">'
            "<Category>Media</Category></GetCapabilities>",
        ).xml
        root = _safe_xml_fromstring(xml)
        for candidate in root.iter():
            if _local_name(candidate.tag) != "Media":
                continue
            xaddr = _first_text(candidate, "XAddr")
            if xaddr:
                return xaddr
        return device_url.replace("device_service", "media_service")

    def _required_media_url(self) -> str:
        if self.media_url is None:
            raise RuntimeError("call connect() first")
        return self.media_url

    def _required_device_url(self) -> str:
        if self.device_url is None:
            raise RuntimeError("call connect() first")
        return self.device_url

    def read_only_call(
        self, body: str, endpoint: str | None = None
    ) -> _SoapResponse:
        device_url = self._required_device_url()
        response = self._request(
            endpoint or device_url,
            body,
            authenticated=True,
            include_status=True,
        )
        assert isinstance(response, _SoapResponse)
        return response

    def get_profiles(self) -> list[OnvifProfile]:
        xml = self._call(
            self._required_media_url(),
            f'<GetProfiles xmlns="{_MEDIA_NS}"/>',
        )
        return parse_profiles(xml)

    def get_stream_uri(self, profile_token: str) -> str:
        body = (
            f'<GetStreamUri xmlns="{_MEDIA_NS}"><StreamSetup>'
            f'<Stream xmlns="{_SCHEMA_NS}">RTP-Unicast</Stream>'
            f'<Transport xmlns="{_SCHEMA_NS}"><Protocol>RTSP</Protocol>'
            "</Transport></StreamSetup>"
            f"<ProfileToken>{escape(profile_token)}</ProfileToken>"
            "</GetStreamUri>"
        )
        xml = self._call(self._required_media_url(), body)
        root = _safe_xml_fromstring(xml)
        for candidate in root.iter():
            if _local_name(candidate.tag) == "Uri" and candidate.text:
                return _sanitize_stream_uri(candidate.text)
        raise RuntimeError(f"no stream URI for profile {profile_token}")


def get_stream_uris(
    *,
    host: str,
    user: str = "",
    password: str = "",
    device_urls: list[str] | None = None,
    timeout: float = 8.0,
) -> list[StreamUri]:
    """Return every ONVIF media profile URI without embedding credentials."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than 0")
    device = OnvifDevice(
        host,
        user,
        password,
        device_urls=device_urls,
        timeout=timeout,
    )
    device.connect()
    return [
        StreamUri(
            profile_token=profile.token,
            profile_name=profile.name,
            uri=_sanitize_stream_uri(device.get_stream_uri(profile.token)),
        )
        for profile in device.get_profiles()
    ]


__all__ = [
    "DeviceInfo",
    "DiscoveredDevice",
    "OnvifDevice",
    "OnvifProfile",
    "StreamUri",
    "discover_devices",
    "get_stream_uris",
    "parse_probe_matches",
    "parse_profiles",
]
