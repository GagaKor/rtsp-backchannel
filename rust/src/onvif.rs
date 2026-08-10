use base64::Engine;
use chrono::{DateTime, Duration as ChronoDuration, TimeZone, Utc};
use reqwest::blocking::Client;
use serde::Serialize;
use sha1::{Digest, Sha1};
use std::io::Read;
use std::time::Duration;

use crate::rtsp::sanitize_rtsp_uri;

pub mod capabilities;
pub use capabilities::{
    CameraCapabilityOptions, CameraCapabilityProfile, CameraCapabilityReport,
    CameraCapabilityService, CameraCapabilityVersion, CameraCapabilityWarning,
    Media2CapabilityReport, PtzCapabilityReport, PtzNode, PtzServiceCapabilities, PtzSpaces,
    get_camera_capabilities,
};

// PtzNode, PtzServiceCapabilities, and PtzSpaces live here so neither
// capabilities.rs nor ptz.rs has to import the other.
mod ptz_types;

// `pub` only so the module is reachable and its API isn't flagged as dead
// code; wiring it into a `pub use` alongside capabilities' exports (README,
// CLI) is Task 7's job.
pub mod ptz;

const DEVICE_NS: &str = "http://www.onvif.org/ver10/device/wsdl";
const MEDIA_NS: &str = "http://www.onvif.org/ver10/media/wsdl";
const SCHEMA_NS: &str = "http://www.onvif.org/ver10/schema";
const MAX_ONVIF_RESPONSE_BYTES: usize = 1024 * 1024;

const WSSE_NS: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd";
const WSU_NS: &str =
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd";
const PASSWORD_DIGEST: &str = concat!(
    "http://docs.oasis-open.org/wss/2004/01/",
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
);

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

pub fn wsse_header(user: &str, password: &str, created: DateTime<Utc>, nonce: &[u8]) -> String {
    let created = created.format("%Y-%m-%dT%H:%M:%SZ").to_string();
    let mut digest = Sha1::new();
    digest.update(nonce);
    digest.update(created.as_bytes());
    digest.update(password.as_bytes());
    let digest = base64::engine::general_purpose::STANDARD.encode(digest.finalize());
    let nonce = base64::engine::general_purpose::STANDARD.encode(nonce);
    format!(
        "<wsse:Security xmlns:wsse=\"{WSSE_NS}\" xmlns:wsu=\"{WSU_NS}\">\
         <wsse:UsernameToken><wsse:Username>{}</wsse:Username>\
         <wsse:Password Type=\"{PASSWORD_DIGEST}\">{digest}</wsse:Password>\
         <wsse:Nonce>{nonce}</wsse:Nonce><wsu:Created>{created}</wsu:Created>\
         </wsse:UsernameToken></wsse:Security>",
        xml_escape(user)
    )
}

fn descendant_text<'a>(node: roxmltree::Node<'a, 'a>, name: &str) -> Option<&'a str> {
    node.descendants()
        .find(|child| child.is_element() && child.tag_name().name() == name)?
        .text()
}

