use serde::Serialize;
use std::collections::BTreeSet;
use std::fmt;
use std::time::Duration;

use super::DeviceInfo;

pub(super) const DEVICE_NS: &str = "http://www.onvif.org/ver10/device/wsdl";
pub(super) const SCHEMA_NS: &str = "http://www.onvif.org/ver10/schema";
pub(super) const MEDIA1_NS: &str = "http://www.onvif.org/ver10/media/wsdl";
pub(super) const MEDIA2_NS: &str = "http://www.onvif.org/ver20/media/wsdl";
pub(super) const PTZ_NS: &str = "http://www.onvif.org/ver20/ptz/wsdl";
pub(super) const EVENTS_NS: &str = "http://www.onvif.org/ver10/events/wsdl";
const SOAP11_NS: &str = "http://schemas.xmlsoap.org/soap/envelope/";
const SOAP12_NS: &str = "http://www.w3.org/2003/05/soap-envelope";
const PROFILE_SCOPE_PREFIX: &str = "onvif://www.onvif.org/Profile/";
const MAX_XML_ELEMENT_DEPTH: usize = 64;

const GET_SCOPES: &str = "<GetScopes xmlns=\"http://www.onvif.org/ver10/device/wsdl\"/>";
const GET_SERVICES: &str = concat!(
    "<GetServices xmlns=\"http://www.onvif.org/ver10/device/wsdl\">",
    "<IncludeCapability>true</IncludeCapability></GetServices>"
);
const GET_ALL_CAPABILITIES: &str = concat!(
    "<GetCapabilities xmlns=\"http://www.onvif.org/ver10/device/wsdl\">",
    "<Category>All</Category></GetCapabilities>"
);
const MEDIA1_GET_PROFILES: &str = "<GetProfiles xmlns=\"http://www.onvif.org/ver10/media/wsdl\"/>";
const MEDIA2_GET_PROFILES: &str = concat!(
    "<GetProfiles xmlns=\"http://www.onvif.org/ver20/media/wsdl\">",
    "<Type>All</Type></GetProfiles>"
);
const MEDIA2_GET_OPTIONS: &str = concat!(
    "<GetVideoEncoderConfigurationOptions ",
    "xmlns=\"http://www.onvif.org/ver20/media/wsdl\"/>"
);
const PTZ_GET_CAPABILITIES: &str =
    "<GetServiceCapabilities xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"/>";
const PTZ_GET_NODES: &str = "<GetNodes xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"/>";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CameraCapabilityOptions {
    pub host: String,
    pub user: String,
    pub password: String,
    pub device_urls: Vec<String>,
    pub timeout: Duration,
}

