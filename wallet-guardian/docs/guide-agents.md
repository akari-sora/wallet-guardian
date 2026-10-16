# Use Wallet Guardian from an AI agent

This page says how an agent should call the installed `guardian` command. The package in this tree is `wallet-guardian-monitor` `0.4.0rc4`. The skill text is `skills/wallet-guardian/`. Host installs below were not exercised (未實測).

The monitor is watch-only. An agent must not sign, approve, revoke, claim, or broadcast. It must not ask for a seed phrase, a private key, a keystore, or an API key. It must not run `guardian relations`, including inbound, expand, and dry-run. Only the owner may run that command, by hand, in their own terminal.

## Before any other command

Run `guardian capabilities`. It prints the package version, a class for every subcommand, and the exit-code map. It does not open a socket and it does not write a file. If this page and that JSON disagree, trust the command. The catalog is the source.

A subcommand path the catalog does not list is class D. Do not run it. A flag that is named in a variant id and changes the class is class D when the catalog does not list that combination. Do not run it. Ordinary arguments a listed variant needs, such as a file path or `--db`, do not change the class.

Top-level `guardian --help` and `guardian --version`, and `guardian <subcommand> --help`, are class A. They exit while arguments are parsed, before the command runs, and they do not open a database or a socket. `guardian --version` prints only the version and exits 0.

An owner's agreement covers the one run the owner answered; it is not standing consent.

- `A_read_only_offline`: no network and no local writes. Commands that open a database are not in this class.
- `B_network_requires_owner_ok`: contacts a configured host. Run it only after the owner agrees to that read. Some of these commands also write local files.
- `C_writes_local_state_owner_decides`: writes local files or rows, or prints an install plan. The owner decides. Most view commands are in this class because opening the monitor database can create it or apply additive migrations. Run those only after the owner agrees to that run.
- `D_never_from_agent`: do not run these. This includes every form of `relations`, and `install-service` with apply, including apply together with dry-run.

A gap is not safe, not zero, and not complete. Exit code 3 is not success. An empty outbox is not all clear. Unknown prices stay null. Do not enable GoPlus or Chainabuse. Do not claim that the 72-hour shadow validation is finished, that CI passed, or that this package was published on PyPI. Call only flags that the installed `--help` lists. There is no MCP server in this tree.

The longer limits are in `wallet-guardian/AGENTS.md` and in `skills/wallet-guardian/references/safety.md`.

## Install the command

Install only a wheel file whose SHA256 you have checked. The release page named in the project README is `https://gitlab.com/aa22396584/wallet-guardian/-/releases/v0.4.0-rc.4`. Download `SHA256SUMS` and every file it lists, including `constraints.txt`, into one directory, and check the hashes before install. The steps are in [install.md](install.md). Do not install `guardian` or `wallet-guardian-monitor` by name from any index (pip, uv, or pipx). A package in an index with one of those names is not this project.

## Install the skill

The installer reads the skill from a checkout of this repository. Use a checkout of the same tag as the installed wheel when you can. From the `wallet-guardian/` directory, run `python3 tools/install_agent_skills.py`. Do not use `uv run`: that may sync dependencies and open a network connection.

The installer defaults to a dry-run. `--apply` writes. It never overwrites an existing copy. It reads `HOME` from the environment, or an absolute `--home` placed before the subcommand. It does not expand a tilde.

```bash
python3 tools/install_agent_skills.py install --agent claude
python3 tools/install_agent_skills.py install --agent claude --apply
python3 tools/install_agent_skills.py doctor
```

| `--agent` | Destination under `HOME` | Hosts that read it |
| --- | --- | --- |
| `claude` | `.claude/skills/wallet-guardian` | Claude Code. Grok Build also reads this directory. |
| `agents` | `.agents/skills/wallet-guardian` | Codex and Cursor. |
| `hermes` | `.hermes/skills/crypto/wallet-guardian` | Hermes. Pass `--category` to use another single path segment. The default category is `crypto`. |

Do not add a separate `grok` destination. That would copy the same skill twice. There is no `grok` choice.

`doctor` lists copies under that `HOME` and exits 1 when a copy does not match its SHA-256 marker or the source tree. A marker file named `.wallet-guardian-skill-sha256.json` is written next to an applied copy.

## Grok Bot

Grok Bot uploads one `SKILL.md`. The flattened file is `skills/dist/grok-bot/SKILL.md`. It inlines the command catalog and the safety page, so it has no relative links. Add it with the host's skill command, or paste the file. The cloud computer for that account must already have `guardian` installed. Bots on the same account share that computer and its data. This host path was not exercised (未實測).

The older paste block is still in [guide-grok-bot.md](guide-grok-bot.md). Prefer the skill file when the host accepts one.

## What was not exercised

Claude Code, Codex, Cursor, Grok Build, Hermes, and Grok Bot were not run against this skill in this tree (未實測). The checks here are the CLI, the skill files, and the installer pointed at a temporary `HOME`.
