# Security Policy & Responsible Disclosure

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 18.0    | :white_check_mark: |
| 17.0    | :white_check_mark: |

## Security Architecture

The **Odoo MCP Server** is built with enterprise zero-trust principles:
1. **Cryptographic Tokens**: Access tokens are generated with high-entropy URL-safe keys and stored solely as SHA-256 hashes in PostgreSQL.
2. **Strict User Scoping**: All execution occurs strictly inside `request.env(user=token.user_id)`. Native Access Rights (ACLs) and Record Rules (`ir.rule`) apply automatically without `.sudo()` bypasses.
3. **Two-Tier Firewall**:
   - Tier 1: Model Whitelisting, Sensitive Field Blacklisting (`password`, `totp_secret`, `credit_card`), and Token Read-Only toggles.
   - Tier 2: Odoo ORM ACLs and multi-company security.
4. **Rate Limiting Guard**: Configurable per-token sliding-window rate limiting (`rate_limit_per_minute`).
5. **Bulk Operation Protection**: Destructive or modification operations targeting >25 records are blocked by default.

## Reporting a Vulnerability

If you discover a security vulnerability within this project, please send an email to the maintainers or open a private security advisory on GitHub.

Please include:
- A detailed description of the vulnerability.
- Steps or proof-of-concept to reproduce the behavior.
- Affected versions and environments.

We appreciate responsible disclosure and will acknowledge receipt within 48 hours.
