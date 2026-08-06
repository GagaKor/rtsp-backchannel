import { SaxesParser } from 'saxes';

const MAX_XML_BYTES = 1024 * 1024;
const FORBIDDEN_DECLARATION_NAMES = ['DOCTYPE', 'ENTITY'] as const;

function isXmlWhitespace(character: string | undefined): boolean {
  return character === ' ' || character === '\t' || character === '\n' || character === '\r';
}

function isAsciiWordCharacter(character: string | undefined): boolean {
  if (character === undefined) return false;
  const code = character.charCodeAt(0);
  return (
    (code >= 48 && code <= 57)
    || (code >= 65 && code <= 90)
    || code === 95
    || (code >= 97 && code <= 122)
  );
}

function containsForbiddenDeclaration(xml: string): boolean {
  let state: 'document' | 'processing-instruction' | 'comment' | 'cdata' = 'document';
  let cursor = 0;
  while (cursor < xml.length) {
    if (state === 'processing-instruction') {
      if (xml.startsWith('?>', cursor)) {
        state = 'document';
        cursor += 2;
      } else {
        cursor++;
      }
      continue;
    }

    if (state === 'comment') {
      if (xml.startsWith('-->', cursor)) {
        state = 'document';
        cursor += 3;
      } else {
        cursor++;
      }
      continue;
    }

    if (state === 'cdata') {
      if (xml.startsWith(']]>', cursor)) {
        state = 'document';
        cursor += 3;
      } else {
        cursor++;
      }
      continue;
    }

    if (xml.startsWith('<?', cursor)) {
      state = 'processing-instruction';
      cursor += 2;
      continue;
    }
    if (xml.startsWith('<!--', cursor)) {
      state = 'comment';
      cursor += 4;
      continue;
    }
    if (xml.startsWith('<![CDATA[', cursor)) {
      state = 'cdata';
      cursor += 9;
      continue;
    }
    if (xml.startsWith('<!', cursor)) {
      let nameStart = cursor + 2;
      while (isXmlWhitespace(xml[nameStart])) nameStart++;
      for (const name of FORBIDDEN_DECLARATION_NAMES) {
        const candidate = xml.slice(nameStart, nameStart + name.length);
        if (
          candidate.toUpperCase() === name
          && !isAsciiWordCharacter(xml[nameStart + name.length])
        ) {
          return true;
        }
      }
    }
    cursor++;
  }
  return false;
}

/** @internal */
export interface XmlAttribute {
  readonly uri: string;
  readonly local: string;
  readonly value: string;
}

/** @internal */
export interface XmlElement {
  readonly uri: string;
  readonly local: string;
  readonly attributes: readonly XmlAttribute[];
  readonly children: readonly XmlElement[];
  readonly text: string;
}

interface MutableXmlElement {
  uri: string;
  local: string;
  attributes: XmlAttribute[];
  children: XmlElement[];
  text: string;
}

/** @internal */
export function parseXml(xml: string): XmlElement {
  if (Buffer.byteLength(xml, 'utf8') > MAX_XML_BYTES) {
    throw new RangeError(`XML input exceeds ${MAX_XML_BYTES} bytes`);
  }
  if (containsForbiddenDeclaration(xml)) {
    throw new Error('DTD and entity declarations are not allowed');
  }

  const parser = new SaxesParser({ xmlns: true });
  const stack: MutableXmlElement[] = [];
  let root: XmlElement | undefined;

  parser.on('opentag', (tag) => {
    const attributes = Object.values(tag.attributes).map((entry) =>
      Object.freeze({ uri: entry.uri, local: entry.local, value: entry.value })
    );
    stack.push({
      uri: tag.uri,
      local: tag.local,
      attributes,
      children: [],
      text: '',
    });
  });
  const appendText = (value: string) => {
    const current = stack.at(-1);
    if (current) current.text += value;
  };
  parser.on('text', appendText);
  parser.on('cdata', appendText);
  parser.on('closetag', () => {
    const completed = stack.pop();
    if (!completed) throw new Error('invalid XML document');
    const element: XmlElement = Object.freeze({
      uri: completed.uri,
      local: completed.local,
      attributes: Object.freeze(completed.attributes),
      children: Object.freeze(completed.children),
      text: completed.text,
    });
    const parent = stack.at(-1);
    if (parent) parent.children.push(element);
    else root = element;
  });

  try {
    parser.write(xml).close();
  } catch {
    throw new Error('invalid XML document');
  }
  if (!root || stack.length !== 0) throw new Error('invalid XML document');
  return root;
}

/** @internal */
export function childElements(
  node: XmlElement,
  uri: string,
  local: string,
): XmlElement[] {
  return node.children.filter((child) => child.uri === uri && child.local === local);
}

/** @internal */
export function firstChild(
  node: XmlElement,
  uri: string,
  local: string,
): XmlElement | undefined {
  return node.children.find((child) => child.uri === uri && child.local === local);
}

/** @internal */
export function requireDescendant(
  node: XmlElement,
  uri: string,
  local: string,
): XmlElement {
  const pending = [...node.children].reverse();
  while (pending.length > 0) {
    const candidate = pending.pop()!;
    if (candidate.uri === uri && candidate.local === local) return candidate;
    for (let index = candidate.children.length - 1; index >= 0; index--) {
      pending.push(candidate.children[index]);
    }
  }
  throw new Error(`required XML element not found: {${uri}}${local}`);
}

/** @internal */
export function attribute(
  node: XmlElement,
  uri: string,
  local: string,
): string | undefined {
  return node.attributes.find((entry) => entry.uri === uri && entry.local === local)?.value;
}

/** @internal */
export function textOf(node: XmlElement | undefined): string | undefined {
  return node?.text.trim();
}
