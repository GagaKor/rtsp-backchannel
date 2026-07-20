use std::collections::{BTreeSet, HashMap};
use std::io::ErrorKind;
use std::net::{IpAddr, Ipv4Addr, UdpSocket};
use std::thread;
use std::time::{Duration, Instant};

use rand::RngCore;
use serde::Serialize;

use crate::onvif::probe_device_service;

const MULTICAST_ADDRESS: Ipv4Addr = Ipv4Addr::new(239, 255, 255, 250);
const MULTICAST_PORT: u16 = 3702;
const DEFAULT_CIDR_PORTS: [u16; 3] = [80, 8000, 443];
const DEFAULT_CIDR_CONCURRENCY: usize = 64;
const MAX_CIDR_HOSTS: usize = 4096;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DiscoveredDevice {
    pub ip: Ipv4Addr,
    pub xaddrs: Vec<String>,
    pub scopes: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hardware: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub endpoint_reference: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiscoveryOptions {
    pub timeout: Duration,
    pub interfaces: Vec<Ipv4Addr>,
}

impl Default for DiscoveryOptions {
    fn default() -> Self {
        Self {
            timeout: Duration::from_secs(3),
            interfaces: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
/// Options for actively probing IPv4 networks and individual addresses.
pub struct CidrDiscoveryOptions {
    /// IPv4 CIDRs or individual IPv4 addresses. All entries are searched.
    pub cidrs: Vec<String>,
    /// ONVIF Device Service ports. Port 443 uses HTTPS; other ports use HTTP.
    pub ports: Vec<u16>,
    /// Timeout applied to each ONVIF probe request.
    pub timeout: Duration,
    /// Number of hosts scanned concurrently. Valid values are 1 through 256.
    pub concurrency: usize,
}

impl CidrDiscoveryOptions {
    /// Creates options with ports 80, 8000, and 443, a one-second timeout,
    /// and concurrency 64.
    pub fn new<I, S>(cidrs: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            cidrs: cidrs.into_iter().map(Into::into).collect(),
            ports: DEFAULT_CIDR_PORTS.to_vec(),
            timeout: Duration::from_secs(1),
            concurrency: DEFAULT_CIDR_CONCURRENCY,
        }
    }
}

fn descendant_text<'a>(node: roxmltree::Node<'a, 'a>, name: &str) -> Option<&'a str> {
    node.descendants()
        .find(|child| child.is_element() && child.tag_name().name() == name)?
        .text()
}

fn percent_decode(value: &str) -> String {
    let input = value.as_bytes();
    let mut output = Vec::with_capacity(input.len());
    let mut index = 0;
    while index < input.len() {
        if input[index] == b'%' {
            if index + 2 >= input.len() {
                return value.to_owned();
            }
            let Some(high) = (input[index + 1] as char).to_digit(16) else {
                return value.to_owned();
            };
            let Some(low) = (input[index + 2] as char).to_digit(16) else {
                return value.to_owned();
            };
            output.push(((high << 4) | low) as u8);
            index += 3;
        } else {
            output.push(input[index]);
            index += 1;
        }
    }
    String::from_utf8(output).unwrap_or_else(|_| value.to_owned())
}

fn scope_value(scopes: &[String], key: &str) -> Option<String> {
    let prefix = format!("onvif://www.onvif.org/{key}/");
    scopes.iter().find_map(|scope| {
        let candidate_prefix = scope.get(..prefix.len())?;
        candidate_prefix
            .eq_ignore_ascii_case(&prefix)
            .then(|| percent_decode(&scope[prefix.len()..]))
    })
}

pub fn parse_probe_matches(
    xml: &str,
    source_ip: Ipv4Addr,
) -> Result<Vec<DiscoveredDevice>, String> {
    let document = roxmltree::Document::parse(xml)
        .map_err(|error| format!("invalid WS-Discovery XML: {error}"))?;
    let mut devices = Vec::new();
    for probe_match in document
        .descendants()
        .filter(|node| node.is_element() && node.tag_name().name() == "ProbeMatch")
    {
        let types = descendant_text(probe_match, "Types").unwrap_or_default();
        let xaddrs: Vec<String> = descendant_text(probe_match, "XAddrs")
            .unwrap_or_default()
            .split_whitespace()
            .map(str::to_owned)
            .collect();
        let scopes: Vec<String> = descendant_text(probe_match, "Scopes")
            .unwrap_or_default()
            .split_whitespace()
            .map(str::to_owned)
            .collect();
        let is_onvif = types.contains("NetworkVideoTransmitter")
            || scopes
                .iter()
                .any(|scope| scope.to_ascii_lowercase().starts_with("onvif://"))
            || xaddrs
                .iter()
                .any(|address| address.to_ascii_lowercase().contains("/onvif/"));
        if !is_onvif {
            continue;
        }
        devices.push(DiscoveredDevice {
            ip: source_ip,
            xaddrs,
            name: scope_value(&scopes, "name"),
            hardware: scope_value(&scopes, "hardware"),
            endpoint_reference: descendant_text(probe_match, "Address").map(str::to_owned),
            scopes,
        });
    }
    Ok(devices)
}

fn message_id() -> String {
    let mut bytes = [0_u8; 16];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    )
}

fn probe_message() -> Vec<u8> {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\
         <e:Envelope xmlns:e=\"http://www.w3.org/2003/05/soap-envelope\"\
         xmlns:w=\"http://schemas.xmlsoap.org/ws/2004/08/addressing\"\
         xmlns:d=\"http://schemas.xmlsoap.org/ws/2005/04/discovery\"\
         xmlns:dn=\"http://www.onvif.org/ver10/network/wsdl\">\
         <e:Header><w:MessageID>uuid:{}</w:MessageID>\
         <w:To e:mustUnderstand=\"true\">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>\
         <w:Action e:mustUnderstand=\"true\">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>\
         </e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>\
         </d:Probe></e:Body></e:Envelope>",
        message_id()
    )
    .into_bytes()
}

fn local_ipv4() -> Vec<Ipv4Addr> {
    let route = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0)).and_then(|socket| {
        socket.connect((MULTICAST_ADDRESS, MULTICAST_PORT))?;
        socket.local_addr()
    });
    match route.map(|address| address.ip()) {
        Ok(IpAddr::V4(address)) if !address.is_loopback() && !address.is_unspecified() => {
            vec![address]
        }
        _ => vec![Ipv4Addr::UNSPECIFIED],
    }
}

