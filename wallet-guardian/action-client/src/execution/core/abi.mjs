/**
 * Minimal strict ABI encoder and decoder for the client core.
 * Supported types: address, bool, uint8..uint256, bytes1..bytes32, bytes, string,
 * T[] (dynamic arrays), and tuples. Anything else is refused.
 * Decoding is canonical-only: the decoded values must re-encode to the exact input.
 */
import { refuse } from "./errors.mjs";
import { bytesToHex, hexToBytes, isAddressHex, selectorOfCanonical } from "./hex.mjs";

const WORD = 32;

function splitTopLevel(text) {
  const parts = [];
  let depth = 0;
  let current = "";
  for (const char of text) {
    if (char === "(") depth += 1;
    if (char === ")") depth -= 1;
    if (depth < 0) refuse("bad_signature", text);
    if (char === "," && depth === 0) {
      parts.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  if (depth !== 0) refuse("bad_signature", text);
  if (current.trim() !== "" || parts.length > 0) parts.push(current);
  return parts.map((part) => part.trim());
}

function parseElementary(type) {
  if (type === "address" || type === "bool" || type === "bytes" || type === "string") return { kind: type, canonical: type };
  const uint = /^uint(\d*)$/.exec(type);
  if (uint) {
    const bits = uint[1] === "" ? 256 : Number(uint[1]);
    if (!Number.isInteger(bits) || bits < 8 || bits > 256 || bits % 8 !== 0) refuse("unsupported_type", type);
    return { kind: "uint", bits, canonical: `uint${bits}` };
  }
  const fixed = /^bytes(\d+)$/.exec(type);
  if (fixed) {
    const size = Number(fixed[1]);
    if (!Number.isInteger(size) || size < 1 || size > 32) refuse("unsupported_type", type);
    return { kind: "fixed_bytes", size, canonical: `bytes${size}` };
  }
  refuse("unsupported_type", type);
}

/** Parse one parameter such as `address spender` or `(address a,uint256 b)[] items`. */
function parseParam(text) {
  let rest = text.trim();
  let base;
  if (rest.startsWith("(")) {
    let depth = 0;
    let end = -1;
    for (let index = 0; index < rest.length; index += 1) {
      if (rest[index] === "(") depth += 1;
      if (rest[index] === ")") depth -= 1;
      if (depth === 0) {
        end = index;
        break;
      }
    }
    if (end < 0) refuse("bad_signature", text);
    const components = splitTopLevel(rest.slice(1, end)).map(parseParam);
    base = { kind: "tuple", components, canonical: `(${components.map((item) => item.type.canonical).join(",")})` };
    rest = rest.slice(end + 1);
  } else {
    const match = /^([A-Za-z0-9]+)/.exec(rest);
    if (!match) refuse("bad_signature", text);
    base = parseElementary(match[1]);
    rest = rest.slice(match[1].length);
  }
  let type = base;
  while (rest.startsWith("[")) {
    if (!rest.startsWith("[]")) refuse("unsupported_type", text);
    type = { kind: "array", element: type, canonical: `${type.canonical}[]` };
    rest = rest.slice(2);
  }
  const name = rest.trim();
  if (name !== "" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) refuse("bad_signature", text);
  return { name, type };
}

/**
 * Parse a full signature with parameter names, for example
 * `approve(address spender,uint256 amount)`. Returns the canonical form too.
 */
export function parseSignature(full) {
  if (typeof full !== "string") refuse("bad_signature", "not a string");
  const match = /^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$/s.exec(full.trim());
  if (!match) refuse("bad_signature", full);
  const params = splitTopLevel(match[2]).map(parseParam);
  const canonical = `${match[1]}(${params.map((item) => item.type.canonical).join(",")})`;
  const normalized = `${match[1]}(${params.map(fullParam).join(",")})`;
  return { name: match[1], params, canonical, full: normalized, selector: selectorOfCanonical(canonical) };
}

function fullParam(param) {
  const type = fullType(param.type);
  return param.name ? `${type} ${param.name}` : type;
}

function fullType(type) {
  if (type.kind === "tuple") return `(${type.components.map(fullParam).join(",")})`;
  if (type.kind === "array") return `${fullType(type.element)}[]`;
  return type.canonical;
}

export function isDynamic(type) {
  if (type.kind === "bytes" || type.kind === "string" || type.kind === "array") return true;
  if (type.kind === "tuple") return type.components.some((item) => isDynamic(item.type));
  return false;
}

function headSize(type) {
  if (type.kind === "tuple" && !isDynamic(type)) return type.components.reduce((sum, item) => sum + headSize(item.type), 0);
  return WORD;
}

function toBigInt(value, label) {
  if (typeof value === "bigint") return value;
  if (typeof value === "number" && Number.isSafeInteger(value)) return BigInt(value);
  if (typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value)) return BigInt(value);
  if (typeof value === "string" && /^0x[0-9a-fA-F]+$/.test(value)) return BigInt(value);
  refuse("bad_value", label);
}

function wordOf(value) {
  const hex = value.toString(16);
  if (hex.length > 64) refuse("bad_value", "word overflow");
  return hex.padStart(64, "0");
}

function padRight(hex) {
  const rem = hex.length % 64;
  return rem === 0 ? hex : hex + "0".repeat(64 - rem);
}

function encodeValue(type, value, label) {
  switch (type.kind) {
    case "address":
      if (!isAddressHex(value)) refuse("bad_value", `${label}: address`);
      return value.slice(2).toLowerCase().padStart(64, "0");
    case "bool":
      if (typeof value !== "boolean") refuse("bad_value", `${label}: bool`);
      return wordOf(value ? 1n : 0n);
    case "uint": {
      const number = toBigInt(value, label);
      if (number < 0n || number >= 1n << BigInt(type.bits)) refuse("bad_value", `${label}: uint range`);
      return wordOf(number);
    }
    case "fixed_bytes": {
      const bytes = hexToBytes(value, label);
      if (bytes.length !== type.size) refuse("bad_value", `${label}: bytes${type.size}`);
      return padRight(bytesToHex(bytes).slice(2));
    }
    case "bytes": {
      const bytes = hexToBytes(value, label);
      return wordOf(BigInt(bytes.length)) + padRight(bytesToHex(bytes).slice(2));
    }
    case "string": {
      if (typeof value !== "string") refuse("bad_value", `${label}: string`);
      const bytes = new TextEncoder().encode(value);
      return wordOf(BigInt(bytes.length)) + padRight(bytesToHex(bytes).slice(2));
    }
    case "array": {
      if (!Array.isArray(value)) refuse("bad_value", `${label}: array`);
      const items = value.map(() => ({ name: "", type: type.element }));
      return wordOf(BigInt(value.length)) + encodeParams(items, value, label);
    }
    case "tuple": {
      const values = Array.isArray(value)
        ? value
        : type.components.map((item) => (value && typeof value === "object" ? value[item.name] : undefined));
      return encodeParams(type.components, values, label);
    }
    default:
      refuse("unsupported_type", type.canonical);
  }
}

/** Encode a list of params (head/tail). Returns hex without 0x. */
export function encodeParams(params, values, label = "args") {
  if (!Array.isArray(values) || values.length !== params.length) refuse("bad_value", `${label}: arity`);
  const heads = [];
  const tails = [];
  let tailOffset = params.reduce((sum, item) => sum + headSize(item.type), 0);
  params.forEach((param, index) => {
    const encoded = encodeValue(param.type, values[index], `${label}.${param.name || index}`);
    if (isDynamic(param.type)) {
      heads.push(wordOf(BigInt(tailOffset)));
      tails.push(encoded);
      tailOffset += encoded.length / 2;
    } else {
      heads.push(encoded);
    }
  });
  return heads.join("") + tails.join("");
}

export function encodeCall(signature, values) {
  const parsed = typeof signature === "string" ? parseSignature(signature) : signature;
  return parsed.selector + encodeParams(parsed.params, values, parsed.name);
}

function readWord(bytes, offset) {
  if (offset < 0 || offset + WORD > bytes.length) refuse("bad_calldata", "short word");
  let hex = "0x";
  for (let index = 0; index < WORD; index += 1) hex += bytes[offset + index].toString(16).padStart(2, "0");
  return BigInt(hex);
}

function readLength(bytes, offset) {
  const value = readWord(bytes, offset);
  if (value > BigInt(bytes.length)) refuse("bad_calldata", "length out of range");
  return Number(value);
}

function decodeValue(type, bytes, offset) {
  switch (type.kind) {
    case "address": {
      const word = readWord(bytes, offset);
      if (word >> 160n !== 0n) refuse("bad_calldata", "address padding");
      return "0x" + word.toString(16).padStart(40, "0");
    }
    case "bool": {
      const word = readWord(bytes, offset);
      if (word !== 0n && word !== 1n) refuse("bad_calldata", "bool");
      return word === 1n;
    }
    case "uint": {
      const word = readWord(bytes, offset);
      if (word >= 1n << BigInt(type.bits)) refuse("bad_calldata", "uint range");
      return word;
    }
    case "fixed_bytes":
      readWord(bytes, offset);
      return bytesToHex(bytes.slice(offset, offset + type.size));
    case "bytes":
    case "string": {
      const length = readLength(bytes, offset);
      if (offset + WORD + length > bytes.length) refuse("bad_calldata", "bytes out of range");
      const data = bytes.slice(offset + WORD, offset + WORD + length);
      return type.kind === "bytes" ? bytesToHex(data) : new TextDecoder("utf-8", { fatal: true }).decode(data);
    }
    case "array": {
      const length = readLength(bytes, offset);
      const items = Array.from({ length }, () => ({ name: "", type: type.element }));
      return decodeParamsAt(items, bytes, offset + WORD);
    }
    case "tuple":
      return decodeParamsAt(type.components, bytes, offset);
    default:
      refuse("unsupported_type", type.canonical);
  }
}

function decodeParamsAt(params, bytes, base) {
  const values = [];
  let head = base;
  for (const param of params) {
    if (isDynamic(param.type)) {
      const relative = readWord(bytes, head);
      if (relative > BigInt(bytes.length)) refuse("bad_calldata", "offset out of range");
      values.push(decodeValue(param.type, bytes, base + Number(relative)));
    } else {
      values.push(decodeValue(param.type, bytes, head));
    }
    head += headSize(param.type);
  }
  return values;
}

/**
 * Decode calldata against a parsed signature. The selector must match and the
 * values must re-encode to the exact same bytes (no trailing data, no dirty padding).
 */
export function decodeCall(parsed, data) {
  const bytes = hexToBytes(data, "data");
  if (bytes.length < 4) refuse("bad_calldata", "no selector");
  const selector = bytesToHex(bytes.slice(0, 4));
  if (selector !== parsed.selector) refuse("signature_mismatch", "selector");
  const body = bytes.slice(4);
  const values = decodeParamsAt(parsed.params, body, 0);
  const again = encodeParams(parsed.params, values, parsed.name);
  if (again !== bytesToHex(body).slice(2)) refuse("bad_calldata", "not canonical");
  return values;
}

/** Walk decoded values and yield every leaf with its declared parameter path. */
export function* leaves(params, values, prefix = "") {
  for (let index = 0; index < params.length; index += 1) {
    const param = params[index];
    const path = prefix ? `${prefix}.${param.name || index}` : param.name || String(index);
    yield* leafOf(param, param.type, values[index], path);
  }
}

function* leafOf(param, type, value, path) {
  if (type.kind === "tuple") {
    yield* leaves(type.components, value, path);
    return;
  }
  if (type.kind === "array") {
    for (let index = 0; index < value.length; index += 1) {
      yield* leafOf(param, type.element, value[index], `${path}[${index}]`);
    }
    yield { param, type, value, path, arrayLength: value.length };
    return;
  }
  yield { param, type, value, path };
}
