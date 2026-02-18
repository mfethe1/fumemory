# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly:

**Email:** security@protelynx.ai

Do **not** open a public issue for security vulnerabilities.

## What We Check on PRs

All pull requests are reviewed for:

1. **No hardcoded secrets** — API keys, tokens, passwords must use environment variables
2. **No SQL injection** — all queries must use parameterized inputs
3. **No PII leaks** — no personal data in code, comments, or test fixtures
4. **Input validation** — all user-facing endpoints validate and sanitize inputs
5. **Dependency safety** — new dependencies are checked for known vulnerabilities

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ Active |

## Best Practices for Contributors

- Use `.env` files (never commit `.env`, only `.env.example`)
- Run the security checklist in the PR template
- Test with the default `memu-dev-key`, never with production keys