fn probe_interface(
    source: Ipv4Addr,
    payload: &[u8],
    deadline: Instant,
) -> Result<Vec<(Vec<u8>, Ipv4Addr)>, String> {
    let socket = UdpSocket::bind((source, 0))
        .map_err(|error| format!("failed to bind discovery interface {source}: {error}"))?;
    for _ in 0..3 {
        socket
            .send_to(payload, (MULTICAST_ADDRESS, MULTICAST_PORT))
            .map_err(|error| format!("failed to send discovery probe on {source}: {error}"))?;
    }

    let mut responses = Vec::new();
    while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
        if remaining.is_zero() {
            break;
        }
        socket
            .set_read_timeout(Some(remaining))
            .map_err(|error| format!("failed to set discovery timeout: {error}"))?;
        let mut buffer = vec![0_u8; 65_535];
        match socket.recv_from(&mut buffer) {
            Ok((length, remote)) => {
                let IpAddr::V4(remote_ip) = remote.ip() else {
                    continue;
                };
                buffer.truncate(length);
                responses.push((buffer, remote_ip));
            }
            Err(error) if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                break;
            }
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(error) => return Err(format!("failed to receive discovery response: {error}")),
        }
    }
    Ok(responses)
}

fn merge_device(target: &mut DiscoveredDevice, incoming: DiscoveredDevice) {
    for xaddr in incoming.xaddrs {
        if !target.xaddrs.contains(&xaddr) {
            target.xaddrs.push(xaddr);
        }
    }
    for scope in incoming.scopes {
        if !target.scopes.contains(&scope) {
            target.scopes.push(scope);
        }
    }
    if target.name.is_none() {
        target.name = incoming.name;
    }
    if target.hardware.is_none() {
        target.hardware = incoming.hardware;
    }
    if target.endpoint_reference.is_none() {
        target.endpoint_reference = incoming.endpoint_reference;
    }
}

fn discover_with_probe<F>(options: &DiscoveryOptions, probe: &F) -> Vec<DiscoveredDevice>
where
    F: Fn(Ipv4Addr, &[u8], Instant) -> Result<Vec<(Vec<u8>, Ipv4Addr)>, String> + Sync,
{
    let mut sources = if options.interfaces.is_empty() {
        local_ipv4()
    } else {
        options.interfaces.clone()
    };
    sources.sort_unstable();
    sources.dedup();
    let payload = probe_message();
    let now = Instant::now();
    let deadline = now.checked_add(options.timeout).unwrap_or(now);
    let responses = thread::scope(|scope| {
        let payload = &payload;
        let handles: Vec<_> = sources
            .into_iter()
            .map(|source| scope.spawn(move || probe(source, payload, deadline)))
            .collect();
        handles
            .into_iter()
            .filter_map(|handle| handle.join().ok()?.ok())
            .flatten()
            .collect::<Vec<_>>()
    });

    let mut found = HashMap::new();
    for (response, source_ip) in responses {
        let xml = String::from_utf8_lossy(&response);
        let Ok(matches) = parse_probe_matches(&xml, source_ip) else {
            continue;
        };
        for incoming in matches {
            match found.get_mut(&incoming.ip) {
                Some(current) => merge_device(current, incoming),
                None => {
                    found.insert(incoming.ip, incoming);
                }
            }
        }
    }
    let mut devices: Vec<_> = found.into_values().collect();
    devices.sort_by_key(|device| device.ip);
    devices
}

