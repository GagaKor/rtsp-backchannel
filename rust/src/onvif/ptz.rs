//! ONVIF PTZ movement control.
//!
//! A session opens once (connect -> GetServices -> GetNodes -> resolve the
//! profile token), caches the node's supported spaces, and rejects any
//! operation the camera did not advertise before building a request.
//! Control reuses the same authenticated transport (`OnvifDevice`'s private
//! `soap_response`) the read-only capability report uses; it is a different
//! SOAP body on the existing pipe, not a new one.
//!
//! This module intentionally does not import `super::capabilities` (and vice
//! versa) so the two stay independent; shared value types live in
//! `ptz_types`. PTZ SOAP fault classification mirrors what
//! `capabilities.rs` already does for its own responses, but is duplicated
//! here rather than imported, for the same reason.

use std::collections::BTreeSet;
use std::time::Duration;

use serde::Serialize;

use super::OnvifDevice;
use super::ptz_types::{PtzNode, PtzSpaces};

const SOAP11_NS: &str = "http://schemas.xmlsoap.org/soap/envelope/";
const SOAP12_NS: &str = "http://www.w3.org/2003/05/soap-envelope";
const DEV_NS: &str = "http://www.onvif.org/ver10/device/wsdl";
const SCHEMA_NS: &str = "http://www.onvif.org/ver10/schema";
const MEDIA1_NS: &str = "http://www.onvif.org/ver10/media/wsdl";
const MEDIA2_NS: &str = "http://www.onvif.org/ver20/media/wsdl";
const PTZ_NS: &str = "http://www.onvif.org/ver20/ptz/wsdl";

const MAX_XML_ELEMENT_DEPTH: usize = 64;

const DEFAULT_MOVE_TIMEOUT_MS: f64 = 1000.0;
const PAN_TILT_RANGE: (f64, f64) = (-1.0, 1.0);
/// Every zoom quantity is -1.0..1.0 except an absolute zoom *position*,
/// which is 0.0..1.0.
const ZOOM_GENERIC_RANGE: (f64, f64) = (-1.0, 1.0);
const ZOOM_POSITION_RANGE: (f64, f64) = (0.0, 1.0);

const GET_SERVICES: &str = "<GetServices xmlns=\"http://www.onvif.org/ver10/device/wsdl\"/>";
const GET_NODES: &str = "<GetNodes xmlns=\"http://www.onvif.org/ver20/ptz/wsdl\"/>";
const MEDIA1_GET_PROFILES: &str = "<GetProfiles xmlns=\"http://www.onvif.org/ver10/media/wsdl\"/>";
const MEDIA2_GET_PROFILES: &str = concat!(
    "<GetProfiles xmlns=\"http://www.onvif.org/ver20/media/wsdl\">",
    "<Type>All</Type></GetProfiles>"
);

// ---------------------------------------------------------------------
// Value types
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct PtzVector {
    pub x: f64,
    pub y: f64,
}

#[derive(Clone, PartialEq)]
pub struct PtzSessionOptions {
    pub host: String,
    pub user: String,
    pub password: String,
    /// Default: first Media1 (falling back to Media2) profile carrying a
    /// PTZ configuration.
    pub profile_token: Option<String>,
    pub device_urls: Vec<String>,
    pub timeout: Duration,
    /// ContinuousMove `Timeout`, default 1000ms.
    pub default_move_timeout_ms: f64,
}

const REDACTED_PASSWORD_PLACEHOLDER: &str = "<redacted>";

impl std::fmt::Debug for PtzSessionOptions {
    // Hand-written so a caller's `dbg!`, log line, or error context can
    // never print the plaintext camera password: this struct is what every
    // caller constructs directly, so it is far more exposed than
    // `PtzSession` (see that type's own hand-written `Debug`).
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PtzSessionOptions")
            .field("host", &self.host)
            .field("user", &self.user)
            .field("password", &REDACTED_PASSWORD_PLACEHOLDER)
            .field("profile_token", &self.profile_token)
            .field("device_urls", &self.device_urls)
            .field("timeout", &self.timeout)
            .field("default_move_timeout_ms", &self.default_move_timeout_ms)
            .finish()
    }
}

