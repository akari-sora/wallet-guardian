import { LOGIN_STATEMENT } from './login-statement.mjs';

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const HEX_32_BYTES = /^[0-9a-f]{64}$/;
function requireValue(condition) {
  if (!condition) throw new Error('登入挑戰與目前網站、帳戶、鏈或固定讀取聲明不符。');
}

/** Fixed P1 SIWE profile, not a general permission-signing or SIWE parser.
 * The caller supplies browser origin and freshly read wallet identity; none of
 * those trust anchors may be obtained from the challenge response itself.
 */
export function validateLoginChallenge(body, { origin, address, chainId, nowMs = Date.now() }) {
  const site = new URL(origin);
  requireValue(site.protocol === 'https:' && site.origin === origin);
  requireValue(ADDRESS.test(address) && Number.isSafeInteger(chainId) && chainId > 0);
  requireValue(body && typeof body === 'object' && !Array.isArray(body));
  requireValue(body.execution_authorized === false && body.send_authorized === false && body.erc1271 === 'unsupported');
  requireValue(typeof body.message === 'string' && body.message.length <= 4096);
  requireValue(typeof body.csrf_token === 'string' && HEX_32_BYTES.test(body.csrf_token));
  requireValue(ADDRESS.test(body.address) && body.address.toLowerCase() === address.toLowerCase());
  requireValue(body.domain === site.host && (body.uri === origin || body.uri === `${origin}/`));
  requireValue(body.chain_id === chainId && Number.isSafeInteger(body.expires_at));
  const lines = body.message.split('\n');
  requireValue(lines.length === 11 && lines[0] === `${site.host} wants you to sign in with your Ethereum account:`);
  requireValue(lines[1] === body.address && lines[2] === '' && lines[3] === LOGIN_STATEMENT && lines[4] === '');
  requireValue(lines[5] === `URI: ${body.uri}` && lines[6] === 'Version: 1' && lines[7] === `Chain ID: ${chainId}`);
  requireValue(/^Nonce: [a-f0-9]{32}$/.test(lines[8]));
  requireValue(lines[9].startsWith('Issued At: ') && lines[10].startsWith('Expiration Time: '));
  const issuedText = lines[9].slice('Issued At: '.length);
  const expiresText = lines[10].slice('Expiration Time: '.length);
  const issuedAt = Date.parse(issuedText), expiresAt = Date.parse(expiresText);
  requireValue(Number.isFinite(nowMs) && Number.isFinite(issuedAt) && Number.isFinite(expiresAt));
  requireValue(new Date(issuedAt).toISOString() === issuedText && new Date(expiresAt).toISOString() === expiresText);
  requireValue(expiresAt === body.expires_at * 1000 && expiresAt > nowMs);
  requireValue(issuedAt <= nowMs + 60_000 && expiresAt > issuedAt && expiresAt - issuedAt <= 600_000);
  requireValue(expiresAt - nowMs <= 660_000);
  return { message: body.message, csrfToken: body.csrf_token };
}