impl CameraCapabilityOptions {
    pub fn new(
        host: impl Into<String>,
        user: impl Into<String>,
        password: impl Into<String>,
    ) -> Self {
        Self {
            host: host.into(),
            user: user.into(),
            password: password.into(),
            device_urls: Vec::new(),
            timeout: Duration::from_secs(8),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CameraCapabilityVersion {
    pub major: i32,
    pub minor: i32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CameraCapabilityService {
    pub namespace: String,
    pub xaddr: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<CameraCapabilityVersion>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CameraCapabilityWarning {
    pub operation: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CameraCapabilityProfile {
    pub token: String,
    pub source: String,
    pub has_audio_encoder: bool,
    pub has_audio_output: bool,
    pub has_audio_source: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ptz_configuration_token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ptz_node_token: Option<String>,
}

// PtzSpaces, PtzNode, and PtzServiceCapabilities live in ptz_types.rs so
// neither this module nor ptz.rs has to import the other; re-export them
// here so `capabilities::{PtzNode, PtzServiceCapabilities, PtzSpaces}` keeps
// resolving for onvif.rs's existing `pub use`.
pub use super::ptz_types::{PtzNode, PtzServiceCapabilities, PtzSpaces};

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PtzCapabilityReport {
    pub detected: Option<bool>,
    pub pan_tilt_supported: Option<bool>,
    pub zoom_supported: Option<bool>,
    pub profile_tokens: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub service_capabilities: Option<PtzServiceCapabilities>,
    pub nodes: Vec<PtzNode>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Media2CapabilityReport {
    pub detected: Option<bool>,
    pub encodings: Vec<String>,
    pub h265_supported: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CameraCapabilityReport {
    pub device: DeviceInfo,
    pub scopes: Vec<String>,
    pub declared_profiles: Vec<String>,
    pub service_discovery: String,
    pub services: Vec<CameraCapabilityService>,
    pub profiles: Vec<CameraCapabilityProfile>,
    pub ptz: PtzCapabilityReport,
    pub media2: Media2CapabilityReport,
    pub warnings: Vec<CameraCapabilityWarning>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ScopesResult {
    scopes: Vec<String>,
    declared_profiles: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ServicesResult {
    services: Vec<CameraCapabilityService>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct PtzNodesResult {
    nodes: Vec<PtzNode>,
    pan_tilt_supported: bool,
    zoom_supported: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum ResponseErrorKind {
    Invalid,
    Fault,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ResponseError {
    kind: ResponseErrorKind,
    message: String,
    fault_code: Option<String>,
}

impl ResponseError {
    fn invalid(message: impl Into<String>) -> Self {
        Self {
            kind: ResponseErrorKind::Invalid,
            message: message.into(),
            fault_code: None,
        }
    }

    fn fault(code: &'static str) -> Self {
        Self {
            kind: ResponseErrorKind::Fault,
            message: format!("SOAP Fault: {code}"),
            fault_code: Some(code.to_owned()),
        }
    }
}

impl fmt::Display for ResponseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ResponseError {}

fn starts_with_ascii_case_insensitive(input: &[u8], prefix: &[u8]) -> bool {
    input.len() >= prefix.len() && input[..prefix.len()].eq_ignore_ascii_case(prefix)
}

fn find_bytes(haystack: &[u8], needle: &[u8], start: usize) -> Option<usize> {
    haystack
        .get(start..)?
        .windows(needle.len())
        .position(|window| window == needle)
        .map(|offset| start + offset)
}

fn has_forbidden_declaration(xml: &str) -> bool {
    let bytes = xml.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let rest = &bytes[index..];
        if rest.starts_with(b"<!--") {
            index = find_bytes(bytes, b"-->", index + 4).map_or(bytes.len(), |end| end + 3);
        } else if rest.starts_with(b"<![CDATA[") {
            index = find_bytes(bytes, b"]]>", index + 9).map_or(bytes.len(), |end| end + 3);
        } else if rest.starts_with(b"<?") {
            index = find_bytes(bytes, b"?>", index + 2).map_or(bytes.len(), |end| end + 2);
        } else if starts_with_ascii_case_insensitive(rest, b"<!DOCTYPE")
            || starts_with_ascii_case_insensitive(rest, b"<!ENTITY")
        {
            return true;
        } else {
            index += 1;
        }
    }
    false
}

fn xml_element_depth(xml: &str) -> Option<usize> {
    let bytes = xml.as_bytes();
    let mut index = 0;
    let mut depth = 0usize;
    let mut maximum = 0usize;
    while index < bytes.len() {
        if bytes[index] != b'<' {
            index += 1;
            continue;
        }
        let rest = &bytes[index..];
        if rest.starts_with(b"<!--") {
            index = find_bytes(bytes, b"-->", index + 4)? + 3;
            continue;
        }
        if rest.starts_with(b"<![CDATA[") {
            index = find_bytes(bytes, b"]]>", index + 9)? + 3;
            continue;
        }
        if rest.starts_with(b"<?") {
            index = find_bytes(bytes, b"?>", index + 2)? + 2;
            continue;
        }
        if rest.starts_with(b"</") {
            let end = find_bytes(bytes, b">", index + 2)?;
            depth = depth.checked_sub(1)?;
            index = end + 1;
            continue;
        }
        if rest.starts_with(b"<!") {
            return None;
        }
        let mut cursor = index + 1;
        let mut quote = None;
        let end = loop {
            let byte = *bytes.get(cursor)?;
            if let Some(delimiter) = quote {
                if byte == delimiter {
                    quote = None;
                }
            } else if matches!(byte, b'\'' | b'"') {
                quote = Some(byte);
            } else if byte == b'>' {
                break cursor;
            }
            cursor += 1;
        };
        let self_closing = bytes[index + 1..end]
            .iter()
            .rev()
            .find(|byte| !matches!(byte, b' ' | b'\t' | b'\r' | b'\n'))
            == Some(&b'/');
        let element_depth = depth.checked_add(1)?;
        maximum = maximum.max(element_depth);
        if maximum > MAX_XML_ELEMENT_DEPTH {
            return None;
        }
        if !self_closing {
            depth = element_depth;
        }
        index = end + 1;
    }
    (depth == 0).then_some(maximum)
}

pub(super) fn parse_document(xml: &str) -> Result<roxmltree::Document<'_>, ResponseError> {
    if has_forbidden_declaration(xml) {
        return Err(ResponseError::invalid("invalid XML document"));
    }
    xml_element_depth(xml).ok_or_else(|| ResponseError::invalid("invalid XML document"))?;
    roxmltree::Document::parse(xml).map_err(|_| ResponseError::invalid("invalid XML document"))
}

fn is_element(node: roxmltree::Node<'_, '_>, namespace: &str, local: &str) -> bool {
    node.is_element()
        && node.tag_name().namespace().unwrap_or_default() == namespace
        && node.tag_name().name() == local
}

fn child<'document, 'input>(
    parent: roxmltree::Node<'document, 'input>,
    namespace: &str,
    local: &str,
) -> Option<roxmltree::Node<'document, 'input>> {
    parent
        .children()
        .find(|node| is_element(*node, namespace, local))
}

fn children<'document, 'input>(
    parent: roxmltree::Node<'document, 'input>,
    namespace: &'document str,
    local: &'document str,
) -> impl Iterator<Item = roxmltree::Node<'document, 'input>> + 'document {
    parent
        .children()
        .filter(move |node| is_element(*node, namespace, local))
}

fn plain_text(node: Option<roxmltree::Node<'_, '_>>) -> Option<String> {
    let value = node?.text()?.trim();
    (!value.is_empty()).then(|| value.to_owned())
}

fn xml_scalar(value: Option<&str>) -> Option<String> {
    let mut normalized = String::new();
    let mut whitespace = false;
    for character in value?.chars() {
        if matches!(character, ' ' | '\t' | '\r' | '\n') {
            whitespace = !normalized.is_empty();
        } else {
            if whitespace {
                normalized.push(' ');
                whitespace = false;
            }
            normalized.push(character);
        }
    }
    (!normalized.is_empty()).then_some(normalized)
}

fn strict_bool(value: Option<&str>) -> Option<bool> {
    match xml_scalar(value)?.as_str() {
        "true" | "1" => Some(true),
        "false" | "0" => Some(false),
        _ => None,
    }
}

fn strict_nonnegative_int32(value: Option<&str>) -> Option<i32> {
    let value = xml_scalar(value)?;
    let bytes = value.as_bytes();
    let digits = match bytes.first() {
        Some(b'+' | b'-') => &bytes[1..],
        Some(_) => bytes,
        None => return None,
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let significant = digits
        .iter()
        .position(|digit| *digit != b'0')
        .map_or(&digits[digits.len() - 1..], |position| &digits[position..]);
    if significant.len() > 10 {
        return None;
    }
    let magnitude = std::str::from_utf8(significant).ok()?.parse::<i64>().ok()?;
    let parsed = if bytes.first() == Some(&b'-') {
        -magnitude
    } else {
        magnitude
    };
    i32::try_from(parsed).ok().filter(|number| *number >= 0)
}

fn unqualified_attribute<'document>(
    node: roxmltree::Node<'document, '_>,
    name: &str,
) -> Option<&'document str> {
    node.attributes()
        .find(|attribute| attribute.namespace().is_none() && attribute.name() == name)
        .map(|attribute| attribute.value())
}

fn canonical_authentication_fault(value: &str) -> Option<&'static str> {
    const TOKENS: [(&str, &str); 13] = [
        ("NotAuthorized", "notauthorized"),
        ("NotAuthorized", "not authorized"),
        ("NotAuthorized", "not_authorized"),
        ("NotAuthorized", "not-authorized"),
        ("InvalidSecurity", "invalidsecurity"),
        ("InvalidSecurity", "invalid security"),
        ("InvalidSecurity", "invalid_security"),
        ("InvalidSecurity", "invalid-security"),
        ("FailedAuthentication", "failedauthentication"),
        ("FailedAuthentication", "failed authentication"),
        ("FailedAuthentication", "failed_authentication"),
        ("FailedAuthentication", "failed-authentication"),
        ("Unauthorized", "unauthorized"),
    ];
    let lower = value.to_ascii_lowercase();
    for (token, wanted) in TOKENS {
        let mut offset = 0;
        while let Some(found) = lower[offset..].find(wanted) {
            let start = offset + found;
            let end = start + wanted.len();
            let boundary =
                |byte: u8| !byte.is_ascii_alphanumeric() && !matches!(byte, b'_' | b'.' | b'-');
            let left_ok = start == 0 || boundary(lower.as_bytes()[start - 1]);
            let right_ok = end == lower.len() || boundary(lower.as_bytes()[end]);
            if left_ok && right_ok {
                return Some(token);
            }
            offset = end;
        }
    }
    None
}

fn canonical_protocol_fault_code(
    value: Option<&str>,
    soap_namespace: &str,
) -> Option<&'static str> {
    let normalized = xml_scalar(value)?;
    let local = normalized.rsplit(':').next()?;
    let canonical =
        |expected: &'static str| local.eq_ignore_ascii_case(expected).then_some(expected);
    canonical("ActionNotSupported").or_else(|| match soap_namespace {
        SOAP11_NS => ["VersionMismatch", "MustUnderstand", "Client", "Server"]
            .into_iter()
            .find_map(canonical),
        SOAP12_NS => [
            "VersionMismatch",
            "MustUnderstand",
            "DataEncodingUnknown",
            "Sender",
            "Receiver",
        ]
        .into_iter()
        .find_map(canonical),
        _ => None,
    })
}

fn fault_error(fault: roxmltree::Node<'_, '_>, soap_namespace: &str) -> ResponseError {
    let mut values = Vec::new();
    let deepest_code;
    if soap_namespace == SOAP12_NS {
        let mut code = child(fault, soap_namespace, "Code");
        let mut deepest = None;
        while let Some(current) = code {
            let value = child(current, soap_namespace, "Value").and_then(|node| node.text());
            deepest = value.map(str::to_owned);
            if let Some(value) = value {
                values.push(value.to_owned());
            }
            code = child(current, soap_namespace, "Subcode");
        }
        deepest_code = deepest;
        if let Some(reason) = child(fault, soap_namespace, "Reason") {
            values.extend(
                children(reason, soap_namespace, "Text")
                    .filter_map(|node| node.text().map(str::to_owned)),
            );
        }
    } else {
        let code = child(fault, "", "faultcode").and_then(|node| node.text());
        let reason = child(fault, "", "faultstring").and_then(|node| node.text());
        values.extend([code, reason].into_iter().flatten().map(str::to_owned));
        deepest_code = code.map(str::to_owned);
    }
    let combined = values.join(" ");
    let code = canonical_authentication_fault(&combined)
        .or_else(|| canonical_protocol_fault_code(deepest_code.as_deref(), soap_namespace))
        .unwrap_or("Fault");
    ResponseError::fault(code)
}

fn operation_response<'document, 'input>(
    document: &'document roxmltree::Document<'input>,
    namespace: &str,
    response_name: &str,
    operation: &str,
) -> Result<roxmltree::Node<'document, 'input>, ResponseError> {
    let root = document.root_element();
    let soap_namespace = root.tag_name().namespace().unwrap_or_default();
    if !matches!(soap_namespace, SOAP11_NS | SOAP12_NS) || root.tag_name().name() != "Envelope" {
        return Err(ResponseError::invalid(format!(
            "invalid {operation} response"
        )));
    }
    let body = child(root, soap_namespace, "Body")
        .ok_or_else(|| ResponseError::invalid(format!("invalid {operation} response")))?;
    if let Some(fault) = child(body, soap_namespace, "Fault") {
        return Err(fault_error(fault, soap_namespace));
    }
    child(body, namespace, response_name)
        .ok_or_else(|| ResponseError::invalid(format!("invalid {operation} response")))
}

fn decode_profile_name(scope: &str) -> Option<String> {
    let encoded = scope.strip_prefix(PROFILE_SCOPE_PREFIX)?;
    let encoded = encoded.split(['/', '?', '#']).next()?;
    if encoded.is_empty() {
        return None;
    }
    let bytes = encoded.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let high = *bytes.get(index + 1)?;
            let low = *bytes.get(index + 2)?;
            let hex = |byte: u8| match byte {
                b'0'..=b'9' => Some(byte - b'0'),
                b'a'..=b'f' => Some(byte - b'a' + 10),
                b'A'..=b'F' => Some(byte - b'A' + 10),
                _ => None,
            };
            decoded.push(hex(high)? * 16 + hex(low)?);
            index += 3;
        } else {
            decoded.push(bytes[index]);
            index += 1;
        }
    }
    let decoded = String::from_utf8(decoded).ok()?;
    if decoded.eq_ignore_ascii_case("streaming") {
        Some("S".to_owned())
    } else {
        Some(decoded.to_uppercase())
    }
}

fn parse_scopes_response(xml: &str) -> Result<ScopesResult, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(&document, DEVICE_NS, "GetScopesResponse", "GetScopes")?;
    let mut scopes = Vec::new();
    let mut scope_seen = BTreeSet::new();
    let mut declared_profiles = Vec::new();
    let mut profile_seen = BTreeSet::new();
    for scope in children(response, DEVICE_NS, "Scopes") {
        let Some(value) = plain_text(child(scope, SCHEMA_NS, "ScopeItem")) else {
            continue;
        };
        if scope_seen.insert(value.clone()) {
            if let Some(profile) = decode_profile_name(&value) {
                if profile_seen.insert(profile.clone()) {
                    declared_profiles.push(profile);
                }
            }
            scopes.push(value);
        }
    }
    Ok(ScopesResult {
        scopes,
        declared_profiles,
    })
}

fn parse_version(parent: roxmltree::Node<'_, '_>) -> Option<CameraCapabilityVersion> {
    let version = child(parent, DEVICE_NS, "Version")?;
    let major = strict_nonnegative_int32(child(version, SCHEMA_NS, "Major")?.text())?;
    let minor = strict_nonnegative_int32(child(version, SCHEMA_NS, "Minor")?.text())?;
    Some(CameraCapabilityVersion { major, minor })
}

fn service_order(
    left: &CameraCapabilityService,
    right: &CameraCapabilityService,
) -> std::cmp::Ordering {
    left.namespace
        .cmp(&right.namespace)
        .then_with(|| left.xaddr.cmp(&right.xaddr))
        .then_with(|| {
            let left = left
                .version
                .as_ref()
                .map_or((-1, -1), |version| (version.major, version.minor));
            let right = right
                .version
                .as_ref()
                .map_or((-1, -1), |version| (version.major, version.minor));
            left.cmp(&right)
        })
}

fn select_service<'a>(
    services: &'a [CameraCapabilityService],
    namespace: &str,
) -> Option<&'a CameraCapabilityService> {
    services
        .iter()
        .filter(|service| service.namespace == namespace)
        .min_by(|left, right| {
            let left_version = left
                .version
                .as_ref()
                .map_or((-1, -1), |version| (version.major, version.minor));
            let right_version = right
                .version
                .as_ref()
                .map_or((-1, -1), |version| (version.major, version.minor));
            right_version
                .cmp(&left_version)
                .then_with(|| left.xaddr.cmp(&right.xaddr))
        })
}

fn parse_services_response(xml: &str) -> Result<ServicesResult, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(&document, DEVICE_NS, "GetServicesResponse", "GetServices")?;
    let mut services = Vec::new();
    for element in children(response, DEVICE_NS, "Service") {
        let namespace = plain_text(child(element, DEVICE_NS, "Namespace"))
            .ok_or_else(|| ResponseError::invalid("invalid GetServices response"))?;
        let xaddr = plain_text(child(element, DEVICE_NS, "XAddr"))
            .ok_or_else(|| ResponseError::invalid("invalid GetServices response"))?;
        services.push(CameraCapabilityService {
            namespace,
            xaddr,
            version: parse_version(element),
        });
    }
    if services.is_empty() {
        return Err(ResponseError::invalid(
            "no services in GetServices response",
        ));
    }
    services.sort_by(service_order);
    Ok(ServicesResult { services })
}

const LEGACY_SERVICES: [(&str, &str); 12] = [
    ("Analytics", "http://www.onvif.org/ver20/analytics/wsdl"),
    ("Device", DEVICE_NS),
    ("DeviceIO", "http://www.onvif.org/ver10/deviceIO/wsdl"),
    ("Display", "http://www.onvif.org/ver10/display/wsdl"),
    ("Events", EVENTS_NS),
    ("Imaging", "http://www.onvif.org/ver20/imaging/wsdl"),
    ("Media", MEDIA1_NS),
    ("PTZ", PTZ_NS),
    ("Receiver", "http://www.onvif.org/ver10/receiver/wsdl"),
    ("Recording", "http://www.onvif.org/ver10/recording/wsdl"),
    ("Replay", "http://www.onvif.org/ver10/replay/wsdl"),
    ("Search", "http://www.onvif.org/ver10/search/wsdl"),
];

fn parse_capabilities_response(xml: &str) -> Result<ServicesResult, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(
        &document,
        DEVICE_NS,
        "GetCapabilitiesResponse",
        "GetCapabilities",
    )?;
    let capabilities = child(response, DEVICE_NS, "Capabilities")
        .ok_or_else(|| ResponseError::invalid("invalid GetCapabilities response"))?;
    let mut containers = vec![capabilities];
    if let Some(extension) = child(capabilities, SCHEMA_NS, "Extension") {
        containers.push(extension);
    }
    let mut services = Vec::new();
    for container in containers {
        for element in container.children().filter(roxmltree::Node::is_element) {
            if element.tag_name().namespace() != Some(SCHEMA_NS) {
                continue;
            }
            let Some((_, namespace)) = LEGACY_SERVICES
                .iter()
                .find(|(local, _)| *local == element.tag_name().name())
            else {
                continue;
            };
            let Some(xaddr) = plain_text(child(element, SCHEMA_NS, "XAddr")) else {
                continue;
            };
            services.push(CameraCapabilityService {
                namespace: (*namespace).to_owned(),
                xaddr,
                version: None,
            });
        }
    }
    if services.is_empty() {
        return Err(ResponseError::invalid(
            "no services in GetCapabilities response",
        ));
    }
    services.sort_by(service_order);
    Ok(ServicesResult { services })
}

