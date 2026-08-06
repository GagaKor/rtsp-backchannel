import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  attribute,
  childElements,
  firstChild,
  parseXml,
  requireDescendant,
  textOf,
  type XmlElement,
} from './xml.ts';

const SOAP_NS = 'http://www.w3.org/2003/05/soap-envelope';
const DEV_NS = 'http://www.onvif.org/ver10/device/wsdl';
const WSTOP_NS = 'http://docs.oasis-open.org/wsn/t-1';
const TOPIC_NS = 'http://www.onvif.org/ver10/topics';
const MAX_XML_BYTES = 1024 * 1024;

function xmlWithUtf8ByteLength(totalBytes: number): string {
  const openingTag = '<root>';
  const closingTag = '</root>';
  const contentBytes = totalBytes - Buffer.byteLength(openingTag + closingTag, 'utf8');
  const multibyteCharacter = '한';
  const multibyteCharacterBytes = Buffer.byteLength(multibyteCharacter, 'utf8');

  return (
    openingTag
    + multibyteCharacter.repeat(Math.floor(contentBytes / multibyteCharacterBytes))
    + 'x'.repeat(contentBytes % multibyteCharacterBytes)
    + closingTag
  );
}

test('finds namespace-qualified elements when prefixes change and preserves repeated siblings', () => {
  const root = parseXml(`
    <soap:Envelope xmlns:soap="${SOAP_NS}" xmlns:tds="${DEV_NS}">
      <soap:Body>
        <tds:GetServicesResponse>
          <tds:Service>
            <tds:Namespace>Media &amp; Camera &#x41;</tds:Namespace>
          </tds:Service>
          <device:Service xmlns:device="${DEV_NS}">
            <device:Namespace>Events</device:Namespace>
          </device:Service>
        </tds:GetServicesResponse>
      </soap:Body>
    </soap:Envelope>
  `);

  assert.deepEqual({ uri: root.uri, local: root.local }, {
    uri: SOAP_NS,
    local: 'Envelope',
  });
  const response = requireDescendant(root, DEV_NS, 'GetServicesResponse');
  const services = childElements(response, DEV_NS, 'Service');
  assert.equal(services.length, 2);
  assert.equal(textOf(firstChild(services[0], DEV_NS, 'Namespace')), 'Media & Camera A');
  assert.equal(textOf(firstChild(services[1], DEV_NS, 'Namespace')), 'Events');
  assert.equal(firstChild(response, DEV_NS, 'Missing'), undefined);
});

test('reads namespaced attributes without confusing an unqualified attribute', () => {
  const root = parseXml(`
    <wstop:TopicSet xmlns:wstop="${WSTOP_NS}" xmlns:tns="${TOPIC_NS}">
      <tns:Motion topic="vendor" wstop:topic="true" />
    </wstop:TopicSet>
  `);
  const motion = firstChild(root, TOPIC_NS, 'Motion');
  assert.ok(motion);

  assert.equal(attribute(motion, WSTOP_NS, 'topic'), 'true');
  assert.equal(attribute(motion, '', 'topic'), 'vendor');
  assert.equal(attribute(motion, DEV_NS, 'topic'), undefined);
});

test('returns a deeply immutable XML tree', () => {
  const root = parseXml('<root key="value"><child>text</child></root>');
  const child = firstChild(root, '', 'child');
  assert.ok(child);

  assert.equal(Object.isFrozen(root), true);
  assert.equal(Object.isFrozen(root.attributes), true);
  assert.equal(Object.isFrozen(root.children), true);
  assert.equal(Object.isFrozen(child), true);
  assert.equal(Object.isFrozen(root.attributes[0]), true);
  assert.throws(() => (root.children as XmlElement[]).push(root), TypeError);
});

test('reports malformed XML with an operation-neutral error', () => {
  assert.throws(
    () => parseXml('<root><child></root>'),
    { name: 'Error', message: 'invalid XML document' },
  );
});

test('allows declaration-like text inside an XML comment', () => {
  const root = parseXml('<root><!-- <!DOCTYPE harmless> --><child>ok</child></root>');

  assert.equal(textOf(firstChild(root, '', 'child')), 'ok');
});

test('allows declaration-like text inside CDATA', () => {
  const root = parseXml('<root><![CDATA[<!ENTITY harmless>]]></root>');

  assert.equal(textOf(root), '<!ENTITY harmless>');
});

test('allows declaration-like text inside a processing instruction', () => {
  const root = parseXml('<root><?note <!DOCTYPE harmless>?></root>');

  assert.equal(root.local, 'root');
});

test('does not let a fake comment marker in a processing instruction hide a real DOCTYPE', () => {
  assert.throws(
    () => parseXml(
      '<?note <!-- fake?><!DOCTYPE camera SYSTEM "https://example.invalid/camera.dtd">'
      + '<!-- --><camera/>',
    ),
    { name: 'Error', message: 'DTD and entity declarations are not allowed' },
  );
});

test('does not let a fake CDATA marker in a processing instruction hide a real DOCTYPE', () => {
  assert.throws(
    () => parseXml(
      '<?note <![CDATA[ fake?><!DOCTYPE camera SYSTEM "https://example.invalid/camera.dtd">'
      + '<camera><![CDATA[safe]]></camera>',
    ),
    { name: 'Error', message: 'DTD and entity declarations are not allowed' },
  );
});

test('rejects DOCTYPE and ENTITY declarations before parsing', () => {
  const forbidden = [
    '<!DOCTYPE camera SYSTEM "https://example.invalid/camera.dtd"><camera/>',
    '<!ENTITY secret SYSTEM "file:///etc/passwd"><camera/>',
  ];

  for (const xml of forbidden) {
    assert.throws(
      () => parseXml(xml),
      { name: 'Error', message: 'DTD and entity declarations are not allowed' },
    );
  }
});

test('rejects an entity declaration in a DOCTYPE internal subset', () => {
  assert.throws(
    () => parseXml('<!DOCTYPE camera [<!ENTITY secret "classified">]><camera>&secret;</camera>'),
    { name: 'Error', message: 'DTD and entity declarations are not allowed' },
  );
});

test('applies the one MiB input limit to UTF-8 bytes', () => {
  const atLimit = xmlWithUtf8ByteLength(MAX_XML_BYTES);
  const aboveLimit = xmlWithUtf8ByteLength(MAX_XML_BYTES + 1);

  assert.equal(Buffer.byteLength(atLimit, 'utf8'), MAX_XML_BYTES);
  assert.equal(parseXml(atLimit).local, 'root');
  assert.equal(Buffer.byteLength(aboveLimit, 'utf8'), MAX_XML_BYTES + 1);
  assert.throws(
    () => parseXml(aboveLimit),
    { name: 'RangeError', message: 'XML input exceeds 1048576 bytes' },
  );
});

test('rejects XML input larger than one MiB', () => {
  assert.throws(
    () => parseXml(`<root>${'x'.repeat(1024 * 1024)}</root>`),
    { name: 'RangeError', message: 'XML input exceeds 1048576 bytes' },
  );
});
