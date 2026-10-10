# Host network and egress

The monitor is read-only. This note is about how it resolves names and where a
connection is allowed to go. It applies to MCM and to the legacy read clients.

## Three separate facts

On the reviewed Linux host these three facts are not the same thing:

1. DNS-over-HTTPS returns public addresses. `https://cloudflare-dns.com/dns-query` and `https://dns.google/resolve` answer with real public A/AAAA records. The system resolver does not: it returns `198.18.0.1` for every name, and UDP/53 to public resolvers is intercepted.
2. TLS still checks the original hostname. A connection presents the Server Name Indication of the host you asked for, and certificate verification stays end to end.
3. The pinned destination address is not honored. Egress is an SNI-routed transparent proxy. Connecting to address A with the name of host B returns B's certificate. The destination address is ignored.

DoH being true does not make fact 3 true. Hostname verification being true does not make fact 3 true.

## Verdicts

`guardian egress-doctor` prints one JSON report. `qualification` is `formal` or `preview`.

| Situation | Formal | Preview |
| --- | --- | --- |
| Probe shows the network delivered the connection to the pinned address | `OK` | `OK` |
| Probe shows host B's certificate when the socket was aimed at host A's address | `BLOCKED_EGRESS` | `OK` |
| Destination enforcement was not proven | `BLOCKED_EGRESS` | `OK` |
| The resolver returned a benchmark, private, or otherwise disallowed address | `BLOCKED_EGRESS` (refused) | `BLOCKED_EGRESS` (refused) |

`BLOCKED_EGRESS` means this host cannot be used for formal qualification. It does not mean the product is blocked on an ordinary Linux host whose network delivers packets to the address you connected to, and it does not mean a preview run has to stop. Preview `OK` is not proof.

A failed lookup is not safe, not complete, and not zero.

## How to read the report

These four fields are separate. Do not collapse them into one "network is fine" bit.

| Field | Meaning |
| --- | --- |
| `resolver_verified` | True only after DNS-over-HTTPS answered from an approved resolver whose hostname was pinned to a configured bootstrap address and whose certificate was checked. System DNS does not set this. |
| `tls_hostname_verified` | True when the handshake or the direct client checked the certificate for the original hostname. |
| `destination_policy_enforced` | True only when a probe or a proven last hop showed the network used the pinned address. |
| `enforcement_location` | `unproven`, `sni_proxy`, `destination_ip`, `last_hop`, or `platform`. |

`enforcement_location=sni_proxy` with `destination_policy_enforced=false` means fact 3 failed: the proxy routed by name.

`trust_env=false` on the HTTP client is not a fifth fact and must not set `destination_policy_enforced`. Turning off ambient proxy use only means this process did not hand the connection to `HTTP_PROXY`. It says nothing about the last hop.

The same four fields are on a `run-once` report under `egress` and under `budget.egress` (the provenance ledger). Capability rows are unchanged. A row is not marked safe because the client pinned an address.

`allow_private_network` in the doctor report is always false. A benchmark answer does not turn that flag on.

Other fields:

- `status` is `ok` when the diagnosis finished, and `refused` when resolution or the direct transport rejected the attempt.
- `reason` is the refusal code, such as `benchmark_address`, `private_endpoint`, `ambient_proxy`, `doh_ttl`, `doh_size`, or `doh_redirect_unapproved`.
- `verdict` is `OK` or `BLOCKED_EGRESS`.
- `queries` lists the DNS-over-HTTPS requests. Each one has only the hostname and the type (`A` or `AAAA`).
- `redirect_guard` records that an HTTP redirect to another host was not fetched.

Example, with public resolver names and no wallet data:

```json
{
  "allow_private_network": false,
  "destination_policy_enforced": false,
  "enforcement_location": "sni_proxy",
  "qualification": "formal",
  "resolver_mode": "doh",
  "resolver_verified": true,
  "status": "ok",
  "tls_hostname_verified": true,
  "verdict": "BLOCKED_EGRESS"
}
```

The same host with `--qualification preview` keeps `destination_policy_enforced` false and sets `verdict` to `OK`.

## Resolver modes