fn required_token(
    element: roxmltree::Node<'_, '_>,
    operation: &str,
) -> Result<String, ResponseError> {
    unqualified_attribute(element, "token")
        .filter(|token| !token.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| ResponseError::invalid(format!("invalid {operation} response")))
}

fn parse_media1_profiles_response(
    xml: &str,
) -> Result<Vec<CameraCapabilityProfile>, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(
        &document,
        MEDIA1_NS,
        "GetProfilesResponse",
        "Media1 GetProfiles",
    )?;
    let mut profiles = Vec::new();
    for element in children(response, MEDIA1_NS, "Profiles") {
        let token = required_token(element, "Media1 GetProfiles")?;
        let ptz = child(element, SCHEMA_NS, "PTZConfiguration");
        profiles.push(CameraCapabilityProfile {
            token,
            source: "media1".to_owned(),
            has_audio_encoder: child(element, SCHEMA_NS, "AudioEncoderConfiguration").is_some(),
            has_audio_output: child(element, SCHEMA_NS, "AudioOutputConfiguration").is_some(),
            has_audio_source: child(element, SCHEMA_NS, "AudioSourceConfiguration").is_some(),
            name: plain_text(child(element, SCHEMA_NS, "Name")),
            ptz_configuration_token: ptz
                .and_then(|node| unqualified_attribute(node, "token"))
                .filter(|token| !token.is_empty())
                .map(str::to_owned),
            ptz_node_token: ptz.and_then(|node| plain_text(child(node, SCHEMA_NS, "NodeToken"))),
        });
    }
    profiles.sort_by(|left, right| left.token.cmp(&right.token));
    Ok(profiles)
}

