/** Print unapproved execution candidates. Does not write config/execution. */

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { proposeExecutionCandidates } from "../src/execution/candidates.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const document = proposeExecutionCandidates(root);
process.stdout.write(`${JSON.stringify(document, null, 2)}\n`);
