import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { canonicalStringify } from "./canonical.mjs";

const body = {
  b: 1,
  a: { d: "字", c: [2, null, false] },
};
const text = canonicalStringify(body);
assert.equal(text, '{"a":{"c":[2,null,false],"d":"字"},"b":1}');
assert.equal(createHash("sha256").update(text).digest("hex").length, 64);
console.log("canonical ok");