fn parse_media2_profiles_response(
    xml: &str,
) -> Result<Vec<CameraCapabilityProfile>, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(
        &document,
        MEDIA2_NS,
        "GetProfilesResponse",
        "Media2 GetProfiles",
    )?;
    let mut profiles = Vec::new();
    for element in children(response, MEDIA2_NS, "Profiles") {
        let token = required_token(element, "Media2 GetProfiles")?;
        let configurations = child(element, MEDIA2_NS, "Configurations");
        let ptz = configurations.and_then(|node| child(node, MEDIA2_NS, "PTZ"));
        profiles.push(CameraCapabilityProfile {
            token,
            source: "media2".to_owned(),
            has_audio_encoder: configurations
                .and_then(|node| child(node, MEDIA2_NS, "AudioEncoder"))
                .is_some(),
            has_audio_output: configurations
                .and_then(|node| child(node, MEDIA2_NS, "AudioOutput"))
                .is_some(),
            has_audio_source: configurations
                .and_then(|node| child(node, MEDIA2_NS, "AudioSource"))
                .is_some(),
            name: plain_text(child(element, MEDIA2_NS, "Name")),
            ptz_configuration_token: ptz
                .and_then(|node| unqualified_attribute(node, "token"))
                .filter(|token| !token.is_empty())
                .map(str::to_owned),
            ptz_node_token: ptz.and_then(|node| plain_text(child(node, SCHEMA_NS, "NodeToken"))),
        });
    }
    profiles.sort_by(|left, right| left.token.cmp(&right.token));
    Ok(profiles)
}

fn parse_ptz_service_capabilities_response(
    xml: &str,
) -> Result<PtzServiceCapabilities, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(
        &document,
        PTZ_NS,
        "GetServiceCapabilitiesResponse",
        "PTZ GetServiceCapabilities",
    )?;
    let capabilities = child(response, PTZ_NS, "Capabilities")
        .ok_or_else(|| ResponseError::invalid("invalid PTZ GetServiceCapabilities response"))?;
    Ok(PtzServiceCapabilities {
        e_flip: strict_bool(unqualified_attribute(capabilities, "EFlip")),
        reverse: strict_bool(unqualified_attribute(capabilities, "Reverse")),
        get_compatible_configurations: strict_bool(unqualified_attribute(
            capabilities,
            "GetCompatibleConfigurations",
        )),
        move_status: strict_bool(unqualified_attribute(capabilities, "MoveStatus")),
        status_position: strict_bool(unqualified_attribute(capabilities, "StatusPosition")),
    })
}

fn ptz_spaces(element: roxmltree::Node<'_, '_>) -> PtzSpaces {
    let has = |name| child(element, SCHEMA_NS, name).is_some();
    PtzSpaces {
        absolute_pan_tilt: has("AbsolutePanTiltPositionSpace"),
        absolute_zoom: has("AbsoluteZoomPositionSpace"),
        relative_pan_tilt: has("RelativePanTiltTranslationSpace"),
        relative_zoom: has("RelativeZoomTranslationSpace"),
        continuous_pan_tilt: has("ContinuousPanTiltVelocitySpace"),
        continuous_zoom: has("ContinuousZoomVelocitySpace"),
    }
}

fn parse_ptz_nodes_response(xml: &str) -> Result<PtzNodesResult, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(&document, PTZ_NS, "GetNodesResponse", "PTZ GetNodes")?;
    let mut nodes = Vec::new();
    for element in children(response, PTZ_NS, "PTZNode") {
        let token = required_token(element, "PTZ GetNodes")?;
        let spaces_element = child(element, SCHEMA_NS, "SupportedPTZSpaces")
            .ok_or_else(|| ResponseError::invalid("invalid PTZ GetNodes response"))?;
        let auxiliary_commands = children(element, SCHEMA_NS, "AuxiliaryCommands")
            .filter_map(|node| plain_text(Some(node)))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        nodes.push(PtzNode {
            token,
            spaces: ptz_spaces(spaces_element),
            name: plain_text(child(element, SCHEMA_NS, "Name")),
            maximum_presets: strict_nonnegative_int32(
                child(element, SCHEMA_NS, "MaximumNumberOfPresets").and_then(|node| node.text()),
            ),
            home_supported: strict_bool(
                child(element, SCHEMA_NS, "HomeSupported").and_then(|node| node.text()),
            ),
            auxiliary_commands,
        });
    }
    nodes.sort_by(|left, right| left.token.cmp(&right.token));
    let pan_tilt_supported = nodes.iter().any(|node| {
        node.spaces.absolute_pan_tilt
            || node.spaces.relative_pan_tilt
            || node.spaces.continuous_pan_tilt
    });
    let zoom_supported = nodes.iter().any(|node| {
        node.spaces.absolute_zoom || node.spaces.relative_zoom || node.spaces.continuous_zoom
    });
    Ok(PtzNodesResult {
        nodes,
        pan_tilt_supported,
        zoom_supported,
    })
}

