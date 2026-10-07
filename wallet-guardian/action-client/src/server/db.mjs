/** Authz, session revoke, and latest-revision reads go to the D1 primary. */
export function primary(env) {
  if (!env || !env.DB || typeof env.DB.withSession !== "function") {
    throw new Error("D1 binding missing");
  }
  return env.DB.withSession("first-primary");
}

export function readConfig(env) {
  const domain = env.AUTH_DOMAIN;
  const uri = env.AUTH_URI;
  const chainId = Number(env.AUTH_CHAIN_ID);
  if (typeof domain !== "string" || typeof uri !== "string" || !Number.isInteger(chainId) || chainId <= 0) {
    throw new Error("auth config missing");
  }
  const parsed = new URL(uri);
  const hostnameMatches = parsed.hostname === domain;
  const pathOk = parsed.pathname === "/";
  const httpsOk = parsed.protocol === "https:" && parsed.port === "" && !parsed.username && !parsed.password;
  if (!hostnameMatches || !pathOk || !httpsOk || parsed.search || parsed.hash) {
    throw new Error("auth audience rejected");
  }
  const challengeLimit = Number(env.AUTH_CHALLENGE_LIMIT ?? 10);
  if (!Number.isInteger(challengeLimit) || challengeLimit < 1 || challengeLimit > 100) {
    throw new Error("auth config missing");
  }
  const challengeGlobalLimit = Number(env.AUTH_CHALLENGE_GLOBAL_LIMIT ?? 200);
  if (!Number.isInteger(challengeGlobalLimit) || challengeGlobalLimit < 1 || challengeGlobalLimit > 10000) {
    throw new Error("auth config missing");
  }
  return {
    domain,
    uri,
    origin: parsed.origin,
    chainId,
    challengeTtl: 600,
    sessionTtl: 12 * 60 * 60,
    challengeLimit,
    challengeGlobalLimit,
    challengeWindow: 600,
  };
}
