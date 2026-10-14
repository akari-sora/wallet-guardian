/**
 * Credential and wallet revocation checks.
 * Validators return a result and do not build or run SQL.
 */

const CREDENTIAL_ID = /^[a-f0-9]{16}$/;
const WALLET_ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function validateCredentialId(id) {
  if (typeof id !== "string" || !CREDENTIAL_ID.test(id)) {
    return { ok: false, error: "credential id must be 16 hex characters" };
  }
  return { ok: true, id };
}

export function validateWalletAddress(address) {
  if (typeof address !== "string" || !WALLET_ADDRESS.test(address)) {
    return { ok: false, error: "wallet address must be 0x plus 40 hex characters" };
  }
  return { ok: true, address: address.toLowerCase() };
}

function literal(value) {
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  if (typeof value === "string" && /^0x[0-9a-f]{40}$/.test(value)) return `'${value}'`;
  throw new Error("refusing to interpolate a SQL parameter");
}

/**
 * Adapter for wrangler `d1 execute`: one batch() call is one command string.
 * Placeholders are filled only after the parameter is a safe integer or a lowercase address.
 */
export function sqlBatchDb(execute) {
  return {
    prepare(sql) {
      const statement = {
        sql,
        params: [],
        bind(...params) {
          statement.params = params;
          return statement;
        },
      };
      return statement;
    },
    async batch(statements) {
      const commands = [];
      for (const statement of statements) {
        let index = 0;
        if (typeof statement.sql !== "string" || !statement.sql.startsWith("UPDATE ")) {
          throw new Error("refusing a non-update revocation statement");
        }
        const compiled = statement.sql.replace(/\?/g, () => literal(statement.params[index++]));
        if (compiled.includes("?") || index !== statement.params.length) {
          throw new Error("SQL placeholder mismatch");
        }
        commands.push(compiled);
      }
      execute(commands.join(";\n"));
    },
  };
}

/**
 * Revoke the publish credential, membership, and every session for one address.
 * One db.batch. Invalid addresses throw before any statement is prepared.
 */
export async function revokeWalletAccess(db, address, now) {
  const wallet = validateWalletAddress(address);
  if (!wallet.ok) {
    const error = new Error(wallet.error);
    error.code = "invalid_address";
    throw error;
  }
  if (!Number.isSafeInteger(now) || now < 0) {
    const error = new Error("revocation time must be a non-negative safe integer");
    error.code = "invalid_time";
    throw error;
  }
  await db.batch([
    db.prepare(
      "UPDATE publish_credentials SET revoked_at = ? WHERE lower(wallet_address) = ? AND revoked_at IS NULL",
    ).bind(now, wallet.address),
    db.prepare(
      "UPDATE tenant_members SET revoked_at = ? WHERE lower(wallet_address) = ? AND revoked_at IS NULL",
    ).bind(now, wallet.address),
    db.prepare(
      "UPDATE sessions SET revoked_at = ? WHERE lower(address) = ? AND revoked_at IS NULL",
    ).bind(now, wallet.address),
  ]);
}