fn parse_media2_options_response(xml: &str) -> Result<Vec<String>, ResponseError> {
    let document = parse_document(xml)?;
    let response = operation_response(
        &document,
        MEDIA2_NS,
        "GetVideoEncoderConfigurationOptionsResponse",
        "Media2 GetVideoEncoderConfigurationOptions",
    )?;
    let mut encodings = BTreeSet::new();
    let mut has_standard_encoding = false;
    for options in children(response, MEDIA2_NS, "Options") {
        for encoding in children(options, SCHEMA_NS, "Encoding") {
            if let Some(value) = xml_scalar(encoding.text()) {
                has_standard_encoding = true;
                encodings.insert(value.to_uppercase());
            }
        }
        if let Some(value) = xml_scalar(unqualified_attribute(options, "Encoding")) {
            encodings.insert(value.to_uppercase());
        }
    }
    if !has_standard_encoding {
        return Err(ResponseError::invalid(
            "invalid Media2 GetVideoEncoderConfigurationOptions response",
        ));
    }
    Ok(encodings.into_iter().collect())
}

#[derive(Debug)]
enum OperationError {
    Http(u16),
    Response(ResponseError),
    Transport(String),
}

impl OperationError {
    fn is_authentication_failure(&self) -> bool {
        match self {
            Self::Http(status) => matches!(status, 401 | 403),
            Self::Response(error) => error.fault_code.as_deref().is_some_and(|code| {
                matches!(
                    code,
                    "NotAuthorized" | "InvalidSecurity" | "FailedAuthentication" | "Unauthorized"
                )
            }),
            Self::Transport(_) => false,
        }
    }

    fn sanitized_message(&self) -> String {
        match self {
            Self::Http(status) => format!("HTTP {status}"),
            Self::Response(error) => error.to_string(),
            Self::Transport(message) if message == "invalid ONVIF service URL" => message.clone(),
            Self::Transport(message)
                if message.to_ascii_lowercase().contains("timeout")
                    || message.to_ascii_lowercase().contains("timed out") =>
            {
                "request timeout".to_owned()
            }
            Self::Transport(message)
                if message.contains("response body") && message.contains("limit") =>
            {
                "response body exceeds limit".to_owned()
            }
            Self::Transport(_) => "request failed".to_owned(),
        }
    }
}

fn read_only_parse<T>(
    device: &super::OnvifDevice,
    endpoint: &str,
    body: &str,
    parser: fn(&str) -> Result<T, ResponseError>,
) -> Result<T, OperationError> {
    let (status, xml) = device
        .soap_response(endpoint, body, true)
        .map_err(OperationError::Transport)?;
    if matches!(status.as_u16(), 401 | 403) {
        return Err(OperationError::Http(status.as_u16()));
    }
    match parser(&xml) {
        Err(error) if error.kind == ResponseErrorKind::Fault => {
            Err(OperationError::Response(error))
        }
        Err(_) if !status.is_success() => Err(OperationError::Http(status.as_u16())),
        Err(error) => Err(OperationError::Response(error)),
        Ok(_) if !status.is_success() => Err(OperationError::Http(status.as_u16())),
        Ok(value) => Ok(value),
    }
}

fn record_optional_error(
    operation: &str,
    error: OperationError,
    warnings: &mut Vec<CameraCapabilityWarning>,
) -> Result<(), String> {
    if error.is_authentication_failure() {
        return Err(error.sanitized_message());
    }
    warnings.push(CameraCapabilityWarning {
        operation: operation.to_owned(),
        message: error.sanitized_message(),
    });
    Ok(())
}

fn ptz_capabilities_are_empty(capabilities: &PtzServiceCapabilities) -> bool {
    capabilities.e_flip.is_none()
        && capabilities.reverse.is_none()
        && capabilities.get_compatible_configurations.is_none()
        && capabilities.move_status.is_none()
        && capabilities.status_position.is_none()
}