pub fn parse_device_time(xml: &str) -> Result<DateTime<Utc>, String> {
    let document =
        capabilities::parse_document(xml).map_err(|_| "invalid ONVIF time XML".to_owned())?;
    let strict_response =
        compatible_operation_response(&document, DEVICE_NS, "GetSystemDateAndTimeResponse")
            .filter(|response| response.tag_name().namespace() == Some(DEVICE_NS));
    if strict_response.is_none() && is_recognized_soap_envelope(&document) {
        return Err("invalid GetSystemDateAndTime response".to_owned());
    }
    let utc = if let Some(response) = strict_response {
        let system_time = direct_child(response, DEVICE_NS, "SystemDateAndTime")
            .ok_or("ONVIF response has no SystemDateAndTime")?;
        direct_child(system_time, SCHEMA_NS, "UTCDateTime")
            .ok_or("ONVIF response has no UTCDateTime")?
    } else {
        document
            .descendants()
            .find(|node| node.is_element() && node.tag_name().name() == "UTCDateTime")
            .ok_or("ONVIF response has no UTCDateTime")?
    };
    let date = strict_response.and_then(|_| direct_child(utc, SCHEMA_NS, "Date"));
    let time = strict_response.and_then(|_| direct_child(utc, SCHEMA_NS, "Time"));
    let number = |name: &str| -> Result<u32, String> {
        let value = if strict_response.is_some() {
            let container = match name {
                "Year" | "Month" | "Day" => date,
                _ => time,
            }
            .ok_or_else(|| format!("ONVIF UTCDateTime has no {name}"))?;
            direct_child(container, SCHEMA_NS, name).and_then(|node| node.text())
        } else {
            descendant_text(utc, name)
        };
        value
            .ok_or_else(|| format!("ONVIF UTCDateTime has no {name}"))?
            .parse()
            .map_err(|_| format!("ONVIF UTCDateTime has invalid {name}"))
    };
    Utc.with_ymd_and_hms(
        number("Year")? as i32,
        number("Month")?,
        number("Day")?,
        number("Hour")?,
        number("Minute")?,
        number("Second")?,
    )
    .single()
    .ok_or_else(|| "ONVIF returned an invalid UTCDateTime".to_owned())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OnvifProfile {
    pub token: String,
    pub name: Option<String>,
    pub has_audio_encoder: bool,
    pub has_audio_output: bool,
    pub has_audio_source: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceInfo {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub manufacturer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub firmware: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub serial: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamUriOptions {
    pub host: String,
    pub user: String,
    pub password: String,
    pub device_urls: Vec<String>,
    pub timeout: Duration,
}

impl StreamUriOptions {
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
pub struct StreamUri {
    pub profile_token: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile_name: Option<String>,
    pub uri: String,
}

pub fn parse_profiles(xml: &str) -> Result<Vec<OnvifProfile>, String> {
    let document =
        capabilities::parse_document(xml).map_err(|_| "invalid GetProfiles XML".to_owned())?;
    let root = document.root_element();
    let strict_response = compatible_operation_response(&document, MEDIA_NS, "GetProfilesResponse")
        .filter(|response| response.tag_name().namespace() == Some(MEDIA_NS))
        .or_else(|| {
            (root.tag_name().namespace() == Some(MEDIA_NS)
                && root.tag_name().name() == "GetProfilesResponse")
                .then_some(root)
        });
    if strict_response.is_none() && is_recognized_soap_envelope(&document) {
        return Err("invalid GetProfiles response".to_owned());
    }
    let profiles = if let Some(response) = strict_response {
        response
            .children()
            .filter(|node| {
                node.is_element()
                    && node.tag_name().namespace() == Some(MEDIA_NS)
                    && node.tag_name().name() == "Profiles"
            })
            .collect::<Vec<_>>()
    } else {
        document
            .descendants()
            .filter(|node| node.is_element() && node.tag_name().name() == "Profiles")
            .collect::<Vec<_>>()
    };
    Ok(profiles
        .into_iter()
        .filter_map(|node| {
            let token = node.attribute("token")?.to_owned();
            let has_element = |name: &str| {
                if strict_response.is_some() {
                    direct_child(node, SCHEMA_NS, name).is_some()
                } else {
                    node.descendants()
                        .any(|child| child.is_element() && child.tag_name().name() == name)
                }
            };
            Some(OnvifProfile {
                token,
                name: if strict_response.is_some() {
                    direct_text(node, SCHEMA_NS, "Name")
                } else {
                    descendant_text(node, "Name").map(str::to_owned)
                },
                has_audio_encoder: has_element("AudioEncoderConfiguration"),
                has_audio_output: has_element("AudioOutputConfiguration"),
                has_audio_source: has_element("AudioSourceConfiguration"),
            })
        })
        .collect())
}

pub fn parse_profile_tokens(xml: &str) -> Result<Vec<String>, String> {
    Ok(parse_profiles(xml)?
        .into_iter()
        .map(|profile| profile.token)
        .collect())
}

pub struct OnvifDevice {
    user: String,
    password: String,
    device_urls: Vec<String>,
    client: Client,
    device_url: Option<String>,
    media_url: Option<String>,
    clock_offset: ChronoDuration,
}

impl OnvifDevice {
    pub fn new(host: &str, user: &str, password: &str) -> Result<Self, String> {
        Self::with_device_urls(
            host,
            user,
            password,
            vec![
                format!("http://{host}/onvif/device_service"),
                format!("https://{host}/onvif/device_service"),
                format!("http://{host}:8000/onvif/device_service"),
            ],
        )
    }

    pub fn with_device_urls(
        host: &str,
        user: &str,
        password: &str,
        device_urls: Vec<String>,
    ) -> Result<Self, String> {
        Self::with_device_urls_and_timeout(
            host,
            user,
            password,
            device_urls,
            Duration::from_secs(8),
        )
    }

    pub fn with_device_urls_and_timeout(
        _host: &str,
        user: &str,
        password: &str,
        device_urls: Vec<String>,
        timeout: Duration,
    ) -> Result<Self, String> {
        if timeout.is_zero() {
            return Err("ONVIF timeout must be greater than zero".to_owned());
        }
        let client = Client::builder()
            .danger_accept_invalid_certs(true)
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(timeout)
            .build()
            .map_err(|error| format!("failed to build ONVIF HTTP client: {error}"))?;
        Ok(Self {
            user: user.to_owned(),
            password: password.to_owned(),
            device_urls,
            client,
            device_url: None,
            media_url: None,
            clock_offset: ChronoDuration::zero(),
        })
    }

    pub fn connect(&mut self) -> Result<(), String> {
        self.connect_with_device_info().map(|_| ())
    }

    fn connect_with_device_info(&mut self) -> Result<DeviceInfo, String> {
        self.device_url = None;
        self.media_url = None;
        self.clock_offset = ChronoDuration::zero();
        for device_url in self.device_urls.clone() {
            let result = (|| {
                let time_xml = self.soap(
                    &device_url,
                    &format!("<GetSystemDateAndTime xmlns=\"{DEVICE_NS}\"/>"),
                    false,
                )?;
                let device_time = parse_device_time(&time_xml)?;
                self.clock_offset = device_time.signed_duration_since(Utc::now());

                let information = self.soap(
                    &device_url,
                    &format!("<GetDeviceInformation xmlns=\"{DEVICE_NS}\"/>"),
                    true,
                )?;
                let device_information = parse_device_information(&information)?;

                let capabilities = self.soap(
                    &device_url,
                    &format!(
                        "<GetCapabilities xmlns=\"{DEVICE_NS}\"><Category>Media</Category></GetCapabilities>"
                    ),
                    true,
                )?;
                let media_url = parse_media_xaddr(&capabilities)?
                    .unwrap_or_else(|| device_url.replace("device_service", "media_service"));
                validate_service_url_against_device(&device_url, &media_url)?;
                Ok::<(DeviceInfo, String), String>((device_information, media_url))
            })();
            if let Ok((device_information, media_url)) = result {
                self.device_url = Some(device_url);
                self.media_url = Some(media_url);
                return Ok(device_information);
            }
        }
        Err("ONVIF connect failed".to_owned())
    }

    pub fn profile_tokens(&self) -> Result<Vec<String>, String> {
        Ok(self
            .profiles()?
            .into_iter()
            .map(|profile| profile.token)
            .collect())
    }

    pub fn profiles(&self) -> Result<Vec<OnvifProfile>, String> {
        let xml = self.soap(
            self.require_media_url()?,
            &format!("<GetProfiles xmlns=\"{MEDIA_NS}\"/>"),
            true,
        )?;
        let profiles = parse_profiles(&xml)?;
        if profiles.is_empty() {
            return Err("ONVIF returned no media profiles".to_owned());
        }
        Ok(profiles)
    }

    pub fn stream_uri(&self, profile_token: &str) -> Result<String, String> {
        let body = format!(
            "<GetStreamUri xmlns=\"{MEDIA_NS}\"><StreamSetup>\
             <Stream xmlns=\"{SCHEMA_NS}\">RTP-Unicast</Stream>\
             <Transport xmlns=\"{SCHEMA_NS}\"><Protocol>RTSP</Protocol></Transport>\
             </StreamSetup><ProfileToken>{}</ProfileToken></GetStreamUri>",
            xml_escape(profile_token)
        );
        let xml = self.soap(self.require_media_url()?, &body, true)?;
        parse_stream_uri(&xml)?.ok_or_else(|| "GetStreamUri returned no Uri".to_owned())
    }

    fn require_device_url(&self) -> Result<&str, String> {
        self.device_url
            .as_deref()
            .ok_or_else(|| "call ONVIF connect() first".to_owned())
    }

    fn require_media_url(&self) -> Result<&str, String> {
        self.media_url
            .as_deref()
            .ok_or_else(|| "call ONVIF connect() first".to_owned())
    }

    fn soap(&self, url: &str, body: &str, authenticated: bool) -> Result<String, String> {
        let (status, text) = self.soap_response(url, body, authenticated)?;
        if !status.is_success() {
            return Err(format!(
                "ONVIF request to {} returned HTTP {status}",
                safe_url(url)
            ));
        }
        Ok(text)
    }

    fn soap_response(
        &self,
        url: &str,
        body: &str,
        authenticated: bool,
    ) -> Result<(reqwest::StatusCode, String), String> {
        if let Some(device_url) = self.device_url.as_deref() {
            validate_service_url_against_device(device_url, url)?;
        } else {
            validate_service_url(url)?;
        }
        let security = if authenticated && !(self.user.is_empty() && self.password.is_empty()) {
            let mut nonce = [0u8; 16];
            rand::fill(&mut nonce[..]);
            wsse_header(
                &self.user,
                &self.password,
                Utc::now() + self.clock_offset,
                &nonce,
            )
        } else {
            String::new()
        };
        let envelope = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
             <s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\">\
             <s:Header>{security}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        );
        let response = self
            .client
            .post(url)
            .header("Content-Type", "application/soap+xml; charset=utf-8")
            .body(envelope)
            .send()
            .map_err(|_| format!("ONVIF request to {} failed", safe_url(url)))?;
        let status = response.status();
        if matches!(status.as_u16(), 401 | 403) {
            return Ok((status, String::new()));
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_ONVIF_RESPONSE_BYTES as u64)
        {
            return Err(format!(
                "ONVIF response body from {} exceeds {MAX_ONVIF_RESPONSE_BYTES} byte limit",
                safe_url(url)
            ));
        }
        let mut bytes = Vec::new();
        response
            .take((MAX_ONVIF_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| format!("failed to read ONVIF response from {}", safe_url(url)))?;
        if bytes.len() > MAX_ONVIF_RESPONSE_BYTES {
            return Err(format!(
                "ONVIF response body from {} exceeds {MAX_ONVIF_RESPONSE_BYTES} byte limit",
                safe_url(url)
            ));
        }
        let text = String::from_utf8(bytes)
            .map_err(|_| format!("ONVIF response from {} is not UTF-8", safe_url(url)))?;
        Ok((status, text))
    }
}

pub(crate) fn probe_device_service(url: &str, timeout: Duration) -> Result<(), String> {
    let device =
        OnvifDevice::with_device_urls_and_timeout("", "", "", vec![url.to_owned()], timeout)?;
    let (status, xml) = device.soap_response(
        url,
        &format!("<GetSystemDateAndTime xmlns=\"{DEVICE_NS}\"/>"),
        false,
    )?;
    if !status.is_success() {
        return Err(format!(
            "ONVIF discovery request to {} returned HTTP {status}",
            safe_url(url)
        ));
    }
    parse_device_time(&xml).map(|_| ())
}

pub fn get_stream_uris(options: &StreamUriOptions) -> Result<Vec<StreamUri>, String> {
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
    device
        .profiles()?
        .into_iter()
        .map(|profile| {
            let uri = sanitize_rtsp_uri(&device.stream_uri(&profile.token)?)?;
            Ok(StreamUri {
                profile_token: profile.token,
                profile_name: profile.name,
                uri,
            })
        })
        .collect()
}

fn safe_url(url: &str) -> String {
    if let Ok(mut parsed) = url::Url::parse(url) {
        let _ = parsed.set_username("");
        let _ = parsed.set_password(None);
        parsed.set_path("/");
        parsed.set_query(None);
        parsed.set_fragment(None);
        parsed.to_string()
    } else {
        "<invalid-url>".to_owned()
    }
}

fn validate_service_url(url: &str) -> Result<(), String> {
    validated_service_url(url).map(|_| ())
}

fn validated_service_url(url: &str) -> Result<url::Url, String> {
    if url
        .chars()
        .any(|character| character.is_control() || character.is_whitespace())
    {
        return Err("invalid ONVIF service URL".to_owned());
    }
    let parsed = url::Url::parse(url).map_err(|_| "invalid ONVIF service URL".to_owned())?;
    if !matches!(parsed.scheme(), "http" | "https")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.fragment().is_some()
        || parsed.port() == Some(0)
    {
        return Err("invalid ONVIF service URL".to_owned());
    }
    Ok(parsed)
}

fn canonical_service_host(url: &url::Url) -> Option<String> {
    match url.host()? {
        url::Host::Domain(domain) => Some(format!(
            "domain:{}",
            domain
                .strip_suffix('.')
                .unwrap_or(domain)
                .to_ascii_lowercase()
        )),
        url::Host::Ipv4(address) => Some(format!("ipv4:{address}")),
        url::Host::Ipv6(address) => Some(format!("ipv6:{address}")),
    }
}

fn validate_service_url_against_device(device_url: &str, service_url: &str) -> Result<(), String> {
    let device = validated_service_url(device_url)?;
    let service = validated_service_url(service_url)?;
    if device.scheme() != service.scheme()
        || canonical_service_host(&device) != canonical_service_host(&service)
    {
        return Err("invalid ONVIF service URL".to_owned());
    }
    Ok(())
}

fn direct_child<'document, 'input>(
    parent: roxmltree::Node<'document, 'input>,
    namespace: &str,
    local: &str,
) -> Option<roxmltree::Node<'document, 'input>> {
    parent.children().find(|node| {
        node.is_element()
            && node.tag_name().namespace().unwrap_or_default() == namespace
            && node.tag_name().name() == local
    })
}

fn is_recognized_soap_envelope(document: &roxmltree::Document<'_>) -> bool {
    const SOAP11_NS: &str = "http://schemas.xmlsoap.org/soap/envelope/";
    const SOAP12_NS: &str = "http://www.w3.org/2003/05/soap-envelope";
    let root = document.root_element();
    root.tag_name().name() == "Envelope"
        && matches!(root.tag_name().namespace(), Some(SOAP11_NS | SOAP12_NS))
}

fn compatible_operation_response<'document, 'input>(
    document: &'document roxmltree::Document<'input>,
    namespace: &str,
    local: &str,
) -> Option<roxmltree::Node<'document, 'input>> {
    const SOAP11_NS: &str = "http://schemas.xmlsoap.org/soap/envelope/";
    const SOAP12_NS: &str = "http://www.w3.org/2003/05/soap-envelope";
    let root = document.root_element();
    let root_namespace = root.tag_name().namespace().unwrap_or_default();
    if root.tag_name().name() != "Envelope" {
        return None;
    }
    if matches!(root_namespace, SOAP11_NS | SOAP12_NS) {
        let body = direct_child(root, root_namespace, "Body")?;
        direct_child(body, namespace, local)
    } else if root_namespace.is_empty() {
        let container = direct_child(root, "", "Body").unwrap_or(root);
        direct_child(container, "", local)
    } else {
        None
    }
}

fn direct_text(parent: roxmltree::Node<'_, '_>, namespace: &str, local: &str) -> Option<String> {
    direct_child(parent, namespace, local)?
        .text()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn parse_device_information(xml: &str) -> Result<DeviceInfo, String> {
    let document = capabilities::parse_document(xml)
        .map_err(|_| "invalid GetDeviceInformation response".to_owned())?;
    let response =
        compatible_operation_response(&document, DEVICE_NS, "GetDeviceInformationResponse")
            .ok_or_else(|| "invalid GetDeviceInformation response".to_owned())?;
    let namespace = response.tag_name().namespace().unwrap_or_default();
    Ok(DeviceInfo {
        manufacturer: direct_text(response, namespace, "Manufacturer"),
        model: direct_text(response, namespace, "Model"),
        firmware: direct_text(response, namespace, "FirmwareVersion"),
        serial: direct_text(response, namespace, "SerialNumber"),
    })
}

fn parse_media_xaddr(xml: &str) -> Result<Option<String>, String> {
    let document = capabilities::parse_document(xml)
        .map_err(|_| "invalid GetCapabilities response".to_owned())?;
    let root = document.root_element();
    let response = compatible_operation_response(&document, DEVICE_NS, "GetCapabilitiesResponse");
    let (container, device_namespace, schema_namespace) = if let Some(response) = response {
        let namespace = response.tag_name().namespace().unwrap_or_default();
        let schema = if namespace.is_empty() { "" } else { SCHEMA_NS };
        (response, namespace, schema)
    } else if root.tag_name().namespace().is_none() && root.tag_name().name() == "Envelope" {
        (root, "", "")
    } else {
        return Err("invalid GetCapabilities response".to_owned());
    };
    let capabilities = direct_child(container, device_namespace, "Capabilities")
        .ok_or_else(|| "invalid GetCapabilities response".to_owned())?;
    let Some(media) = direct_child(capabilities, schema_namespace, "Media") else {
        return Ok(None);
    };
    Ok(direct_text(media, schema_namespace, "XAddr"))
}

fn parse_stream_uri(xml: &str) -> Result<Option<String>, String> {
    let document =
        capabilities::parse_document(xml).map_err(|_| "invalid ONVIF XML response".to_owned())?;
    let root = document.root_element();
    let strict_response =
        compatible_operation_response(&document, MEDIA_NS, "GetStreamUriResponse")
            .filter(|response| response.tag_name().namespace() == Some(MEDIA_NS))
            .or_else(|| {
                (root.tag_name().namespace() == Some(MEDIA_NS)
                    && root.tag_name().name() == "GetStreamUriResponse")
                    .then_some(root)
            });
    if strict_response.is_none() && is_recognized_soap_envelope(&document) {
        return Err("invalid GetStreamUri response".to_owned());
    }
    if let Some(response) = strict_response {
        let Some(media_uri) = direct_child(response, MEDIA_NS, "MediaUri") else {
            return Ok(None);
        };
        return Ok(direct_text(media_uri, SCHEMA_NS, "Uri"));
    }
    Ok(document
        .descendants()
        .find(|node| node.is_element() && node.tag_name().name() == "Uri")
        .and_then(|node| node.text())
        .map(str::to_owned))
}

#[cfg(test)]
mod tests {
    use std::io::{ErrorKind, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;

    use chrono::{TimeZone, Utc};

    use super::{
        OnvifDevice, parse_device_information, parse_device_time, parse_media_xaddr,
        parse_profile_tokens, parse_profiles, parse_stream_uri, probe_device_service, wsse_header,
    };

    #[test]
    fn builds_a_deterministic_wsse_password_digest() {
        let created = Utc.with_ymd_and_hms(2026, 7, 16, 0, 0, 0).unwrap();

        let header = wsse_header(
            "admin",
            "pass",
            created,
            &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        );

        assert!(header.contains("i9BQe26F+iQzWat9eChlLdU8dbU="));
        assert!(header.contains("AAECAwQFBgcICQoLDA0ODw=="));
        assert!(header.contains("2026-07-16T00:00:00Z"));
        assert!(header.contains("<wsse:Username>admin</wsse:Username>"));
        assert!(!header.contains(">pass<"));
    }

    #[test]
    fn parses_namespaced_device_time_and_profile_tokens() {
        let time = parse_device_time(
            r#"<s:Envelope xmlns:s="urn:s"><s:Body><tds:GetSystemDateAndTimeResponse xmlns:tds="urn:d"><tds:UTCDateTime><tt:Time xmlns:tt="urn:t"><tt:Hour>13</tt:Hour><tt:Minute>14</tt:Minute><tt:Second>15</tt:Second></tt:Time><tt:Date xmlns:tt="urn:t"><tt:Year>2026</tt:Year><tt:Month>7</tt:Month><tt:Day>16</tt:Day></tt:Date></tds:UTCDateTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>"#,
        )
        .unwrap();
        assert_eq!(time, Utc.with_ymd_and_hms(2026, 7, 16, 13, 14, 15).unwrap());

        let profiles = parse_profile_tokens(
            r#"<trt:GetProfilesResponse xmlns:trt="urn:m"><trt:Profiles token="main"/><trt:Profiles token="sub"/></trt:GetProfilesResponse>"#,
        )
        .unwrap();
        assert_eq!(profiles, ["main", "sub"]);
    }

    #[test]
    fn recognized_soap_envelopes_reject_wrong_operations_and_vendor_decoys() {
        const SOAP12: &str = "http://www.w3.org/2003/05/soap-envelope";
        let time_error = parse_device_time(&format!(
            "<s:Envelope xmlns:s=\"{SOAP12}\" xmlns:v=\"urn:vendor\"><s:Body><v:WrongResponse><v:UTCDateTime><v:Time><v:Hour>13</v:Hour><v:Minute>14</v:Minute><v:Second>15</v:Second></v:Time><v:Date><v:Year>2026</v:Year><v:Month>7</v:Month><v:Day>16</v:Day></v:Date></v:UTCDateTime></v:WrongResponse></s:Body></s:Envelope>"
        ))
        .unwrap_err();
        assert_eq!(time_error, "invalid GetSystemDateAndTime response");

        let profiles_error = parse_profiles(&format!(
            "<s:Envelope xmlns:s=\"{SOAP12}\" xmlns:v=\"urn:vendor\"><s:Body><v:WrongResponse><v:Profiles token=\"decoy-secret\"/></v:WrongResponse></s:Body></s:Envelope>"
        ))
        .unwrap_err();
        assert_eq!(profiles_error, "invalid GetProfiles response");
        assert!(!profiles_error.contains("decoy-secret"));

        let stream_error = parse_stream_uri(&format!(
            "<s:Envelope xmlns:s=\"{SOAP12}\" xmlns:v=\"urn:vendor\"><s:Body><v:WrongResponse><v:Uri>rtsp://camera/decoy-secret</v:Uri></v:WrongResponse></s:Body></s:Envelope>"
        ))
        .unwrap_err();
        assert_eq!(stream_error, "invalid GetStreamUri response");
        assert!(!stream_error.contains("decoy-secret"));
    }

    #[test]
    fn rejects_dtd_entities_at_every_onvif_xml_boundary_without_payload_leakage() {
        let forbidden = |xml: &str| {
            format!("<!DOCTYPE s:Envelope [<!ENTITY injected \"entity-payload-marker\">]>{xml}")
        };
        let cases = [
            parse_device_time(&forbidden(
                "<Envelope><UTCDateTime><Year>2026</Year></UTCDateTime></Envelope>",
            ))
            .map(|_| ()),
            parse_profiles(&forbidden(
                "<Envelope><GetProfilesResponse><Profiles token=\"main\"/></GetProfilesResponse></Envelope>",
            ))
            .map(|_| ()),
            parse_device_information(&forbidden(
                "<Envelope><GetDeviceInformationResponse/></Envelope>",
            ))
            .map(|_| ()),
            parse_media_xaddr(&forbidden(
                "<Envelope><Capabilities><Media><XAddr>&injected;</XAddr></Media></Capabilities></Envelope>",
            ))
            .map(|_| ()),
            parse_stream_uri(&forbidden(
                "<Envelope><GetStreamUriResponse><Uri>&injected;</Uri></GetStreamUriResponse></Envelope>",
            ))
            .map(|_| ()),
        ];

        for result in cases {
            let error = result.unwrap_err();
            assert!(!error.contains("entity-payload-marker"));
        }
    }

    #[test]
    fn resolves_a_stream_uri_through_authenticated_onvif_calls() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
        let media_url = format!("http://127.0.0.1:{port}/onvif/media_service");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let server_requests = Arc::clone(&requests);
        let server = thread::spawn(move || {
            let responses = [
                "<Envelope><UTCDateTime><Time><Hour>13</Hour><Minute>14</Minute><Second>15</Second></Time><Date><Year>2026</Year><Month>7</Month><Day>16</Day></Date></UTCDateTime></Envelope>".to_owned(),
                "<Envelope><GetDeviceInformationResponse><Model>camera</Model></GetDeviceInformationResponse></Envelope>".to_owned(),
                format!("<Envelope><Capabilities><Media><XAddr>{media_url}</XAddr></Media></Capabilities></Envelope>"),
                "<Envelope><GetProfilesResponse><Profiles token=\"main\"/><Profiles token=\"sub\"/></GetProfilesResponse></Envelope>".to_owned(),
                "<Envelope><GetStreamUriResponse><MediaUri><Uri>rtsp://127.0.0.1/live</Uri></MediaUri></GetStreamUriResponse></Envelope>".to_owned(),
            ];
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let request = read_http_request(&mut stream);
                server_requests.lock().unwrap().push(request);
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/soap+xml\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response.len(),
                    response
                )
                .unwrap();
            }
        });

        let mut device =
            OnvifDevice::with_device_urls("camera", "admin", "pass", vec![device_url]).unwrap();
        device.connect().unwrap();
        assert_eq!(device.profile_tokens().unwrap(), ["main", "sub"]);
        assert_eq!(device.stream_uri("main").unwrap(), "rtsp://127.0.0.1/live");
        server.join().unwrap();

        let requests = requests.lock().unwrap();
        assert!(requests[0].contains("GetSystemDateAndTime"));
        assert!(!requests[0].contains("wsse:Security"));
        for request in &requests[1..] {
            assert!(request.contains("wsse:Security"));
            assert!(!request.contains(">pass<"));
        }
        assert!(requests[4].contains("<ProfileToken>main</ProfileToken>"));
    }

    #[test]
    fn connect_rejects_a_connected_media_xaddr_outside_the_device_origin() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
        let untrusted_media_url = format!("https://127.0.0.1:{port}/onvif/media_service");
        let server = thread::spawn(move || {
            let responses = [
                "<Envelope><UTCDateTime><Time><Hour>13</Hour><Minute>14</Minute><Second>15</Second></Time><Date><Year>2026</Year><Month>7</Month><Day>16</Day></Date></UTCDateTime></Envelope>".to_owned(),
                "<Envelope><GetDeviceInformationResponse><Model>camera</Model></GetDeviceInformationResponse></Envelope>".to_owned(),
                format!("<Envelope><Capabilities><Media><XAddr>{untrusted_media_url}</XAddr></Media></Capabilities></Envelope>"),
            ];
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let _ = read_http_request(&mut stream);
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response.len(),
                    response
                )
                .unwrap();
            }
        });
        let mut device =
            OnvifDevice::with_device_urls("camera", "admin", "pass", vec![device_url]).unwrap();

        let error = device.connect().unwrap_err();
        server.join().unwrap();

        assert_eq!(error, "ONVIF connect failed");
    }

    #[test]
    fn omits_ws_security_when_both_credentials_are_empty() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
        let media_url = format!("http://127.0.0.1:{port}/onvif/media_service");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let server_requests = Arc::clone(&requests);
        let server = thread::spawn(move || {
            let responses = [
                "<Envelope><UTCDateTime><Time><Hour>13</Hour><Minute>14</Minute><Second>15</Second></Time><Date><Year>2026</Year><Month>7</Month><Day>16</Day></Date></UTCDateTime></Envelope>".to_owned(),
                "<Envelope><GetDeviceInformationResponse/></Envelope>".to_owned(),
                format!("<Envelope><Capabilities><Media><XAddr>{media_url}</XAddr></Media></Capabilities></Envelope>"),
            ];
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                server_requests
                    .lock()
                    .unwrap()
                    .push(read_http_request(&mut stream));
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response.len(),
                    response
                )
                .unwrap();
            }
        });

        let mut device = OnvifDevice::with_device_urls("camera", "", "", vec![device_url]).unwrap();
        device.connect().unwrap();
        server.join().unwrap();

        assert!(
            requests
                .lock()
                .unwrap()
                .iter()
                .all(|request| !request.contains("wsse:Security"))
        );
    }

    #[test]
    fn rejects_an_onvif_response_body_that_exceeds_the_limit_without_content_length() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let device_url = format!("http://127.0.0.1:{port}/onvif/device_service");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
                .unwrap();
            let oversized = vec![b'x'; 1024 * 1024 + 1];
            let _ = stream.write_all(&oversized);
        });
        let device =
            OnvifDevice::with_device_urls("127.0.0.1", "", "", vec![device_url.clone()]).unwrap();

        let error = device
            .soap(&device_url, "<GetSystemDateAndTime/>", false)
            .unwrap_err();
        server.join().unwrap();

        assert!(error.contains("response body"));
        assert!(error.contains("limit"));
    }

    #[test]
    fn device_service_probe_requires_a_success_http_status() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            let body = concat!(
                "<Envelope><UTCDateTime><Time><Hour>1</Hour>",
                "<Minute>2</Minute><Second>3</Second></Time><Date>",
                "<Year>2026</Year><Month>7</Month><Day>20</Day>",
                "</Date></UTCDateTime></Envelope>"
            );
            write!(
                stream,
                "HTTP/1.1 404 Not Found\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });

        let error = probe_device_service(
            &format!("http://127.0.0.1:{port}/onvif/device_service"),
            std::time::Duration::from_secs(1),
        )
        .unwrap_err();
        server.join().unwrap();

        assert!(error.contains("404"));
    }

    #[test]
    fn onvif_transport_errors_hide_url_paths_queries_and_client_details() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let error = probe_device_service(
            &format!("http://127.0.0.1:{port}/path-secret?token=query-secret"),
            std::time::Duration::from_secs(1),
        )
        .unwrap_err();

        assert_eq!(
            error,
            format!("ONVIF request to http://127.0.0.1:{port}/ failed")
        );
        assert!(!error.contains("path-secret"));
        assert!(!error.contains("query-secret"));
    }

    #[test]
    fn service_origin_binding_canonicalizes_hosts_and_ignores_ports_and_paths() {
        for (device_url, service_url) in [
            (
                "https://Camera.Example./onvif/device_service",
                "https://camera.example:8443/vendor/media?opaque=value",
            ),
            ("http://127.0.0.1:80/device", "http://127.0.0.1:9000/media"),
            ("http://127.1/device", "http://127.0.0.1:9000/media"),
            ("http://[0:0:0:0:0:0:0:1]/device", "http://[::1]:9000/media"),
        ] {
            super::validate_service_url_against_device(device_url, service_url).unwrap();
        }

        for (device_url, service_url) in [
            (
                "https://camera.example/device",
                "http://camera.example/media",
            ),
            (
                "https://camera.example/device",
                "https://other.example/media",
            ),
            ("https://camera.example/device", "https://127.0.0.1/media"),
            ("http://127.0.0.1/device", "http://127.0.0.2/media"),
            ("http://[::1]/device", "http://[::2]/media"),
        ] {
            assert_eq!(
                super::validate_service_url_against_device(device_url, service_url).unwrap_err(),
                "invalid ONVIF service URL"
            );
        }
    }

    #[test]
    fn device_service_probe_does_not_follow_http_redirects() {
        let redirect_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let redirect_port = redirect_listener.local_addr().unwrap().port();
        let target_listener = TcpListener::bind("127.0.0.1:0").unwrap();
        target_listener.set_nonblocking(true).unwrap();
        let target_port = target_listener.local_addr().unwrap().port();

        let redirect_server = thread::spawn(move || {
            let (mut stream, _) = redirect_listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            write!(
                stream,
                "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{target_port}/redirected\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
        });
        let target_server = thread::spawn(move || {
            let deadline = std::time::Instant::now() + std::time::Duration::from_millis(500);
            while std::time::Instant::now() < deadline {
                match target_listener.accept() {
                    Ok((mut stream, _)) => {
                        let mut request = [0u8; 4096];
                        let _ = stream.read(&mut request).unwrap();
                        let body = concat!(
                            "<Envelope><UTCDateTime><Time><Hour>1</Hour>",
                            "<Minute>2</Minute><Second>3</Second></Time><Date>",
                            "<Year>2026</Year><Month>7</Month><Day>20</Day>",
                            "</Date></UTCDateTime></Envelope>"
                        );
                        write!(
                            stream,
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        )
                        .unwrap();
                        return true;
                    }
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(std::time::Duration::from_millis(5));
                    }
                    Err(error) => panic!("redirect target failed: {error}"),
                }
            }
            false
        });

        let result = probe_device_service(
            &format!("http://127.0.0.1:{redirect_port}/onvif/device_service"),
            std::time::Duration::from_secs(1),
        );
        redirect_server.join().unwrap();
        let target_contacted = target_server.join().unwrap();

        assert!(result.is_err());
        assert!(!target_contacted);
    }

    fn read_http_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut chunk = [0u8; 4096];
        let header_end;
        loop {
            let read = stream.read(&mut chunk).unwrap();
            request.extend_from_slice(&chunk[..read]);
            if let Some(end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
                header_end = end;
                break;
            }
        }
        let header = String::from_utf8_lossy(&request[..header_end]);
        let content_length = header
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().to_owned())
            })
            .unwrap()
            .trim()
            .parse::<usize>()
            .unwrap();
        let total = header_end + 4 + content_length;
        while request.len() < total {
            let read = stream.read(&mut chunk).unwrap();
            request.extend_from_slice(&chunk[..read]);
        }
        String::from_utf8(request[..total].to_vec()).unwrap()
    }
}
