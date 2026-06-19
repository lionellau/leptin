# Security Policy

## Reporting a vulnerability

Please report security issues privately — **do not open a public issue** for a
vulnerability.

- Use GitHub's **"Report a vulnerability"** (Security → Advisories) on the repo, or
- email the maintainer listed on the GitHub profile.

Include: affected version, a description, reproduction steps, and impact. We aim
to acknowledge within 72 hours and to ship a fix or mitigation promptly.

## Scope notes

Leptin is a **local-first** tool:

- The MCP server speaks JSON-RPC over **stdio** — there is no network listener.
- The dashboard binds to **127.0.0.1** only and rejects non-localhost `Host`
  headers (a DNS-rebinding mitigation). It has no authentication and is intended
  for single-user local use — do not expose it to untrusted networks.
- Memory content is treated as data, never executed. Hosted embedding/LLM calls
  (opt-in `[hosted]` extra) send memory text to the configured provider; review
  your provider's data policy before enabling.

## Supported versions

The latest released minor version receives security fixes.