pub fn discover_devices(options: &DiscoveryOptions) -> Vec<DiscoveredDevice> {
    discover_with_probe(options, &probe_interface)
}

fn discovery_hosts(cidrs: &[String]) -> Result<Vec<Ipv4Addr>, String> {
    let mut hosts = BTreeSet::new();
    for raw_target in cidrs {
        let target = raw_target.trim();
        let (address, prefix) = match target.split_once('/') {
            Some((address, prefix)) => (address, prefix),
            None => (target, "32"),
        };
        let address = address
            .parse::<Ipv4Addr>()
            .map_err(|_| format!("invalid IPv4 address or CIDR: {raw_target}"))?;
        let prefix = prefix
            .parse::<u8>()
            .ok()
            .filter(|prefix| *prefix <= 32)
            .ok_or_else(|| format!("invalid IPv4 address or CIDR: {raw_target}"))?;
        let size = 1_u64 << (32 - u32::from(prefix));
        let network = (u64::from(u32::from(address)) / size) * size;
        let first = if prefix <= 30 { network + 1 } else { network };
        let last = if prefix <= 30 {
            network + size - 2
        } else {
            network + size - 1
        };
        let host_count = last.saturating_sub(first) + 1;
        if host_count > MAX_CIDR_HOSTS as u64 {
            return Err(format!(
                "CIDR discovery is limited to {MAX_CIDR_HOSTS} IPv4 hosts"
            ));
        }
        for value in first..=last {
            hosts.insert(Ipv4Addr::from(value as u32));
            if hosts.len() > MAX_CIDR_HOSTS {
                return Err(format!(
                    "CIDR discovery is limited to {MAX_CIDR_HOSTS} IPv4 hosts"
                ));
            }
        }
    }
    Ok(hosts.into_iter().collect())
}

fn device_service_url(ip: Ipv4Addr, port: u16) -> String {
    let secure = port == 443;
    let default_port = if secure { 443 } else { 80 };
    let authority = if port == default_port {
        ip.to_string()
    } else {
        format!("{ip}:{port}")
    };
    format!(
        "{}://{authority}/onvif/device_service",
        if secure { "https" } else { "http" }
    )
}

fn probe_cidr_host(ip: Ipv4Addr, ports: &[u16], timeout: Duration) -> Option<DiscoveredDevice> {
    let xaddrs = ports
        .iter()
        .map(|port| device_service_url(ip, *port))
        .filter(|url| probe_device_service(url, timeout).is_ok())
        .collect::<Vec<_>>();
    (!xaddrs.is_empty()).then_some(DiscoveredDevice {
        ip,
        xaddrs,
        scopes: Vec::new(),
        name: None,
        hardware: None,
        endpoint_reference: None,
    })
}

/// Actively probes every unique host represented by `options.cidrs`.
pub fn discover_devices_in_cidrs(
    options: &CidrDiscoveryOptions,
) -> Result<Vec<DiscoveredDevice>, String> {
    if options.timeout.is_zero() {
        return Err("timeout must be greater than 0 for CIDR discovery".to_owned());
    }
    if !(1..=256).contains(&options.concurrency) {
        return Err("concurrency must be between 1 and 256".to_owned());
    }
    if options.ports.is_empty() || options.ports.contains(&0) {
        return Err("ports must contain values between 1 and 65535".to_owned());
    }
    let hosts = discovery_hosts(&options.cidrs)?;
    if hosts.is_empty() {
        return Ok(Vec::new());
    }
    let mut ports = options.ports.clone();
    let mut seen_ports = BTreeSet::new();
    ports.retain(|port| seen_ports.insert(*port));
    let worker_count = options.concurrency.min(hosts.len());
    let chunk_size = hosts.len().div_ceil(worker_count);
    let timeout = options.timeout;
    let devices = thread::scope(|scope| {
        let handles = hosts
            .chunks(chunk_size)
            .map(|chunk| {
                let ports = &ports;
                scope.spawn(move || {
                    chunk
                        .iter()
                        .filter_map(|host| probe_cidr_host(*host, ports, timeout))
                        .collect::<Vec<_>>()
                })
            })
            .collect::<Vec<_>>();
        handles
            .into_iter()
            .filter_map(|handle| handle.join().ok())
            .flatten()
            .collect::<Vec<_>>()
    });
    let mut devices = devices;
    devices.sort_by_key(|device| device.ip);
    Ok(devices)
}