impl PtzSessionOptions {
    pub fn new(
        host: impl Into<String>,
        user: impl Into<String>,
        password: impl Into<String>,
    ) -> Self {
        Self {
            host: host.into(),
            user: user.into(),
            password: password.into(),
            profile_token: None,
            device_urls: Vec::new(),
            timeout: Duration::from_secs(8),
            default_move_timeout_ms: DEFAULT_MOVE_TIMEOUT_MS,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PtzStatus {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pan_tilt: Option<PtzVector>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub zoom: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pan_tilt_move_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub zoom_move_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub utc_time: Option<String>,
    // Deliberately no error field: GetStatusResponse carries a
    // camera-supplied <Error> string; it must never surface in PtzStatus,
    // in any form.
}

// ---------------------------------------------------------------------
// XML parsing infrastructure (own copy; see module docs)
// ---------------------------------------------------------------------

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

fn parse_document(xml: &str) -> Result<roxmltree::Document<'_>, String> {
    if has_forbidden_declaration(xml) {
        return Err("invalid XML document".to_owned());
    }
    xml_element_depth(xml).ok_or_else(|| "invalid XML document".to_owned())?;
    roxmltree::Document::parse(xml).map_err(|_| "invalid XML document".to_owned())
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

// ---------------------------------------------------------------------
// SOAP fault classification (own copy; mirrors capabilities.rs)
// ---------------------------------------------------------------------

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

fn fault_message(fault: roxmltree::Node<'_, '_>, soap_namespace: &str) -> String {
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
    format!("SOAP Fault: {code}")
}

fn operation_response<'document, 'input>(
    document: &'document roxmltree::Document<'input>,
    namespace: &str,
    response_name: &str,
    operation: &str,
) -> Result<roxmltree::Node<'document, 'input>, String> {
    let root = document.root_element();
    let soap_namespace = root.tag_name().namespace().unwrap_or_default();
    if !matches!(soap_namespace, SOAP11_NS | SOAP12_NS) || root.tag_name().name() != "Envelope" {
        return Err(format!("invalid {operation} response"));
    }
    let body = child(root, soap_namespace, "Body")
        .ok_or_else(|| format!("invalid {operation} response"))?;
    if let Some(fault) = child(body, soap_namespace, "Fault") {
        return Err(fault_message(fault, soap_namespace));
    }
    child(body, namespace, response_name).ok_or_else(|| format!("invalid {operation} response"))
}

fn parsed_operation_response<T>(
    xml: &str,
    namespace: &str,
    response_name: &str,
    operation: &str,
    extract: impl FnOnce(roxmltree::Node<'_, '_>) -> T,
) -> Result<T, String> {
    let document = parse_document(xml)?;
    let response = operation_response(&document, namespace, response_name, operation)?;
    Ok(extract(response))
}

// ---------------------------------------------------------------------
// Formatting (Shared Operation Contract)
// ---------------------------------------------------------------------

fn format_ptz_number(value: f64) -> Result<String, String> {
    if !value.is_finite() {
        return Err("PTZ value must be finite".to_owned());
    }
    let text = format!("{value:.6}");
    Ok(if text == "-0.000000" {
        "0.000000".to_owned()
    } else {
        text
    })
}

fn format_ptz_duration(milliseconds: f64) -> Result<String, String> {
    if !milliseconds.is_finite() || milliseconds <= 0.0 {
        return Err("PTZ timeout must be finite and greater than 0".to_owned());
    }
    Ok(format!("PT{:.3}S", milliseconds / 1000.0))
}

fn require_finite_in_range(value: f64, range: (f64, f64)) -> Result<f64, String> {
    if !value.is_finite() {
        return Err("PTZ value must be finite".to_owned());
    }
    if value < range.0 || value > range.1 {
        return Err("PTZ value must be within its valid range".to_owned());
    }
    Ok(value)
}

fn validated_vector(
    vector: Option<PtzVector>,
    range: (f64, f64),
) -> Result<Option<PtzVector>, String> {
    let Some(vector) = vector else {
        return Ok(None);
    };
    Ok(Some(PtzVector {
        x: require_finite_in_range(vector.x, range)?,
        y: require_finite_in_range(vector.y, range)?,
    }))
}

fn validated_zoom(zoom: Option<f64>, range: (f64, f64)) -> Result<Option<f64>, String> {
    let Some(zoom) = zoom else { return Ok(None) };
    Ok(Some(require_finite_in_range(zoom, range)?))
}

fn vector_xml(tag: &str, pan_tilt: Option<PtzVector>, zoom: Option<f64>) -> Result<String, String> {
    let mut inner = String::new();
    if let Some(vector) = pan_tilt {
        inner.push_str(&format!(
            "<PanTilt xmlns=\"{SCHEMA_NS}\" x=\"{}\" y=\"{}\"/>",
            format_ptz_number(vector.x)?,
            format_ptz_number(vector.y)?,
        ));
    }
    if let Some(zoom) = zoom {
        inner.push_str(&format!(
            "<Zoom xmlns=\"{SCHEMA_NS}\" x=\"{}\"/>",
            format_ptz_number(zoom)?
        ));
    }
    Ok(format!("<{tag}>{inner}</{tag}>"))
}

fn assert_move_shape(pan_tilt: Option<PtzVector>, zoom: Option<f64>) -> Result<(), String> {
    if pan_tilt.is_none() && zoom.is_none() {
        return Err("PTZ move requires pan/tilt or zoom".to_owned());
    }
    Ok(())
}

// ---------------------------------------------------------------------
// GetNodes / GetServices / GetProfiles response parsing
// ---------------------------------------------------------------------

type SpaceField = (&'static str, fn(&mut PtzSpaces) -> &mut bool);

const PTZ_SPACE_FIELDS: [SpaceField; 6] = [
    ("AbsolutePanTiltPositionSpace", |spaces| {
        &mut spaces.absolute_pan_tilt
    }),
    ("AbsoluteZoomPositionSpace", |spaces| {
        &mut spaces.absolute_zoom
    }),
    ("RelativePanTiltTranslationSpace", |spaces| {
        &mut spaces.relative_pan_tilt
    }),
    ("RelativeZoomTranslationSpace", |spaces| {
        &mut spaces.relative_zoom
    }),
    ("ContinuousPanTiltVelocitySpace", |spaces| {
        &mut spaces.continuous_pan_tilt
    }),
    ("ContinuousZoomVelocitySpace", |spaces| {
        &mut spaces.continuous_zoom
    }),
];

fn ptz_spaces(element: roxmltree::Node<'_, '_>) -> PtzSpaces {
    let mut spaces = PtzSpaces::default();
    for (name, field) in PTZ_SPACE_FIELDS {
        *field(&mut spaces) = child(element, SCHEMA_NS, name).is_some();
    }
    spaces
}

fn parse_node(element: roxmltree::Node<'_, '_>) -> Option<PtzNode> {
    let token = unqualified_attribute(element, "token")
        .filter(|token| !token.is_empty())?
        .to_owned();
    let spaces_element = child(element, SCHEMA_NS, "SupportedPTZSpaces")?;
    let auxiliary_commands = children(element, SCHEMA_NS, "AuxiliaryCommands")
        .filter_map(|node| plain_text(Some(node)))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    Some(PtzNode {
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
    })
}

fn parse_nodes_response(response: roxmltree::Node<'_, '_>) -> Result<Vec<PtzNode>, String> {
    let mut nodes = Vec::new();
    for element in children(response, PTZ_NS, "PTZNode") {
        nodes.push(parse_node(element).ok_or_else(|| "invalid PTZ GetNodes response".to_owned())?);
    }
    nodes.sort_by(|left, right| left.token.cmp(&right.token));
    Ok(nodes)
}

fn find_service_xaddr(response: roxmltree::Node<'_, '_>, namespace: &str) -> Option<String> {
    children(response, DEV_NS, "Service").find_map(|service| {
        let service_namespace = plain_text(child(service, DEV_NS, "Namespace"))?;
        if service_namespace != namespace {
            return None;
        }
        plain_text(child(service, DEV_NS, "XAddr"))
    })
}

fn find_media1_profile_token(response: roxmltree::Node<'_, '_>) -> Option<String> {
    children(response, MEDIA1_NS, "Profiles").find_map(|profile| {
        let token = unqualified_attribute(profile, "token").filter(|token| !token.is_empty())?;
        child(profile, SCHEMA_NS, "PTZConfiguration")?;
        Some(token.to_owned())
    })
}

// Mirrors capabilities.rs's Media2 profile parsing: PTZ binding lives at
// Profiles/Configurations/PTZ, not directly on Profiles like Media1.
fn find_media2_profile_token(response: roxmltree::Node<'_, '_>) -> Option<String> {
    children(response, MEDIA2_NS, "Profiles").find_map(|profile| {
        let token = unqualified_attribute(profile, "token").filter(|token| !token.is_empty())?;
        let configurations = child(profile, MEDIA2_NS, "Configurations")?;
        child(configurations, MEDIA2_NS, "PTZ")?;
        Some(token.to_owned())
    })
}

fn number_attribute(node: Option<roxmltree::Node<'_, '_>>, name: &str) -> Option<f64> {
    let raw = unqualified_attribute(node?, name)?;
    let value = xml_scalar(Some(raw))?.parse::<f64>().ok()?;
    value.is_finite().then_some(value)
}

fn parse_status(response: roxmltree::Node<'_, '_>) -> PtzStatus {
    let Some(status) = child(response, PTZ_NS, "PTZStatus") else {
        return PtzStatus::default();
    };
    let position = child(status, SCHEMA_NS, "Position");
    let pan_tilt_position = position.and_then(|node| child(node, SCHEMA_NS, "PanTilt"));
    let zoom_position = position.and_then(|node| child(node, SCHEMA_NS, "Zoom"));
    let x = number_attribute(pan_tilt_position, "x");
    let y = number_attribute(pan_tilt_position, "y");
    let zoom = number_attribute(zoom_position, "x");
    let move_status = child(status, SCHEMA_NS, "MoveStatus");
    let pan_tilt_move_status =
        move_status.and_then(|node| plain_text(child(node, SCHEMA_NS, "PanTilt")));
    let zoom_move_status = move_status.and_then(|node| plain_text(child(node, SCHEMA_NS, "Zoom")));
    let utc_time = plain_text(child(status, SCHEMA_NS, "UtcTime"));
    // Deliberately not read: <Error> — GetStatusResponse carries a
    // camera-supplied error string; it must never surface in PtzStatus.
    PtzStatus {
        pan_tilt: match (x, y) {
            (Some(x), Some(y)) => Some(PtzVector { x, y }),
            _ => None,
        },
        zoom,
        pan_tilt_move_status,
        zoom_move_status,
        utc_time,
    }
}

// ---------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------

/// A guarded ONVIF PTZ movement control session.
///
/// Opened once via [`open_ptz_session`], which caches the resolved profile
/// token and the PTZ node's supported spaces. Every move method rejects an
/// operation the camera did not advertise, and every method fails once the
/// session is closed.
///
/// # Experimental
///
/// Physical movement is unverified against real PTZ hardware. Request
/// construction, capability guarding, the device-side move timeout, and
/// stop-on-close are covered by tests; that a camera actually moves as
/// intended is not.
pub struct PtzSession {
    device: OnvifDevice,
    ptz_xaddr: String,
    node: PtzNode,
    profile_token: String,
    default_move_timeout_ms: f64,
    closed: bool,
}

impl std::fmt::Debug for PtzSession {
    // Hand-written, and deliberately omits `device` and `ptz_xaddr`, so
    // `Result<PtzSession, _>::unwrap_err()` works in tests without ever
    // being able to print a credential, hostname, or URL (`OnvifDevice`
    // holds the camera's password; deriving Debug on it would print it).
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("PtzSession")
            .field("node", &self.node)
            .field("profile_token", &self.profile_token)
            .field("closed", &self.closed)
            .finish_non_exhaustive()
    }
}

impl PtzSession {
    pub fn node(&self) -> &PtzNode {
        &self.node
    }

    pub fn profile_token(&self) -> &str {
        &self.profile_token
    }

    fn ensure_open(&self) -> Result<(), String> {
        if self.closed {
            return Err("PTZ session is closed".to_owned());
        }
        Ok(())
    }

    fn profile_token_xml(&self) -> String {
        super::xml_escape(&self.profile_token)
    }

    fn call(&self, body: &str) -> Result<String, String> {
        let (_status, xml) = self.device.soap_response(&self.ptz_xaddr, body, true)?;
        Ok(xml)
    }

    pub fn continuous_move(
        &self,
        pan_tilt: Option<PtzVector>,
        zoom: Option<f64>,
        timeout_ms: Option<f64>,
    ) -> Result<(), String> {
        self.ensure_open()?;
        assert_move_shape(pan_tilt, zoom)?;
        if pan_tilt.is_some() && !self.node.spaces.continuous_pan_tilt {
            return Err("PTZ continuous pan/tilt is not supported".to_owned());
        }
        if zoom.is_some() && !self.node.spaces.continuous_zoom {
            return Err("PTZ continuous zoom is not supported".to_owned());
        }
        let pan_tilt = validated_vector(pan_tilt, PAN_TILT_RANGE)?;
        let zoom = validated_zoom(zoom, ZOOM_GENERIC_RANGE)?;
        let timeout = format_ptz_duration(timeout_ms.unwrap_or(self.default_move_timeout_ms))?;
        let body = format!(
            "<ContinuousMove xmlns=\"{PTZ_NS}\"><ProfileToken>{}</ProfileToken>{}<Timeout>{timeout}</Timeout></ContinuousMove>",
            self.profile_token_xml(),
            vector_xml("Velocity", pan_tilt, zoom)?,
        );
        let xml = self.call(&body)?;
        parsed_operation_response(
            &xml,
            PTZ_NS,
            "ContinuousMoveResponse",
            "PTZ ContinuousMove",
            |_| (),
        )
    }

    pub fn absolute_move(
        &self,
        pan_tilt: Option<PtzVector>,
        zoom: Option<f64>,
        speed_pan_tilt: Option<PtzVector>,
        speed_zoom: Option<f64>,
    ) -> Result<(), String> {
        self.ensure_open()?;
        assert_move_shape(pan_tilt, zoom)?;
        if pan_tilt.is_some() && !self.node.spaces.absolute_pan_tilt {
            return Err("PTZ absolute pan/tilt is not supported".to_owned());
        }
        if zoom.is_some() && !self.node.spaces.absolute_zoom {
            return Err("PTZ absolute zoom is not supported".to_owned());
        }
        let position_pan_tilt = validated_vector(pan_tilt, PAN_TILT_RANGE)?;
        let position_zoom = validated_zoom(zoom, ZOOM_POSITION_RANGE)?;
        let has_speed = speed_pan_tilt.is_some() || speed_zoom.is_some();
        let speed_pan_tilt = validated_vector(speed_pan_tilt, PAN_TILT_RANGE)?;
        let speed_zoom = validated_zoom(speed_zoom, ZOOM_GENERIC_RANGE)?;
        let speed_xml = if has_speed {
            vector_xml("Speed", speed_pan_tilt, speed_zoom)?
        } else {
            String::new()
        };
        let body = format!(
            "<AbsoluteMove xmlns=\"{PTZ_NS}\"><ProfileToken>{}</ProfileToken>{}{speed_xml}</AbsoluteMove>",
            self.profile_token_xml(),
            vector_xml("Position", position_pan_tilt, position_zoom)?,
        );
        let xml = self.call(&body)?;
        parsed_operation_response(
            &xml,
            PTZ_NS,
            "AbsoluteMoveResponse",
            "PTZ AbsoluteMove",
            |_| (),
        )
    }

    pub fn relative_move(
        &self,
        pan_tilt: Option<PtzVector>,
        zoom: Option<f64>,
        speed_pan_tilt: Option<PtzVector>,
        speed_zoom: Option<f64>,
    ) -> Result<(), String> {
        self.ensure_open()?;
        assert_move_shape(pan_tilt, zoom)?;
        if pan_tilt.is_some() && !self.node.spaces.relative_pan_tilt {
            return Err("PTZ relative pan/tilt is not supported".to_owned());
        }
        if zoom.is_some() && !self.node.spaces.relative_zoom {
            return Err("PTZ relative zoom is not supported".to_owned());
        }
        let translation_pan_tilt = validated_vector(pan_tilt, PAN_TILT_RANGE)?;
        let translation_zoom = validated_zoom(zoom, ZOOM_GENERIC_RANGE)?;
        let has_speed = speed_pan_tilt.is_some() || speed_zoom.is_some();
        let speed_pan_tilt = validated_vector(speed_pan_tilt, PAN_TILT_RANGE)?;
        let speed_zoom = validated_zoom(speed_zoom, ZOOM_GENERIC_RANGE)?;
        let speed_xml = if has_speed {
            vector_xml("Speed", speed_pan_tilt, speed_zoom)?
        } else {
            String::new()
        };
        let body = format!(
            "<RelativeMove xmlns=\"{PTZ_NS}\"><ProfileToken>{}</ProfileToken>{}{speed_xml}</RelativeMove>",
            self.profile_token_xml(),
            vector_xml("Translation", translation_pan_tilt, translation_zoom)?,
        );
        let xml = self.call(&body)?;
        parsed_operation_response(
            &xml,
            PTZ_NS,
            "RelativeMoveResponse",
            "PTZ RelativeMove",
            |_| (),
        )
    }

    pub fn stop(&self, pan_tilt: Option<bool>, zoom: Option<bool>) -> Result<(), String> {
        self.ensure_open()?;
        let pan_tilt = pan_tilt.unwrap_or(true);
        let zoom = zoom.unwrap_or(true);
        let body = format!(
            "<Stop xmlns=\"{PTZ_NS}\"><ProfileToken>{}</ProfileToken><PanTilt>{pan_tilt}</PanTilt><Zoom>{zoom}</Zoom></Stop>",
            self.profile_token_xml(),
        );
        let xml = self.call(&body)?;
        parsed_operation_response(&xml, PTZ_NS, "StopResponse", "PTZ Stop", |_| ())
    }

    pub fn get_status(&self) -> Result<PtzStatus, String> {
        self.ensure_open()?;
        let body = format!(
            "<GetStatus xmlns=\"{PTZ_NS}\"><ProfileToken>{}</ProfileToken></GetStatus>",
            self.profile_token_xml(),
        );
        let xml = self.call(&body)?;
        parsed_operation_response(
            &xml,
            PTZ_NS,
            "GetStatusResponse",
            "PTZ GetStatus",
            parse_status,
        )
    }

    /// Stop both axes and mark the session closed. A failing Stop is
    /// swallowed: it must never mask a caller's in-flight error.
    pub fn close(&mut self) {
        if self.closed {
            return;
        }
        let _ = self.stop(Some(true), Some(true));
        self.closed = true;
    }
}

/// Open a PTZ control session.
///
/// **Experimental.** Physical movement is unverified against real PTZ
/// hardware. Request construction, capability guarding, the device-side move
/// timeout, and stop-on-close are covered by tests; that a camera actually
/// moves as intended is not.
pub fn open_ptz_session(options: &PtzSessionOptions) -> Result<PtzSession, String> {
    let device_urls = if options.device_urls.is_empty() {
        vec![
            format!("http://{}/onvif/device_service", options.host),
            format!("https://{}/onvif/device_service", options.host),
            format!("http://{}:8000/onvif/device_service", options.host),
        ]
    } else {
        options.device_urls.clone()
    };
    let mut device = OnvifDevice::with_device_urls_and_timeout(
        &options.host,
        &options.user,
        &options.password,
        device_urls,
        options.timeout,
    )?;
    device.connect()?;
    let device_endpoint = device.require_device_url()?.to_owned();

    let (_status, services_xml) = device.soap_response(&device_endpoint, GET_SERVICES, true)?;
    let (ptz_xaddr, media2_xaddr) = parsed_operation_response(
        &services_xml,
        DEV_NS,
        "GetServicesResponse",
        "GetServices",
        |response| {
            (
                find_service_xaddr(response, PTZ_NS),
                find_service_xaddr(response, MEDIA2_NS),
            )
        },
    )?;
    let ptz_xaddr = ptz_xaddr.ok_or_else(|| "no ONVIF PTZ service".to_owned())?;

    let (_status, nodes_xml) = device.soap_response(&ptz_xaddr, GET_NODES, true)?;
    let mut nodes = parsed_operation_response(
        &nodes_xml,
        PTZ_NS,
        "GetNodesResponse",
        "PTZ GetNodes",
        parse_nodes_response,
    )??;
    if nodes.is_empty() {
        return Err("no ONVIF PTZ node".to_owned());
    }
    let node = nodes.remove(0);

    let profile_token = match &options.profile_token {
        Some(token) => token.clone(),
        None => {
            let media1_endpoint = device.require_media_url()?.to_owned();
            let (_status, media1_xml) =
                device.soap_response(&media1_endpoint, MEDIA1_GET_PROFILES, true)?;
            let mut token = parsed_operation_response(
                &media1_xml,
                MEDIA1_NS,
                "GetProfilesResponse",
                "Media1 GetProfiles",
                find_media1_profile_token,
            )?;
            if token.is_none() {
                if let Some(media2_xaddr) = media2_xaddr.as_deref() {
                    let (_status, media2_xml) =
                        device.soap_response(media2_xaddr, MEDIA2_GET_PROFILES, true)?;
                    token = parsed_operation_response(
                        &media2_xml,
                        MEDIA2_NS,
                        "GetProfilesResponse",
                        "Media2 GetProfiles",
                        find_media2_profile_token,
                    )?;
                }
            }
            token.ok_or_else(|| "no ONVIF PTZ profile".to_owned())?
        }
    };

    Ok(PtzSession {
        device,
        ptz_xaddr,
        node,
        profile_token,
        default_move_timeout_ms: options.default_move_timeout_ms,
        closed: false,
    })
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};
    use std::thread;

    use super::*;

    const SOAP_NS: &str = "http://www.w3.org/2003/05/soap-envelope";

    #[test]
    fn formats_ptz_numbers_as_fixed_six_decimals() {
        assert_eq!(format_ptz_number(0.5).unwrap(), "0.500000");
        assert_eq!(format_ptz_number(-1.0).unwrap(), "-1.000000");
        assert_eq!(format_ptz_number(-0.0).unwrap(), "0.000000");
        assert_eq!(format_ptz_number(0.1 + 0.2).unwrap(), "0.300000");
        assert!(format_ptz_number(f64::NAN).is_err());
    }

    #[test]
    fn formats_ptz_durations_as_fixed_three_decimals() {
        assert_eq!(format_ptz_duration(1000.0).unwrap(), "PT1.000S");
        assert_eq!(format_ptz_duration(250.0).unwrap(), "PT0.250S");
        assert!(format_ptz_duration(0.0).is_err());
    }

    #[test]
    fn rejects_non_finite_or_non_positive_ptz_durations() {
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert_eq!(
                format_ptz_duration(bad).unwrap_err(),
                "PTZ timeout must be finite and greater than 0"
            );
        }
    }

    #[test]
    fn debug_formatting_of_ptz_session_options_redacts_the_password() {
        let secret_password = "super-secret-camera-password";
        let options = PtzSessionOptions::new("camera", "operator", secret_password);

        let formatted = format!("{options:?}");

        assert!(formatted.contains(REDACTED_PASSWORD_PLACEHOLDER));
        assert!(!formatted.contains(secret_password));
    }

    // -----------------------------------------------------------------
    // Fake ONVIF + PTZ server
    // -----------------------------------------------------------------

    fn soap(body: &str) -> String {
        format!(
            "<s:Envelope xmlns:s=\"{SOAP_NS}\" xmlns:tds=\"{DEV_NS}\" xmlns:tt=\"{SCHEMA_NS}\" \
             xmlns:trt=\"{MEDIA1_NS}\" xmlns:tr2=\"{MEDIA2_NS}\" xmlns:tptz=\"{PTZ_NS}\">\
             <s:Body>{body}</s:Body></s:Envelope>"
        )
    }

    fn extract_soap_body(raw_envelope: &str) -> String {
        let start = raw_envelope
            .find("<s:Body>")
            .map_or(0, |index| index + "<s:Body>".len());
        let end = raw_envelope
            .rfind("</s:Body>")
            .unwrap_or(raw_envelope.len());
        raw_envelope[start..end].to_owned()
    }

    fn read_request_body(stream: &mut impl Read) -> String {
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 4096];
        let header_end;
        loop {
            let read = stream.read(&mut chunk).unwrap();
            buffer.extend_from_slice(&chunk[..read]);
            if let Some(end) = buffer.windows(4).position(|window| window == b"\r\n\r\n") {
                header_end = end;
                break;
            }
        }
        let header = String::from_utf8_lossy(&buffer[..header_end]).into_owned();
        let content_length: usize = header
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().to_owned())
            })
            .unwrap()
            .parse()
            .unwrap();
        let total = header_end + 4 + content_length;
        while buffer.len() < total {
            let read = stream.read(&mut chunk).unwrap();
            buffer.extend_from_slice(&chunk[..read]);
        }
        extract_soap_body(std::str::from_utf8(&buffer[header_end + 4..total]).unwrap())
    }

    struct FakeServer {
        url: String,
        requests: Arc<Mutex<Vec<String>>>,
        stop: Arc<AtomicBool>,
        handle: Option<thread::JoinHandle<()>>,
    }

    impl FakeServer {
        fn request_bodies(&self) -> Vec<String> {
            self.requests.lock().unwrap().clone()
        }
    }

    impl Drop for FakeServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(handle) = self.handle.take() {
                let _ = handle.join();
            }
        }
    }

    fn spawn_fake_server(
        make_responder: impl FnOnce(String) -> Box<dyn Fn(&str) -> (u16, String) + Send>,
    ) -> FakeServer {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let url = format!("http://127.0.0.1:{port}");
        let responder = make_responder(url.clone());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let server_requests = Arc::clone(&requests);
        let server_stop = Arc::clone(&stop);
        let handle = thread::spawn(move || {
            loop {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        let body = read_request_body(&mut stream);
                        let (status, response_body) = responder(&body);
                        server_requests.lock().unwrap().push(body);
                        let _ = write!(
                            stream,
                            "HTTP/1.1 {status} X\r\nContent-Type: application/soap+xml\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            response_body.len(),
                            response_body
                        );
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        if server_stop.load(Ordering::SeqCst) {
                            break;
                        }
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        FakeServer {
            requests,
            stop,
            handle: Some(handle),
            url,
        }
    }

    fn space_elements(spaces: &PtzSpaces) -> String {
        let fields: [(bool, &str); 6] = [
            (spaces.absolute_pan_tilt, "AbsolutePanTiltPositionSpace"),
            (spaces.absolute_zoom, "AbsoluteZoomPositionSpace"),
            (spaces.relative_pan_tilt, "RelativePanTiltTranslationSpace"),
            (spaces.relative_zoom, "RelativeZoomTranslationSpace"),
            (spaces.continuous_pan_tilt, "ContinuousPanTiltVelocitySpace"),
            (spaces.continuous_zoom, "ContinuousZoomVelocitySpace"),
        ];
        fields
            .into_iter()
            .filter(|(enabled, _)| *enabled)
            .map(|(_, tag)| format!("<tt:{tag}/>"))
            .collect()
    }

    type CustomResponder = Arc<dyn Fn(&str) -> Option<(u16, String)> + Send + Sync>;

    #[derive(Clone)]
    struct FakeCameraConfig {
        spaces: PtzSpaces,
        profile_token: String,
        explicit_profile_token: bool,
        omit_ptz_service: bool,
        nodes_xml: Option<String>,
        profiles_xml: Option<String>,
        media2: bool,
        media2_profiles_xml: Option<String>,
        custom: Option<CustomResponder>,
    }

    impl Default for FakeCameraConfig {
        fn default() -> Self {
            Self {
                spaces: PtzSpaces::default(),
                profile_token: "main".to_owned(),
                explicit_profile_token: true,
                omit_ptz_service: false,
                nodes_xml: None,
                profiles_xml: None,
                media2: false,
                media2_profiles_xml: None,
                custom: None,
            }
        }
    }

    impl FakeCameraConfig {
        fn with_spaces(mut self, spaces: PtzSpaces) -> Self {
            self.spaces = spaces;
            self
        }

        fn with_profile_token(mut self, token: impl Into<String>) -> Self {
            self.profile_token = token.into();
            self
        }

        fn resolve_profile(mut self) -> Self {
            self.explicit_profile_token = false;
            self
        }

        fn omit_ptz_service(mut self) -> Self {
            self.omit_ptz_service = true;
            self
        }

        fn nodes_xml(mut self, xml: impl Into<String>) -> Self {
            self.nodes_xml = Some(xml.into());
            self
        }

        fn profiles_xml(mut self, xml: impl Into<String>) -> Self {
            self.profiles_xml = Some(xml.into());
            self
        }

        fn media2(mut self) -> Self {
            self.media2 = true;
            self
        }

        fn media2_profiles_xml(mut self, xml: impl Into<String>) -> Self {
            self.media2_profiles_xml = Some(xml.into());
            self
        }

        fn respond(
            mut self,
            responder: impl Fn(&str) -> Option<(u16, String)> + Send + Sync + 'static,
        ) -> Self {
            self.custom = Some(Arc::new(responder));
            self
        }
    }

    /// Starts a fake camera exposing device, media, and PTZ endpoints on a
    /// single listener (only request bodies are matched; paths are
    /// irrelevant, mirroring how a real single-camera deployment would
    /// route every one of these to the same host).
    fn fake_camera(config: FakeCameraConfig) -> (FakeServer, PtzSessionOptions) {
        let profile_token = config
            .explicit_profile_token
            .then(|| config.profile_token.clone());
        let server = spawn_fake_server(move |url: String| {
            Box::new(move |body: &str| -> (u16, String) {
                if let Some(custom) = &config.custom {
                    if let Some(result) = custom(body) {
                        return result;
                    }
                }
                if body.contains("GetSystemDateAndTime") {
                    return (
                        200,
                        "<Envelope><UTCDateTime><Time><Hour>0</Hour><Minute>0</Minute>\
                         <Second>0</Second></Time><Date><Year>2026</Year><Month>1</Month>\
                         <Day>1</Day></Date></UTCDateTime></Envelope>"
                            .to_owned(),
                    );
                }
                if body.contains("GetDeviceInformation") {
                    return (
                        200,
                        "<Envelope><GetDeviceInformationResponse/></Envelope>".to_owned(),
                    );
                }
                if body.contains("GetCapabilities") {
                    return (
                        200,
                        format!(
                            "<Envelope><Capabilities><Media><XAddr>{url}/media</XAddr></Media>\
                             </Capabilities></Envelope>"
                        ),
                    );
                }
                if body == GET_SERVICES {
                    let ptz_service = if config.omit_ptz_service {
                        String::new()
                    } else {
                        format!(
                            "<tds:Service><tds:Namespace>{PTZ_NS}</tds:Namespace>\
                             <tds:XAddr>{url}/ptz</tds:XAddr></tds:Service>"
                        )
                    };
                    let media2_service = if config.media2 {
                        format!(
                            "<tds:Service><tds:Namespace>{MEDIA2_NS}</tds:Namespace>\
                             <tds:XAddr>{url}/media2</tds:XAddr></tds:Service>"
                        )
                    } else {
                        String::new()
                    };
                    return (
                        200,
                        soap(&format!(
                            "<tds:GetServicesResponse>{ptz_service}{media2_service}</tds:GetServicesResponse>"
                        )),
                    );
                }
                if body == GET_NODES {
                    let xml = config.nodes_xml.clone().unwrap_or_else(|| {
                        format!(
                            "<tptz:GetNodesResponse><tptz:PTZNode token=\"node-1\">\
                             <tt:SupportedPTZSpaces>{}</tt:SupportedPTZSpaces>\
                             </tptz:PTZNode></tptz:GetNodesResponse>",
                            space_elements(&config.spaces)
                        )
                    });
                    return (200, soap(&xml));
                }
                if body == MEDIA1_GET_PROFILES {
                    let xml = config.profiles_xml.clone().unwrap_or_else(|| {
                        format!(
                            "<trt:GetProfilesResponse><trt:Profiles token=\"{}\">\
                             <tt:PTZConfiguration token=\"ptz-config\"/></trt:Profiles>\
                             </trt:GetProfilesResponse>",
                            config.profile_token
                        )
                    });
                    return (200, soap(&xml));
                }
                if body == MEDIA2_GET_PROFILES {
                    let xml = config
                        .media2_profiles_xml
                        .clone()
                        .unwrap_or_else(|| "<tr2:GetProfilesResponse/>".to_owned());
                    return (200, soap(&xml));
                }
                if body.starts_with("<ContinuousMove ") {
                    return (200, soap("<tptz:ContinuousMoveResponse/>"));
                }
                if body.starts_with("<AbsoluteMove ") {
                    return (200, soap("<tptz:AbsoluteMoveResponse/>"));
                }
                if body.starts_with("<RelativeMove ") {
                    return (200, soap("<tptz:RelativeMoveResponse/>"));
                }
                if body.starts_with("<Stop ") {
                    return (200, soap("<tptz:StopResponse/>"));
                }
                if body.starts_with("<GetStatus ") {
                    return (
                        200,
                        soap("<tptz:GetStatusResponse><tptz:PTZStatus/></tptz:GetStatusResponse>"),
                    );
                }
                panic!("fake PTZ responder: unexpected request body: {body}");
            })
        });
        let options = PtzSessionOptions {
            device_urls: vec![format!("{}/onvif/device_service", server.url)],
            profile_token,
            ..PtzSessionOptions::new("camera", "operator", "secret")
        };
        (server, options)
    }

    /// Filters a request-body log down to the session-lifecycle calls
    /// (`open_ptz_session`'s own GetServices/GetNodes/GetProfiles bodies),
    /// dropping the underlying `OnvifDevice::connect()` calls that precede
    /// them and any PTZ operation bodies that follow.
    fn lifecycle_bodies(requests: &[String]) -> Vec<String> {
        requests
            .iter()
            .filter(|body| {
                matches!(
                    body.as_str(),
                    GET_SERVICES | GET_NODES | MEDIA1_GET_PROFILES | MEDIA2_GET_PROFILES
                )
            })
            .cloned()
            .collect()
    }

    fn open(config: FakeCameraConfig) -> (FakeServer, PtzSession) {
        let (server, options) = fake_camera(config);
        let session = open_ptz_session(&options).unwrap();
        (server, session)
    }

    // -----------------------------------------------------------------
    // Session lifecycle
    // -----------------------------------------------------------------

    #[test]
    fn fails_to_open_when_no_ptz_service_is_advertised() {
        let (server, options) = fake_camera(FakeCameraConfig::default().omit_ptz_service());
        let error = open_ptz_session(&options).unwrap_err();
        assert_eq!(error, "no ONVIF PTZ service");
        assert_eq!(lifecycle_bodies(&server.request_bodies()), [GET_SERVICES]);
    }

    #[test]
    fn fails_to_open_when_get_nodes_returns_no_node() {
        let (_server, options) =
            fake_camera(FakeCameraConfig::default().nodes_xml("<tptz:GetNodesResponse/>"));
        let error = open_ptz_session(&options).unwrap_err();
        assert_eq!(error, "no ONVIF PTZ node");
    }

    #[test]
    fn resolves_the_default_profile_token_from_the_first_profile_carrying_a_ptz_configuration() {
        let (server, options) =
            fake_camera(FakeCameraConfig::default().resolve_profile().profiles_xml(
                "<trt:GetProfilesResponse><trt:Profiles token=\"no-ptz\"/>\
             <trt:Profiles token=\"has-ptz\"><tt:PTZConfiguration token=\"cfg\"/></trt:Profiles>\
             </trt:GetProfilesResponse>",
            ));
        let session = open_ptz_session(&options).unwrap();
        assert_eq!(session.profile_token(), "has-ptz");
        assert_eq!(
            lifecycle_bodies(&server.request_bodies()),
            [GET_SERVICES, GET_NODES, MEDIA1_GET_PROFILES]
        );
    }

    #[test]
    fn fails_to_open_when_no_media_profile_carries_a_ptz_configuration() {
        let (_server, options) = fake_camera(
            FakeCameraConfig::default()
                .resolve_profile()
                .profiles_xml("<trt:GetProfilesResponse><trt:Profiles token=\"no-ptz\"/></trt:GetProfilesResponse>"),
        );
        let error = open_ptz_session(&options).unwrap_err();
        assert_eq!(error, "no ONVIF PTZ profile");
    }

    #[test]
    fn falls_back_to_media2_and_resolves_a_ptz_capable_profile_media1_does_not_have() {
        let (server, options) = fake_camera(
            FakeCameraConfig::default()
                .resolve_profile()
                .profiles_xml(
                    "<trt:GetProfilesResponse><trt:Profiles token=\"media1-no-ptz\"/></trt:GetProfilesResponse>",
                )
                .media2()
                .media2_profiles_xml(
                    "<tr2:GetProfilesResponse>\
                     <tr2:Profiles token=\"media2-no-ptz\"><tr2:Configurations/></tr2:Profiles>\
                     <tr2:Profiles token=\"media2-has-ptz\"><tr2:Configurations>\
                     <tr2:PTZ token=\"ptz-two\"/></tr2:Configurations></tr2:Profiles>\
                     </tr2:GetProfilesResponse>",
                ),
        );
        let session = open_ptz_session(&options).unwrap();
        assert_eq!(session.profile_token(), "media2-has-ptz");
        assert_eq!(
            lifecycle_bodies(&server.request_bodies()),
            [
                GET_SERVICES,
                GET_NODES,
                MEDIA1_GET_PROFILES,
                MEDIA2_GET_PROFILES
            ]
        );
    }

    #[test]
    fn fails_to_open_when_both_media1_and_media2_have_no_ptz_capable_profile() {
        let (server, options) = fake_camera(
            FakeCameraConfig::default()
                .resolve_profile()
                .profiles_xml(
                    "<trt:GetProfilesResponse><trt:Profiles token=\"media1-no-ptz\"/></trt:GetProfilesResponse>",
                )
                .media2()
                .media2_profiles_xml(
                    "<tr2:GetProfilesResponse><tr2:Profiles token=\"media2-no-ptz\">\
                     <tr2:Configurations/></tr2:Profiles></tr2:GetProfilesResponse>",
                ),
        );
        let error = open_ptz_session(&options).unwrap_err();
        assert_eq!(error, "no ONVIF PTZ profile");
        assert_eq!(
            lifecycle_bodies(&server.request_bodies()),
            [
                GET_SERVICES,
                GET_NODES,
                MEDIA1_GET_PROFILES,
                MEDIA2_GET_PROFILES
            ]
        );
    }

    #[test]
    fn skips_media1_get_profiles_entirely_when_an_explicit_profile_token_is_given() {
        let (server, options) =
            fake_camera(FakeCameraConfig::default().with_profile_token("explicit-token"));
        let session = open_ptz_session(&options).unwrap();
        assert_eq!(session.profile_token(), "explicit-token");
        assert_eq!(
            lifecycle_bodies(&server.request_bodies()),
            [GET_SERVICES, GET_NODES]
        );
    }

    #[test]
    fn exposes_the_cached_ptz_node_and_resolved_profile_token_on_the_session() {
        let (_server, session) = open(
            FakeCameraConfig::default()
                .with_profile_token("fixed-token")
                .with_spaces(PtzSpaces {
                    continuous_pan_tilt: true,
                    ..PtzSpaces::default()
                }),
        );
        assert_eq!(session.profile_token(), "fixed-token");
        assert_eq!(session.node().token, "node-1");
        assert!(session.node().spaces.continuous_pan_tilt);
        assert!(!session.node().spaces.absolute_pan_tilt);
    }

    // -----------------------------------------------------------------
    // Guards
    // -----------------------------------------------------------------

    #[test]
    fn rejects_every_unsupported_guard_in_the_table_with_zero_additional_requests() {
        struct Case {
            message: &'static str,
            invoke: fn(&PtzSession) -> Result<(), String>,
        }
        let cases = [
            Case {
                message: "PTZ continuous pan/tilt is not supported",
                invoke: |session| {
                    session.continuous_move(Some(PtzVector { x: 0.0, y: 0.0 }), None, None)
                },
            },
            Case {
                message: "PTZ continuous zoom is not supported",
                invoke: |session| session.continuous_move(None, Some(0.5), None),
            },
            Case {
                message: "PTZ absolute pan/tilt is not supported",
                invoke: |session| {
                    session.absolute_move(Some(PtzVector { x: 0.0, y: 0.0 }), None, None, None)
                },
            },
            Case {
                message: "PTZ absolute zoom is not supported",
                invoke: |session| session.absolute_move(None, Some(0.5), None, None),
            },
            Case {
                message: "PTZ relative pan/tilt is not supported",
                invoke: |session| {
                    session.relative_move(Some(PtzVector { x: 0.0, y: 0.0 }), None, None, None)
                },
            },
            Case {
                message: "PTZ relative zoom is not supported",
                invoke: |session| session.relative_move(None, Some(0.5), None, None),
            },
        ];

        // Every space defaults to false, so each case above always
        // exercises an unsupported guard regardless of which one it names.
        for case in cases {
            let (server, session) = open(FakeCameraConfig::default());
            let before = server.request_bodies().len();
            assert_eq!((case.invoke)(&session).unwrap_err(), case.message);
            assert_eq!(server.request_bodies().len(), before);
        }
    }

    #[test]
    fn rejects_a_move_with_neither_pan_tilt_nor_zoom_without_sending_a_request() {
        let (server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_pan_tilt: true,
            continuous_zoom: true,
            ..PtzSpaces::default()
        }));
        let before = server.request_bodies().len();

        assert_eq!(
            session.continuous_move(None, None, None).unwrap_err(),
            "PTZ move requires pan/tilt or zoom"
        );
        assert_eq!(
            session.absolute_move(None, None, None, None).unwrap_err(),
            "PTZ move requires pan/tilt or zoom"
        );
        assert_eq!(
            session.relative_move(None, None, None, None).unwrap_err(),
            "PTZ move requires pan/tilt or zoom"
        );
        assert_eq!(server.request_bodies().len(), before);
    }

    #[test]
    fn rejects_out_of_range_values_without_sending_a_request() {
        let (server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_pan_tilt: true,
            ..PtzSpaces::default()
        }));
        let before = server.request_bodies().len();
        for bad in [1.5, -1.5, f64::NAN, f64::INFINITY] {
            assert!(
                session
                    .continuous_move(Some(PtzVector { x: bad, y: 0.0 }), None, None)
                    .is_err()
            );
        }
        assert_eq!(server.request_bodies().len(), before);
    }

    #[test]
    fn rejects_an_absolute_zoom_position_out_of_range_while_the_same_continuous_zoom_velocity_is_in_range()
     {
        let (_server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            absolute_zoom: true,
            continuous_zoom: true,
            ..PtzSpaces::default()
        }));

        assert!(session.absolute_move(None, Some(-0.5), None, None).is_err());
        session.continuous_move(None, Some(-0.5), None).unwrap();
    }

    // -----------------------------------------------------------------
    // Request body construction
    // -----------------------------------------------------------------

    #[test]
    fn sends_a_continuous_move_with_the_default_timeout_and_correct_number_formatting() {
        let (server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_pan_tilt: true,
            ..PtzSpaces::default()
        }));
        session
            .continuous_move(Some(PtzVector { x: 0.5, y: -0.25 }), None, None)
            .unwrap();

        let bodies = server.request_bodies();
        assert_eq!(
            bodies.last().unwrap(),
            &format!(
                "<ContinuousMove xmlns=\"{PTZ_NS}\"><ProfileToken>main</ProfileToken>\
                 <Velocity><PanTilt xmlns=\"{SCHEMA_NS}\" x=\"0.500000\" y=\"-0.250000\"/></Velocity>\
                 <Timeout>PT1.000S</Timeout></ContinuousMove>"
            )
        );
    }

    #[test]
    fn continuous_move_sends_an_explicit_per_call_timeout_in_the_body() {
        // The Timeout element is the runaway guard: it must reach the wire
        // as the value the caller actually asked for, not a hardcoded
        // default. Every other test in this suite uses the 1000ms default,
        // so without this test PT1.000S could be hardcoded undetected.
        let (server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_zoom: true,
            ..PtzSpaces::default()
        }));
        session
            .continuous_move(None, Some(0.5), Some(1500.0))
            .unwrap();

        let bodies = server.request_bodies();
        assert!(
            bodies
                .last()
                .unwrap()
                .contains("<Timeout>PT1.500S</Timeout>")
        );
    }

    #[test]
    fn continuous_move_uses_the_session_level_default_timeout_in_the_body() {
        let (server, options) = fake_camera(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_zoom: true,
            ..PtzSpaces::default()
        }));
        let session = open_ptz_session(&PtzSessionOptions {
            default_move_timeout_ms: 250.0,
            ..options
        })
        .unwrap();
        session.continuous_move(None, Some(0.5), None).unwrap();

        let bodies = server.request_bodies();
        assert!(
            bodies
                .last()
                .unwrap()
                .contains("<Timeout>PT0.250S</Timeout>")
        );
    }

    #[test]
    fn builds_an_absolute_move_body_with_position_optional_speed_and_no_timeout() {
        let (server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            absolute_pan_tilt: true,
            absolute_zoom: true,
            ..PtzSpaces::default()
        }));
        session
            .absolute_move(
                Some(PtzVector { x: -1.0, y: 1.0 }),
                Some(0.25),
                Some(PtzVector { x: 0.1, y: 0.2 }),
                Some(0.3),
            )
            .unwrap();

        let bodies = server.request_bodies();
        let body = bodies.last().unwrap();
        assert_eq!(
            body,
            &format!(
                "<AbsoluteMove xmlns=\"{PTZ_NS}\"><ProfileToken>main</ProfileToken>\
                 <Position><PanTilt xmlns=\"{SCHEMA_NS}\" x=\"-1.000000\" y=\"1.000000\"/>\
                 <Zoom xmlns=\"{SCHEMA_NS}\" x=\"0.250000\"/></Position>\
                 <Speed><PanTilt xmlns=\"{SCHEMA_NS}\" x=\"0.100000\" y=\"0.200000\"/>\
                 <Zoom xmlns=\"{SCHEMA_NS}\" x=\"0.300000\"/></Speed></AbsoluteMove>"
            )
        );
        assert!(!body.contains("Timeout"));
    }

    #[test]
    fn builds_a_relative_move_body_with_translation_and_omits_absent_fields() {
        let (server, session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            relative_zoom: true,
            ..PtzSpaces::default()
        }));
        session.relative_move(None, Some(-0.5), None, None).unwrap();

        let bodies = server.request_bodies();
        let body = bodies.last().unwrap();
        assert_eq!(
            body,
            &format!(
                "<RelativeMove xmlns=\"{PTZ_NS}\"><ProfileToken>main</ProfileToken>\
                 <Translation><Zoom xmlns=\"{SCHEMA_NS}\" x=\"-0.500000\"/></Translation></RelativeMove>"
            )
        );
        for absent in ["PanTilt", "Speed", "Timeout"] {
            assert!(!body.contains(absent));
        }
    }

    #[test]
    fn sends_stop_with_explicit_per_axis_booleans_and_get_status_with_only_the_profile_token() {
        let (server, session) = open(FakeCameraConfig::default());
        session.stop(Some(true), Some(false)).unwrap();
        assert_eq!(
            server.request_bodies().last().unwrap(),
            &format!(
                "<Stop xmlns=\"{PTZ_NS}\"><ProfileToken>main</ProfileToken>\
                 <PanTilt>true</PanTilt><Zoom>false</Zoom></Stop>"
            )
        );

        session.get_status().unwrap();
        assert_eq!(
            server.request_bodies().last().unwrap(),
            &format!("<GetStatus xmlns=\"{PTZ_NS}\"><ProfileToken>main</ProfileToken></GetStatus>")
        );
    }

    #[test]
    fn escapes_a_profile_token_containing_quotes_ampersands_and_angle_brackets() {
        // Matches the shared reference behaviour byte for byte: escaping
        // only `&`, `<`, `>` (as e.g. a naive escaper might) would leave the
        // quote and apostrophe untouched, diverging from the TypeScript and
        // Python request bytes for a token containing either character.
        let (server, session) =
            open(FakeCameraConfig::default().with_profile_token("a\"b'c&d<e>f"));
        session.get_status().unwrap();

        assert_eq!(
            server.request_bodies().last().unwrap(),
            &format!(
                "<GetStatus xmlns=\"{PTZ_NS}\"><ProfileToken>a&quot;b&apos;c&amp;d&lt;e&gt;f</ProfileToken></GetStatus>"
            )
        );
    }

    // -----------------------------------------------------------------
    // GetStatus parsing
    // -----------------------------------------------------------------

    #[test]
    fn parses_position_move_status_and_utc_time_from_get_status() {
        let (_server, session) = open(FakeCameraConfig::default().respond(|body| {
            body.starts_with("<GetStatus ").then(|| {
                (
                    200,
                    soap(
                        "<tptz:GetStatusResponse><tptz:PTZStatus>\
                         <tt:Position><tt:PanTilt x=\"0.25\" y=\"-0.5\"/><tt:Zoom x=\"0.75\"/></tt:Position>\
                         <tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>MOVING</tt:Zoom></tt:MoveStatus>\
                         <tt:UtcTime>2026-08-10T00:00:00Z</tt:UtcTime>\
                         </tptz:PTZStatus></tptz:GetStatusResponse>",
                    ),
                )
            })
        }));

        let status = session.get_status().unwrap();
        assert_eq!(
            status,
            PtzStatus {
                pan_tilt: Some(PtzVector { x: 0.25, y: -0.5 }),
                zoom: Some(0.75),
                pan_tilt_move_status: Some("IDLE".to_owned()),
                zoom_move_status: Some("MOVING".to_owned()),
                utc_time: Some("2026-08-10T00:00:00Z".to_owned()),
            }
        );
    }

    #[test]
    fn never_surfaces_a_camera_supplied_get_status_error_anywhere_in_ptz_status() {
        let secret_error = "internal-diagnostic-marker-should-not-leak";
        let (_server, session) = open(FakeCameraConfig::default().respond(move |body| {
            body.starts_with("<GetStatus ").then(|| {
                (
                    200,
                    soap(&format!(
                        "<tptz:GetStatusResponse><tptz:PTZStatus>\
                         <tt:Error>{secret_error}</tt:Error>\
                         <tt:UtcTime>2026-08-10T00:00:00Z</tt:UtcTime>\
                         </tptz:PTZStatus></tptz:GetStatusResponse>"
                    )),
                )
            })
        }));

        let status = session.get_status().unwrap();
        assert_eq!(status.utc_time.as_deref(), Some("2026-08-10T00:00:00Z"));
        assert!(!format!("{status:?}").contains(secret_error));
    }

    #[test]
    fn classifies_a_ptz_soap_fault_the_same_way_the_capability_report_does() {
        let (_server, session) = open(FakeCameraConfig::default().respond(|body| {
            body.starts_with("<GetStatus ").then(|| {
                (
                    500,
                    soap(
                        "<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>\
                         <s:Value xmlns:ter=\"http://www.onvif.org/ver10/error\">\
                         ter:ActionNotSupported</s:Value></s:Subcode></s:Code></s:Fault>",
                    ),
                )
            })
        }));

        assert_eq!(
            session.get_status().unwrap_err(),
            "SOAP Fault: ActionNotSupported"
        );
    }

    // -----------------------------------------------------------------
    // Close / session lifecycle
    // -----------------------------------------------------------------

    #[test]
    fn stops_both_axes_on_close() {
        let (server, mut session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_pan_tilt: true,
            ..PtzSpaces::default()
        }));
        session.close();

        let bodies = server.request_bodies();
        let stop_body = bodies.last().unwrap();
        assert!(stop_body.contains("<PanTilt>true</PanTilt>"));
        assert!(stop_body.contains("<Zoom>true</Zoom>"));
    }

    #[test]
    fn close_swallows_a_failing_stop_and_still_marks_the_session_closed() {
        let (_server, mut session) = open(FakeCameraConfig::default().respond(|body| {
            body.starts_with("<Stop ")
                .then(|| (500, soap("<s:Fault/>")))
        }));
        session.close();

        assert_eq!(session.get_status().unwrap_err(), "PTZ session is closed");
    }

    #[test]
    fn rejects_every_call_after_close_with_a_fixed_message_and_zero_additional_requests() {
        let (server, mut session) = open(FakeCameraConfig::default().with_spaces(PtzSpaces {
            continuous_pan_tilt: true,
            absolute_pan_tilt: true,
            relative_pan_tilt: true,
            ..PtzSpaces::default()
        }));
        session.close();
        let before = server.request_bodies().len();

        let vector = Some(PtzVector { x: 0.0, y: 0.0 });
        assert_eq!(
            session.continuous_move(vector, None, None).unwrap_err(),
            "PTZ session is closed"
        );
        assert_eq!(
            session.absolute_move(vector, None, None, None).unwrap_err(),
            "PTZ session is closed"
        );
        assert_eq!(
            session.relative_move(vector, None, None, None).unwrap_err(),
            "PTZ session is closed"
        );
        assert_eq!(
            session.stop(None, None).unwrap_err(),
            "PTZ session is closed"
        );
        assert_eq!(session.get_status().unwrap_err(), "PTZ session is closed");

        assert_eq!(server.request_bodies().len(), before);
    }
}
