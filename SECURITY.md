# Security Policy

## Reporting a vulnerability

Email **security@benthic.io** with a description, the affected version, and reproduction steps if you
have them. Please do not open a public issue for anything exploitable.

Include the commit or release you tested. The project is pre-1.0, so there is no long-term support
window to work within; a fix lands on `main` as a normal commit.

## What this server is

A read-only MCP server over signed [Benthic Data Provenance](https://benthic.io/bdp/) datasets. It
executes no model of its own, stores no query results, and cannot write to any dataset. Its authority
comes entirely from the signed manifest, so a vulnerability is most likely to be one of:

- accepting a relation, column, join path, or RPC endpoint that the signed manifest does not grant
- failing to verify a manifest signature, or accepting a manifest from an untrusted key
- letting a caller reach the BDP backend by a path that bypasses the catalog
- an unauthenticated or unbound request reaching the service

Each of those has an invariant test in `tests/`, so a regression should be caught before it ships.

## Deployment notes

The service is designed for a trusted LAN, not for the public internet. It is authenticated with a
bearer token, but the token is the only thing standing between a caller and the datasets, so:

- Set `BENTHIC_MCP_BEARER_TOKEN` to at least 32 random characters. Generate it, do not type it:
  `openssl rand -base64 36`. Keep the env file mode at `600`.
- Set `BENTHIC_MCP_ALLOWED_HOSTS` and `BENTHIC_MCP_ALLOWED_ORIGINS` to the hosts you actually serve.
  The shipped default is localhost only, on purpose.
- Terminate TLS in front of it if it leaves the machine. The bearer token is sent in a header, so
  plaintext HTTP exposes it to anyone on the path.
- Do not run it as root. The bundled `examples/benthic-mcp.service` sets `NoNewPrivileges`,
  `PrivateTmp` and `ProtectSystem` for that reason.

## Trust boundary

Model-authored content can never widen what the server will do. Lessons reported through
`benthic_report` are text shown to the calling model, not capabilities: the RPC allowlist is
hand-written in `src/benthic_mcp/catalog.py`, and every relation, column and join path is resolved
from the signed manifest. If you find a way for reported text to change the set of permitted
operations, that is a security bug regardless of how well-behaved the text looked.