The default mode is `system`. Existing configs do not change. If the system resolver returns an address in `198.18.0.0/15`, the lookup is refused. The error tells the operator to switch the mode to `doh`. The address is not allowlisted for any hostname, including public APIs.

`doh` uses only configured HTTPS resolvers:

- The resolver hostname is connected to a bootstrap address from the operator config, not from the system resolver.
- TLS verifies that resolver hostname.
- The client sends a DNS-JSON query (`Accept: application/dns-json`) with the hostname and type `A` or `AAAA`. It does not send a wallet address, an RPC URL, or an API key.
- Answers must be NOERROR, not truncated, within the size limit, and carry an integer TTL from 0 through 7 days.
- CNAME chains are followed only inside that answer. Loops and incomplete chains fail.
- Private, loopback, link-local, metadata, reserved, CGNAT (`100.64.0.0/10`), and benchmark (`198.18.0.0/15`, plus the IPv6 benchmark range) answers are rejected. One bad address rejects the name.
- Redirects are not followed. A timeout or a 5xx may retry the same approved resolver and then the next configured one. Nothing in the response can add a resolver.

Public monitor files only name environment variables. They do not contain the endpoint values:

```json
"resolver": {
  "mode_env": "GUARDIAN_RESOLVER_MODE",
  "endpoints_env": "GUARDIAN_DOH_ENDPOINTS",
  "bootstrap_env": "GUARDIAN_DOH_BOOTSTRAP"
}
```

`GUARDIAN_RESOLVER_MODE` is `system` or `doh`.

`GUARDIAN_DOH_ENDPOINTS` is a comma-separated list of HTTPS URLs, for example `https://cloudflare-dns.com/dns-query,https://dns.google/resolve`.

`GUARDIAN_DOH_BOOTSTRAP` pins those hosts, for example `cloudflare-dns.com=1.1.1.1,1.0.0.1;dns.google=8.8.8.8,8.8.4.4`.

An operator file passed as `--egress-config` may contain the same public URLs and bootstrap addresses directly. It is not a wallet file. Bootstrap addresses must themselves be public unicast. `198.18.0.1` is not a bootstrap address.

```text
guardian egress-doctor --qualification formal --egress-config PATH
uv run python tools/egress_doctor.py --qualification preview --egress-config PATH
```

The egress doctor does not open the accounts file or the secret profile.

`guardian doctor` (no `--live`) is a separate offline report. It prints `resolver.requested_mode` and `resolver.effective_mode`, and DoH endpoint hostnames only. `requested_mode` is `system`, `doh`, or `invalid`. An unusable setting is `invalid` and the text is not printed. `connectivity.probed` stays false unless you pass `--live`. `egress_qualification.qualified` stays false. DoH enabled is not a qualified path. `GUARDIAN_RESOLVER_MODE` is `system` or `doh`. `GUARDIAN_DOH_ENDPOINTS` and `GUARDIAN_DOH_BOOTSTRAP` are the endpoint list and the pins, as above. `--live` passes that resolver into its probe calls and stops each probe at a 5 second whole-call deadline.

## What formal qualification requires

Either of these, and not a client-side flag:

1. Platform-approved destination passthrough, or another controlled egress, proven by reading the platform config back and by the doctor's cross-connect test (`destination_policy_enforced=true`, `enforcement_location=destination_ip`).
2. An operator-controlled egress that enforces the destination at the last hop, reached over an authenticated channel. Task data must not be able to choose an arbitrary URL or Server Name Indication. Record that as `enforcement_location=last_hop` or `platform` only when that proof exists.

Until one of those is true, formal qualification stays `BLOCKED_EGRESS`.

## Do not

- Do not allowlist `198.18.0.1` or the rest of `198.18.0.0/15`.
- Do not set `allow_private_network` to get past a benchmark or private answer.
- Do not disable TLS hostname verification.
- Do not route through another proxy to bypass this policy. `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` are refused unless the operator sets `GUARDIAN_EGRESS_AMBIENT_PROXY=direct`, which means "ignore those variables and connect directly," not "use them."
- Do not treat SNI-only routing as destination enforcement.
- Do not treat `trust_env=false` as last-hop evidence.
- Do not send wallet addresses or RPC keys to a DNS resolver.
- Do not record a failed read as zero, safe, or complete.
