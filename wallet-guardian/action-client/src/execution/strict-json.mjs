/** JSON parser that rejects floats, duplicate keys, and unsafe integers. */

import { ExecutionRegistryError } from "./errors.mjs";

const MAX_SAFE = 9007199254740991n;

export function parseStrictJson(text) {
  if (typeof text !== "string") throw new ExecutionRegistryError("bad_json", "type");
  if (text.charCodeAt(0) === 0xfeff) throw new ExecutionRegistryError("bad_json", "bom");
  const parser = new Parser(text);
  const value = parser.parseValue();
  parser.skipWs();
  if (!parser.eof()) throw new ExecutionRegistryError("bad_json", "trailing");
  return value;
}

class Parser {
  constructor(text) {
    this.text = text;
    this.i = 0;
    this.n = text.length;
  }

  eof() {
    return this.i >= this.n;
  }

  peek() {
    return this.i >= this.n ? "" : this.text[this.i];
  }

  skipWs() {
    while (this.i < this.n) {
      const char = this.text[this.i];
      if (char === " " || char === "\n" || char === "\r" || char === "\t") this.i += 1;
      else break;
    }
  }

  parseValue() {
    this.skipWs();
    const char = this.peek();
    if (char === "{") return this.parseObject();
    if (char === "[") return this.parseArray();
    if (char === '"') return this.parseString();
    if (char === "t") return this.parseLiteral("true", true);
    if (char === "f") return this.parseLiteral("false", false);
    if (char === "n") return this.parseLiteral("null", null);
    if (this.text.startsWith("-Infinity", this.i)) return this.rejectConstant("-Infinity");
    if (this.text.startsWith("Infinity", this.i)) return this.rejectConstant("Infinity");
    if (this.text.startsWith("NaN", this.i)) return this.rejectConstant("NaN");
    if (char === "-" || (char >= "0" && char <= "9")) return this.parseNumber();
    throw new ExecutionRegistryError("bad_json", "value");
  }

  rejectConstant(word) {
    throw new ExecutionRegistryError("float_rejected", word);
  }

  parseLiteral(word, value) {
    if (this.text.slice(this.i, this.i + word.length) !== word) {
      throw new ExecutionRegistryError("bad_json", word);
    }
    this.i += word.length;
    return value;
  }

  parseObject() {
    this.i += 1;
    const pairs = [];
    this.skipWs();
    if (this.peek() === "}") {
      this.i += 1;
      return Object.create(null);
    }
    while (this.i < this.n) {
      this.skipWs();
      if (this.peek() !== '"') throw new ExecutionRegistryError("bad_json", "key");
      const key = this.parseString();
      this.skipWs();
      if (this.peek() !== ":") throw new ExecutionRegistryError("bad_json", "colon");
      this.i += 1;
      const value = this.parseValue();
      pairs.push([key, value]);
      this.skipWs();
      const char = this.peek();
      if (char === ",") {
        this.i += 1;
        continue;
      }
      if (char === "}") {
        this.i += 1;
        return this.objectFromPairs(pairs);
      }
      throw new ExecutionRegistryError("bad_json", "object");
    }
    throw new ExecutionRegistryError("bad_json", "object");
  }

  objectFromPairs(pairs) {
    const obj = Object.create(null);
    for (const [key, value] of pairs) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) {
        throw new ExecutionRegistryError("duplicate_key", key);
      }
      Object.defineProperty(obj, key, { value, writable: true, enumerable: true, configurable: true });
    }
    return obj;
  }

  parseArray() {
    this.i += 1;
    const items = [];
    this.skipWs();
    if (this.peek() === "]") {
      this.i += 1;
      return items;
    }
    while (this.i < this.n) {
      items.push(this.parseValue());
      this.skipWs();
      const char = this.peek();
      if (char === ",") {
        this.i += 1;
        continue;
      }
      if (char === "]") {
        this.i += 1;
        return items;
      }
      throw new ExecutionRegistryError("bad_json", "array");
    }
    throw new ExecutionRegistryError("bad_json", "array");
  }

  parseString() {
    this.i += 1;
    let out = "";
    while (this.i < this.n) {
      const char = this.text[this.i];
      if (char === '"') {
        this.i += 1;
        return out;
      }
      if (char === "\\") {
        this.i += 1;
        const escaped = this.text[this.i];
        const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
        if (Object.prototype.hasOwnProperty.call(simple, escaped)) {
          out += simple[escaped];
          this.i += 1;
          continue;
        }
        if (escaped === "u") {
          const hex = this.text.slice(this.i + 1, this.i + 5);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw new ExecutionRegistryError("bad_json", "escape");
          out += String.fromCharCode(Number.parseInt(hex, 16));
          this.i += 5;
          continue;
        }
        throw new ExecutionRegistryError("bad_json", "escape");
      }
      if (char.charCodeAt(0) < 32) throw new ExecutionRegistryError("bad_json", "control");
      out += char;
      this.i += 1;
    }
    throw new ExecutionRegistryError("bad_json", "string");
  }

  parseNumber() {
    const start = this.i;
    if (this.peek() === "-") this.i += 1;
    if (this.peek() === "0") {
      this.i += 1;
      const next = this.peek();
      if (next >= "0" && next <= "9") throw new ExecutionRegistryError("bad_json", "leading_zero");
    } else if (this.peek() >= "1" && this.peek() <= "9") {
      while (this.peek() >= "0" && this.peek() <= "9") this.i += 1;
    } else {
      throw new ExecutionRegistryError("bad_json", "number");
    }
    let isFloat = false;
    if (this.peek() === ".") {
      this.i += 1;
      if (this.peek() < "0" || this.peek() > "9") throw new ExecutionRegistryError("bad_json", "number");
      while (this.peek() >= "0" && this.peek() <= "9") this.i += 1;
      isFloat = true;
    }
    const exponent = this.peek();
    if (exponent === "e" || exponent === "E") {
      this.i += 1;
      const sign = this.peek();
      if (sign === "+" || sign === "-") this.i += 1;
      if (this.peek() < "0" || this.peek() > "9") throw new ExecutionRegistryError("bad_json", "number");
      while (this.peek() >= "0" && this.peek() <= "9") this.i += 1;
      isFloat = true;
    }
    const raw = this.text.slice(start, this.i);
    if (isFloat) throw new ExecutionRegistryError("float_rejected", raw.slice(0, 32));
    const numeric = BigInt(raw);
    if (numeric > MAX_SAFE || numeric < -MAX_SAFE) {
      throw new ExecutionRegistryError("integer_out_of_range", raw);
    }
    return Number(raw);
  }
}