pub fn get_camera_capabilities(
    options: &CameraCapabilityOptions,
) -> Result<CameraCapabilityReport, String> {
    let device_urls = if options.device_urls.is_empty() {
        vec![
            format!("http://{}/onvif/device_service", options.host),
            format!("https://{}/onvif/device_service", options.host),
            format!("http://{}:8000/onvif/device_service", options.host),
        ]
    } else {
        options.device_urls.clone()
    };
    let mut device = super::OnvifDevice::with_device_urls_and_timeout(
        &options.host,
        &options.user,
        &options.password,
        device_urls,
        options.timeout,
    )?;
    let device_information = device.connect_with_device_info()?;
    let device_endpoint = device.require_device_url()?.to_owned();
    let connected_media_endpoint = device.require_media_url()?.to_owned();
    let mut warnings = Vec::new();

    let (scopes, declared_profiles) =
        match read_only_parse(&device, &device_endpoint, GET_SCOPES, parse_scopes_response) {
            Ok(result) => (result.scopes, result.declared_profiles),
            Err(error) => {
                record_optional_error("GetScopes", error, &mut warnings)?;
                (Vec::new(), Vec::new())
            }
        };

    let mut service_discovery = "unavailable".to_owned();
    let mut services = Vec::new();
    let mut get_services_succeeded = false;
    let mut discovery_succeeded = false;
    match read_only_parse(
        &device,
        &device_endpoint,
        GET_SERVICES,
        parse_services_response,
    ) {
        Ok(result) => {
            service_discovery = "getServices".to_owned();
            services = result.services;
            get_services_succeeded = true;
            discovery_succeeded = true;
        }
        Err(error) => {
            record_optional_error("GetServices", error, &mut warnings)?;
            match read_only_parse(
                &device,
                &device_endpoint,
                GET_ALL_CAPABILITIES,
                parse_capabilities_response,
            ) {
                Ok(result) => {
                    service_discovery = "getCapabilities".to_owned();
                    services = result.services;
                    discovery_succeeded = true;
                }
                Err(error) => {
                    record_optional_error("GetCapabilities", error, &mut warnings)?;
                }
            }
        }
    }

    let media1_endpoint = select_service(&services, MEDIA1_NS)
        .map(|service| service.xaddr.clone())
        .unwrap_or(connected_media_endpoint);
    let ptz_endpoint = select_service(&services, PTZ_NS).map(|service| service.xaddr.clone());
    let media2_endpoint = select_service(&services, MEDIA2_NS).map(|service| service.xaddr.clone());

    let mut profiles = match read_only_parse(
        &device,
        &media1_endpoint,
        MEDIA1_GET_PROFILES,
        parse_media1_profiles_response,
    ) {
        Ok(profiles) => profiles,
        Err(error) => {
            record_optional_error("Media1 GetProfiles", error, &mut warnings)?;
            Vec::new()
        }
    };

    let mut ptz_service_capabilities = None;
    let mut ptz_nodes = Vec::new();
    let mut pan_tilt_supported = None;
    let mut zoom_supported = None;
    if let Some(endpoint) = ptz_endpoint.as_deref() {
        match read_only_parse(
            &device,
            endpoint,
            PTZ_GET_CAPABILITIES,
            parse_ptz_service_capabilities_response,
        ) {
            Ok(capabilities) if !ptz_capabilities_are_empty(&capabilities) => {
                ptz_service_capabilities = Some(capabilities);
            }
            Ok(_) => {}
            Err(error) => {
                record_optional_error("PTZ GetServiceCapabilities", error, &mut warnings)?;
            }
        }
        match read_only_parse(&device, endpoint, PTZ_GET_NODES, parse_ptz_nodes_response) {
            Ok(result) => {
                ptz_nodes = result.nodes;
                pan_tilt_supported = Some(result.pan_tilt_supported);
                zoom_supported = Some(result.zoom_supported);
            }
            Err(error) => {
                record_optional_error("PTZ GetNodes", error, &mut warnings)?;
            }
        }
    }

    let mut media2_encodings = Vec::new();
    let mut h265_supported = None;
    if let Some(endpoint) = media2_endpoint.as_deref() {
        match read_only_parse(
            &device,
            endpoint,
            MEDIA2_GET_PROFILES,
            parse_media2_profiles_response,
        ) {
            Ok(media2_profiles) => profiles.extend(media2_profiles),
            Err(error) => {
                record_optional_error("Media2 GetProfiles", error, &mut warnings)?;
            }
        }
        match read_only_parse(
            &device,
            endpoint,
            MEDIA2_GET_OPTIONS,
            parse_media2_options_response,
        ) {
            Ok(encodings) => {
                h265_supported = Some(encodings.iter().any(|encoding| encoding == "H265"));
                media2_encodings = encodings;
            }
            Err(error) => {
                record_optional_error(
                    "Media2 GetVideoEncoderConfigurationOptions",
                    error,
                    &mut warnings,
                )?;
            }
        }
    }

    profiles.sort_by(|left, right| {
        left.token
            .cmp(&right.token)
            .then_with(|| left.source.cmp(&right.source))
    });
    let profile_tokens = profiles
        .iter()
        .filter(|profile| profile.ptz_configuration_token.is_some())
        .map(|profile| profile.token.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();

    Ok(CameraCapabilityReport {
        device: device_information,
        scopes,
        declared_profiles,
        service_discovery,
        services,
        profiles,
        ptz: PtzCapabilityReport {
            detected: discovery_succeeded.then_some(ptz_endpoint.is_some()),
            pan_tilt_supported,
            zoom_supported,
            profile_tokens,
            service_capabilities: ptz_service_capabilities,
            nodes: ptz_nodes,
        },
        media2: Media2CapabilityReport {
            detected: get_services_succeeded.then_some(media2_endpoint.is_some()),
            encodings: media2_encodings,
            h265_supported,
        },
        warnings,
    })
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::onvif::DeviceInfo;

    const SOAP12_NS: &str = "http://www.w3.org/2003/05/soap-envelope";
    const DEVICE_NS: &str = "http://www.onvif.org/ver10/device/wsdl";
    const SCHEMA_NS: &str = "http://www.onvif.org/ver10/schema";
    const MEDIA1_NS: &str = "http://www.onvif.org/ver10/media/wsdl";
    const MEDIA2_NS: &str = "http://www.onvif.org/ver20/media/wsdl";
    const PTZ_NS: &str = "http://www.onvif.org/ver20/ptz/wsdl";

    fn soap(body: &str) -> String {
        format!(
            "<s:Envelope xmlns:s=\"{SOAP12_NS}\" xmlns:tds=\"{DEVICE_NS}\" \
             xmlns:tt=\"{SCHEMA_NS}\" xmlns:trt=\"{MEDIA1_NS}\" \
             xmlns:tr2=\"{MEDIA2_NS}\" xmlns:tptz=\"{PTZ_NS}\" \
             xmlns:vendor=\"urn:vendor\">\
             <s:Body>{body}</s:Body></s:Envelope>"
        )
    }

    #[test]
    fn report_json_skips_optional_members_but_keeps_tristate_nulls() {
        let report = CameraCapabilityReport {
            device: DeviceInfo {
                manufacturer: Some("Fixture Camera".to_owned()),
                model: None,
                firmware: None,
                serial: None,
            },
            scopes: Vec::new(),
            declared_profiles: Vec::new(),
            service_discovery: "unavailable".to_owned(),
            services: Vec::new(),
            profiles: Vec::new(),
            ptz: PtzCapabilityReport {
                detected: None,
                pan_tilt_supported: None,
                zoom_supported: Some(false),
                profile_tokens: Vec::new(),
                service_capabilities: None,
                nodes: Vec::new(),
            },
            media2: Media2CapabilityReport {
                detected: None,
                encodings: Vec::new(),
                h265_supported: None,
            },
            warnings: Vec::new(),
        };

        assert_eq!(
            serde_json::to_value(report).unwrap(),
            json!({
                "device": {"manufacturer": "Fixture Camera"},
                "scopes": [],
                "declaredProfiles": [],
                "serviceDiscovery": "unavailable",
                "services": [],
                "profiles": [],
                "ptz": {
                    "detected": null,
                    "panTiltSupported": null,
                    "zoomSupported": false,
                    "profileTokens": [],
                    "nodes": []
                },
                "media2": {
                    "detected": null,
                    "encodings": [],
                    "h265Supported": null
                },
                "warnings": []
            })
        );
    }

    #[test]
    fn parses_scopes_with_stable_deduplication_and_profile_aliases() {
        let parsed = parse_scopes_response(&soap(
            r#"<tds:GetScopesResponse>
              <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/Streaming</tt:ScopeItem></tds:Scopes>
              <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/hardware/Camera%201</tt:ScopeItem></tds:Scopes>
              <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/Streaming</tt:ScopeItem></tds:Scopes>
              <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/%74</tt:ScopeItem></tds:Scopes>
              <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/vendor%2Dplus</tt:ScopeItem></tds:Scopes>
              <tds:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/%ZZ</tt:ScopeItem></tds:Scopes>
              <vendor:Scopes><tt:ScopeItem>onvif://www.onvif.org/Profile/Q</tt:ScopeItem></vendor:Scopes>
            </tds:GetScopesResponse>"#,
        ))
        .unwrap();

        assert_eq!(
            parsed.scopes,
            vec![
                "onvif://www.onvif.org/Profile/Streaming",
                "onvif://www.onvif.org/hardware/Camera%201",
                "onvif://www.onvif.org/Profile/%74",
                "onvif://www.onvif.org/Profile/vendor%2Dplus",
                "onvif://www.onvif.org/Profile/%ZZ",
            ]
        );
        assert_eq!(parsed.declared_profiles, vec!["S", "T", "VENDOR-PLUS"]);
    }

    #[test]
    fn services_use_direct_association_strict_versions_and_stable_selection() {
        let plus = " \t+1\r\n";
        let parsed = parse_services_response(&soap(&format!(
            r#"<tds:GetServicesResponse>
              <tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-z</tds:XAddr>
                <tds:Version><tt:Major>2</tt:Major><tt:Minor>9</tt:Minor></tds:Version></tds:Service>
              <tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-b</tds:XAddr>
                <tds:Version><tt:Major>2</tt:Major><tt:Minor>10</tt:Minor></tds:Version></tds:Service>
              <tds:Service><tds:Namespace>{MEDIA1_NS}</tds:Namespace><tds:XAddr>http://camera/media-a</tds:XAddr>
                <tds:Version><tt:Major>2</tt:Major><tt:Minor>10</tt:Minor></tds:Version></tds:Service>
              <tds:Service><tds:Namespace>urn:plus</tds:Namespace><tds:XAddr>http://camera/plus</tds:XAddr>
                <tds:Version><tt:Major>{plus}</tt:Major><tt:Minor>+2</tt:Minor></tds:Version></tds:Service>
              <tds:Service><tds:Namespace>urn:nbsp</tds:Namespace><tds:XAddr>http://camera/nbsp</tds:XAddr>
                <tds:Version><tt:Major> +1 </tt:Major><tt:Minor>2</tt:Minor></tds:Version></tds:Service>
              <tds:Service><tds:Namespace>urn:overflow</tds:Namespace><tds:XAddr>http://camera/overflow</tds:XAddr>
                <tds:Version><tt:Major>2147483648</tt:Major><tt:Minor>0</tt:Minor></tds:Version></tds:Service>
              <tds:Wrapper><tds:Service><tds:Namespace>urn:decoy</tds:Namespace>
                <tds:XAddr>http://camera/decoy</tds:XAddr></tds:Service></tds:Wrapper>
            </tds:GetServicesResponse>"#
        )))
        .unwrap();

        assert_eq!(
            parsed.services,
            vec![
                CameraCapabilityService {
                    namespace: MEDIA1_NS.to_owned(),
                    xaddr: "http://camera/media-a".to_owned(),
                    version: Some(CameraCapabilityVersion {
                        major: 2,
                        minor: 10
                    }),
                },
                CameraCapabilityService {
                    namespace: MEDIA1_NS.to_owned(),
                    xaddr: "http://camera/media-b".to_owned(),
                    version: Some(CameraCapabilityVersion {
                        major: 2,
                        minor: 10
                    }),
                },
                CameraCapabilityService {
                    namespace: MEDIA1_NS.to_owned(),
                    xaddr: "http://camera/media-z".to_owned(),
                    version: Some(CameraCapabilityVersion { major: 2, minor: 9 }),
                },
                CameraCapabilityService {
                    namespace: "urn:nbsp".to_owned(),
                    xaddr: "http://camera/nbsp".to_owned(),
                    version: None,
                },
                CameraCapabilityService {
                    namespace: "urn:overflow".to_owned(),
                    xaddr: "http://camera/overflow".to_owned(),
                    version: None,
                },
                CameraCapabilityService {
                    namespace: "urn:plus".to_owned(),
                    xaddr: "http://camera/plus".to_owned(),
                    version: Some(CameraCapabilityVersion { major: 1, minor: 2 }),
                },
            ]
        );
        assert_eq!(
            select_service(&parsed.services, MEDIA1_NS).unwrap().xaddr,
            "http://camera/media-a"
        );
    }

    #[test]
    fn parses_media_profiles_with_namespace_association_and_preserved_tokens() {
        let media1 = parse_media1_profiles_response(&soap(
            r#"<trt:GetProfilesResponse>
              <trt:Profiles token="main&amp;special"><tt:Name>Main</tt:Name>
                <tt:AudioSourceConfiguration/><tt:AudioEncoderConfiguration/><tt:AudioOutputConfiguration/>
                <tt:PTZConfiguration token=" ptz-one "><tt:NodeToken>node-one</tt:NodeToken></tt:PTZConfiguration>
              </trt:Profiles>
              <trt:Profiles token="vendor-only"><vendor:AudioEncoderConfiguration/>
                <vendor:PTZConfiguration token="decoy"/></trt:Profiles>
              <trt:Wrapper><trt:Profiles token="nested"/></trt:Wrapper>
            </trt:GetProfilesResponse>"#,
        ))
        .unwrap();
        assert_eq!(
            media1,
            vec![
                CameraCapabilityProfile {
                    token: "main&special".to_owned(),
                    source: "media1".to_owned(),
                    name: Some("Main".to_owned()),
                    has_audio_encoder: true,
                    has_audio_output: true,
                    has_audio_source: true,
                    ptz_configuration_token: Some(" ptz-one ".to_owned()),
                    ptz_node_token: Some("node-one".to_owned()),
                },
                CameraCapabilityProfile {
                    token: "vendor-only".to_owned(),
                    source: "media1".to_owned(),
                    name: None,
                    has_audio_encoder: false,
                    has_audio_output: false,
                    has_audio_source: false,
                    ptz_configuration_token: None,
                    ptz_node_token: None,
                },
            ]
        );

        let media2 = parse_media2_profiles_response(&soap(
            r#"<tr2:GetProfilesResponse>
              <tr2:Profiles token="main"><tr2:Name>Media2 Main</tr2:Name><tr2:Configurations>
                <tr2:AudioSource/><tr2:AudioEncoder/><tr2:AudioOutput/>
                <tr2:PTZ token="ptz-two"><tt:NodeToken>node-two</tt:NodeToken></tr2:PTZ>
              </tr2:Configurations></tr2:Profiles>
              <tr2:Profiles token="vendor-only"><tr2:Configurations>
                <vendor:AudioEncoder/><vendor:PTZ token="decoy"/>
              </tr2:Configurations></tr2:Profiles>
            </tr2:GetProfilesResponse>"#,
        ))
        .unwrap();
        assert_eq!(
            media2,
            vec![
                CameraCapabilityProfile {
                    token: "main".to_owned(),
                    source: "media2".to_owned(),
                    name: Some("Media2 Main".to_owned()),
                    has_audio_encoder: true,
                    has_audio_output: true,
                    has_audio_source: true,
                    ptz_configuration_token: Some("ptz-two".to_owned()),
                    ptz_node_token: Some("node-two".to_owned()),
                },
                CameraCapabilityProfile {
                    token: "vendor-only".to_owned(),
                    source: "media2".to_owned(),
                    name: None,
                    has_audio_encoder: false,
                    has_audio_output: false,
                    has_audio_source: false,
                    ptz_configuration_token: None,
                    ptz_node_token: None,
                },
            ]
        );
    }

    #[test]
    fn strict_ptz_nodes_keep_zoom_distinct_from_pan_tilt() {
        let capabilities = parse_ptz_service_capabilities_response(&soap(
            r#"<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities
              EFlip="true" Reverse="false" GetCompatibleConfigurations="1"
              MoveStatus="0" StatusPosition="TRUE" vendor:EFlip="true"/>
            </tptz:GetServiceCapabilitiesResponse>"#,
        ))
        .unwrap();
        assert_eq!(
            capabilities,
            PtzServiceCapabilities {
                e_flip: Some(true),
                reverse: Some(false),
                get_compatible_configurations: Some(true),
                move_status: Some(false),
                status_position: None,
            }
        );

        let parsed = parse_ptz_nodes_response(&soap(
            r#"<tptz:GetNodesResponse>
              <tptz:PTZNode token="pan"><tt:Name>Pan</tt:Name><tt:SupportedPTZSpaces>
                <tt:AbsolutePanTiltPositionSpace/><tt:ContinuousPanTiltVelocitySpace/>
              </tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>+8</tt:MaximumNumberOfPresets>
                <tt:HomeSupported>true</tt:HomeSupported><tt:AuxiliaryCommands>LightOn</tt:AuxiliaryCommands>
                <tt:AuxiliaryCommands>LightOff</tt:AuxiliaryCommands><tt:AuxiliaryCommands>LightOn</tt:AuxiliaryCommands>
              </tptz:PTZNode>
              <tptz:PTZNode token="zoom"><tt:SupportedPTZSpaces>
                <tt:RelativeZoomTranslationSpace/><tt:ContinuousZoomVelocitySpace/>
              </tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>2147483648</tt:MaximumNumberOfPresets>
                <tt:HomeSupported>0</tt:HomeSupported></tptz:PTZNode>
            </tptz:GetNodesResponse>"#,
        ))
        .unwrap();
        assert!(parsed.pan_tilt_supported);
        assert!(parsed.zoom_supported);
        assert_eq!(parsed.nodes[0].token, "pan");
        assert_eq!(parsed.nodes[0].maximum_presets, Some(8));
        assert_eq!(
            parsed.nodes[0].auxiliary_commands,
            vec!["LightOff", "LightOn"]
        );
        assert!(parsed.nodes[0].spaces.absolute_pan_tilt);
        assert!(!parsed.nodes[0].spaces.absolute_zoom);
        assert_eq!(parsed.nodes[1].token, "zoom");
        assert_eq!(parsed.nodes[1].maximum_presets, None);
        assert_eq!(parsed.nodes[1].home_supported, Some(false));
        assert!(!parsed.nodes[1].spaces.absolute_pan_tilt);
        assert!(parsed.nodes[1].spaces.relative_zoom);

        let zoom_only = parse_ptz_nodes_response(&soap(
            r#"<tptz:GetNodesResponse><tptz:PTZNode token="zoom-only">
              <tt:SupportedPTZSpaces><tt:AbsoluteZoomPositionSpace/></tt:SupportedPTZSpaces>
            </tptz:PTZNode></tptz:GetNodesResponse>"#,
        ))
        .unwrap();
        assert!(!zoom_only.pan_tilt_supported);
        assert!(zoom_only.zoom_supported);
    }

    #[test]
    fn media2_options_are_namespace_aware_sorted_and_detect_h265() {
        let encodings = parse_media2_options_response(&soap(
            r#"<tr2:GetVideoEncoderConfigurationOptionsResponse>
              <tr2:Options><tt:Encoding>H264</tt:Encoding></tr2:Options>
              <tr2:Options Encoding="h265"><tt:Encoding>H264</tt:Encoding></tr2:Options>
              <tr2:Options Encoding="VP9"><tt:Encoding>h265</tt:Encoding></tr2:Options>
              <tr2:Options vendor:Encoding="H266"><vendor:Encoding>H267</vendor:Encoding></tr2:Options>
            </tr2:GetVideoEncoderConfigurationOptionsResponse>"#,
        ))
        .unwrap();

        assert_eq!(encodings, vec!["H264", "H265", "VP9"]);
        assert!(encodings.iter().any(|encoding| encoding == "H265"));
    }

    #[test]
    fn rejects_faults_wrong_operations_malformed_xml_and_dtds_without_payloads() {
        let action_fault = soap(
            r#"<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>
              <s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:ActionNotSupported</s:Value>
            </s:Subcode></s:Code><s:Reason><s:Text>payload-secret</s:Text></s:Reason></s:Fault>"#,
        );
        let error = parse_services_response(&action_fault).unwrap_err();
        assert_eq!(error.fault_code.as_deref(), Some("ActionNotSupported"));
        assert_eq!(error.to_string(), "SOAP Fault: ActionNotSupported");
        assert!(!error.to_string().contains("payload-secret"));

        let soap11_auth = concat!(
            "<env:Envelope xmlns:env=\"http://schemas.xmlsoap.org/soap/envelope/\">",
            "<env:Body><env:Fault><faultcode>env:Client</faultcode>",
            "<faultstring>request rejected: notauthorized; payload-secret</faultstring>",
            "</env:Fault></env:Body></env:Envelope>"
        );
        let error = parse_services_response(soap11_auth).unwrap_err();
        assert_eq!(error.fault_code.as_deref(), Some("NotAuthorized"));
        assert_eq!(error.to_string(), "SOAP Fault: NotAuthorized");

        for (reason, expected) in [
            ("not authorized", "NotAuthorized"),
            ("invalid-security", "InvalidSecurity"),
            ("failed_authentication", "FailedAuthentication"),
        ] {
            let error = parse_services_response(&soap(&format!(
                "<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code><s:Reason><s:Text>{reason}</s:Text></s:Reason></s:Fault>"
            )))
            .unwrap_err();
            assert_eq!(error.fault_code.as_deref(), Some(expected));
        }

        for code in [
            "UnauthorizedOperation",
            "NotAuthorized2",
            "foo.NotAuthorized",
        ] {
            let error = parse_services_response(&soap(&format!(
                "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>{code}</s:Value></s:Subcode></s:Code></s:Fault>"
            )))
            .unwrap_err();
            assert_eq!(error.fault_code.as_deref(), Some("Fault"));
            assert_eq!(error.to_string(), "SOAP Fault: Fault");
            assert!(!error.to_string().contains(code));
        }

        let soap12_sensitive = soap(
            "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>vendor:camera-password-marker</s:Value></s:Subcode></s:Code><s:Reason><s:Text>viewer-marker</s:Text></s:Reason><s:Detail>PasswordDigestABC123</s:Detail></s:Fault>",
        );
        let error = parse_services_response(&soap12_sensitive).unwrap_err();
        assert_eq!(error.fault_code.as_deref(), Some("Fault"));
        assert_eq!(error.to_string(), "SOAP Fault: Fault");

        let soap11_sensitive = concat!(
            "<env:Envelope xmlns:env=\"http://schemas.xmlsoap.org/soap/envelope/\">",
            "<env:Body><env:Fault><faultcode>camera-password-marker</faultcode>",
            "<faultstring>viewer-marker</faultstring>",
            "<detail>PasswordDigestABC123</detail></env:Fault></env:Body></env:Envelope>"
        );
        let error = parse_services_response(soap11_sensitive).unwrap_err();
        assert_eq!(error.fault_code.as_deref(), Some("Fault"));
        assert_eq!(error.to_string(), "SOAP Fault: Fault");

        for marker in [
            "camera-password-marker",
            "viewer-marker",
            "PasswordDigestABC123",
        ] {
            assert!(
                !parse_services_response(&soap12_sensitive)
                    .unwrap_err()
                    .to_string()
                    .contains(marker)
            );
            assert!(
                !parse_services_response(soap11_sensitive)
                    .unwrap_err()
                    .to_string()
                    .contains(marker)
            );
        }

        for expected in [
            "VersionMismatch",
            "MustUnderstand",
            "DataEncodingUnknown",
            "Sender",
            "Receiver",
        ] {
            let error = parse_services_response(&soap(&format!(
                "<s:Fault><s:Code><s:Value>s:{expected}</s:Value></s:Code></s:Fault>"
            )))
            .unwrap_err();
            assert_eq!(error.fault_code.as_deref(), Some(expected));
            assert_eq!(error.to_string(), format!("SOAP Fault: {expected}"));
        }
        for expected in ["VersionMismatch", "MustUnderstand", "Client", "Server"] {
            let xml = format!(
                "<env:Envelope xmlns:env=\"http://schemas.xmlsoap.org/soap/envelope/\"><env:Body><env:Fault><faultcode>env:{expected}</faultcode><faultstring>request failed</faultstring></env:Fault></env:Body></env:Envelope>"
            );
            let error = parse_services_response(&xml).unwrap_err();
            assert_eq!(error.fault_code.as_deref(), Some(expected));
            assert_eq!(error.to_string(), format!("SOAP Fault: {expected}"));
        }

        for xml in [
            "<broken>".to_owned(),
            soap("<tds:GetScopesResponse/>"),
            format!(
                "<!DOCTYPE s:Envelope [<!ENTITY injected \"payload-secret\">]>{}",
                soap(
                    "<tds:GetServicesResponse><tds:Service><tds:Namespace>urn:test</tds:Namespace><tds:XAddr>&injected;</tds:XAddr></tds:Service></tds:GetServicesResponse>"
                )
            ),
        ] {
            let error = parse_services_response(&xml).unwrap_err();
            assert!(!error.to_string().contains("payload-secret"));
        }
    }

    #[test]
    fn accepts_64_nested_xml_elements_and_rejects_depth_65_deterministically() {
        let depth_64 = format!("{}{}", "<n>".repeat(64), "</n>".repeat(64));
        let document = parse_document(&depth_64).expect("64 levels of nesting must be accepted");
        assert_eq!(
            document
                .descendants()
                .filter(roxmltree::Node::is_element)
                .count(),
            64
        );

        let depth_65 = format!("{}{}", "<n>".repeat(65), "</n>".repeat(65));
        let error = parse_document(&depth_65).unwrap_err();
        assert_eq!(error.to_string(), "invalid XML document");
    }
}