#[cfg(test)]
mod tests {
    use std::io::{ErrorKind, Read, Write};
    use std::net::{Ipv4Addr, TcpListener};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{
        CidrDiscoveryOptions, DiscoveryOptions, discover_devices_in_cidrs, discover_with_probe,
    };

    const FIRST_RESPONSE: &[u8] = br#"<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <a:EndpointReference><a:Address>urn:uuid:camera-1</a:Address></a:EndpointReference>
  <d:Types>dn:NetworkVideoTransmitter</d:Types>
  <d:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/hardware/SM-DM-4M2W</d:Scopes>
  <d:XAddrs>http://10.128.10.141/onvif/device_service http://camera.local/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>"#;

    const SECOND_RESPONSE: &[u8] = br#"<?xml version="1.0"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <e:Body><wsd:ProbeMatches><wsd:ProbeMatch>
  <w:EndpointReference><w:Address>urn:uuid:camera-1</w:Address></w:EndpointReference>
  <wsd:Types>tds:NetworkVideoTransmitter</wsd:Types>
  <wsd:Scopes>onvif://www.onvif.org/name/Front%20Door onvif://www.onvif.org/location/Entrance</wsd:Scopes>
  <wsd:XAddrs>http://10.128.10.141:8000/onvif/device_service</wsd:XAddrs>
 </wsd:ProbeMatch></wsd:ProbeMatches></e:Body>
</e:Envelope>"#;

    #[test]
    fn probes_selected_interfaces_to_one_deadline_and_merges_duplicates() {
        let first_source = Ipv4Addr::new(10, 0, 0, 10);
        let second_source = Ipv4Addr::new(192, 168, 0, 20);
        let options = DiscoveryOptions {
            timeout: Duration::from_secs(3),
            interfaces: vec![first_source, second_source],
        };
        let calls = Mutex::new(Vec::new());

        let devices = discover_with_probe(&options, &|source, payload, deadline| {
            calls.lock().unwrap().push((source, deadline));
            assert!(
                payload
                    .windows("NetworkVideoTransmitter".len())
                    .any(|window| window == b"NetworkVideoTransmitter")
            );
            let response = if source == first_source {
                FIRST_RESPONSE
            } else {
                SECOND_RESPONSE
            };
            Ok(vec![(response.to_vec(), Ipv4Addr::new(10, 128, 10, 141))])
        });

        let calls = calls.into_inner().unwrap();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].1, calls[1].1);
        assert!(calls[0].1 > Instant::now());
        assert_eq!(devices.len(), 1);
        assert_eq!(devices[0].xaddrs.len(), 3);
        assert!(
            devices[0]
                .scopes
                .contains(&"onvif://www.onvif.org/location/Entrance".to_owned())
        );
    }

    #[test]
    fn expands_every_selected_ip_and_cidr_and_removes_overlaps() {
        let hosts = super::discovery_hosts(&[
            "10.0.0.0/30".to_owned(),
            "10.128.0.10".to_owned(),
            "10.0.0.1".to_owned(),
        ])
        .unwrap();

        assert_eq!(
            hosts,
            [
                Ipv4Addr::new(10, 0, 0, 1),
                Ipv4Addr::new(10, 0, 0, 2),
                Ipv4Addr::new(10, 128, 0, 10),
            ]
        );
    }

    #[test]
    fn deduplicates_non_adjacent_ports_before_probing() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let live_port = listener.local_addr().unwrap().port();
        let closed_port = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let requests = Arc::new(Mutex::new(0usize));
        let server_requests = Arc::clone(&requests);
        let server = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_millis(500);
            while Instant::now() < deadline {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let mut request = [0u8; 4096];
                        let _ = stream.read(&mut request).unwrap();
                        *server_requests.lock().unwrap() += 1;
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
                    }
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("test HTTP server failed: {error}"),
                }
            }
        });
        let mut options = CidrDiscoveryOptions::new(["127.0.0.1"]);
        options.ports = vec![live_port, closed_port, live_port];
        options.timeout = Duration::from_millis(100);
        options.concurrency = 1;

        let devices = discover_devices_in_cidrs(&options).unwrap();
        server.join().unwrap();

        assert_eq!(*requests.lock().unwrap(), 1);
        assert_eq!(devices.len(), 1);
        assert_eq!(devices[0].xaddrs.len(), 1);
    }

    #[test]
    fn cidr_discovery_rejects_valid_onvif_xml_from_non_success_http_status() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
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
                "HTTP/1.1 404 Not Found\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });
        let mut options = CidrDiscoveryOptions::new(["127.0.0.1"]);
        options.ports = vec![port];
        options.timeout = Duration::from_secs(1);
        options.concurrency = 1;

        let devices = discover_devices_in_cidrs(&options).unwrap();
        server.join().unwrap();

        assert!(devices.is_empty());
    }
}
