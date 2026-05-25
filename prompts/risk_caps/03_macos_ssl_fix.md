# 03 — macOS Python 3.14 SSL Trust Store Fix

## Task

Install Python's bundled CA bundle into the user's Framework Python 3.14 install so the runner's WebSocket can establish TLS connections to Kalshi without `CERTIFICATE_VERIFY_FAILED`.

## Context

A `--run --dry-run` launch showed repeated `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate` errors when the WS client tried to connect to `wss://api.elections.kalshi.com/trade-api/ws/v2`. Diagnosis:

```
ssl.get_default_verify_paths().cafile = None
openssl_cafile = /Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/cert.pem  (does not exist)
```

This is the canonical macOS Python install issue: the Framework Python installer doesn't auto-link the system root certificates. Python ships an installer script that fixes it.

The runner falls back to REST polling without WebSocket, so dry-run still "works" — but live mode would miss real-time fill events, which materially degrades risk control (we'd only learn about fills at :00 and :30 REST polls). Fix is non-optional before live deployment.

## Required Reading

- The diagnostic output cited above.
- The installer script itself (read-only): `/Applications/Python 3.14/Install Certificates.command`.

## Scope Boundaries

May modify:

- The user's local Python 3.14 Framework install (system action, no repo change).

Off-limits:

- Any repo file. This task produces no commit.
- The `.venv/` — the venv inherits the Framework Python's default cert path, so fixing the base install fixes the venv automatically.

## Dependencies

None.

## Implementation Notes

1. Run the bundled installer:

   ```bash
   /Applications/Python\ 3.14/Install\ Certificates.command
   ```

   The installer:
   - Upgrades `certifi` in the Framework Python's site-packages.
   - Symlinks `certifi.where()` into `/Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/cert.pem`.

2. Verify with the venv's Python (it inherits Framework cert paths):

   ```bash
   uv run python -c "import ssl; p = ssl.get_default_verify_paths(); print('cafile:', p.cafile)"
   ```

   Expected: `cafile: /Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/cert.pem` (a real, readable path).

3. Smoke-test the actual WS endpoint:

   ```bash
   uv run python -c "import ssl, socket; ctx = ssl.create_default_context(); s = ctx.wrap_socket(socket.create_connection(('api.elections.kalshi.com', 443)), server_hostname='api.elections.kalshi.com'); print('TLS OK, peer cert subject:', s.getpeercert()['subject']); s.close()"
   ```

   Expected: `TLS OK, peer cert subject: ...`. Any TLS error means the fix didn't apply.

4. Final integration check: relaunch `uv run python -m src.main --run --dry-run` and confirm the log no longer contains `CERTIFICATE_VERIFY_FAILED` lines, and the line `WebSocket: connecting to wss://...` is followed by a connected state (rather than reconnect backoff).

## Acceptance Criteria

- [ ] `ssl.get_default_verify_paths().cafile` resolves to an existing file.
- [ ] TLS smoke test against `api.elections.kalshi.com:443` succeeds.
- [ ] `--run --dry-run` log shows no `CERTIFICATE_VERIFY_FAILED` lines.

## Documentation Requirement

- Task 04 CHANGELOG mentions the host-side fix and that no repo files changed.

## Commit Requirement

No commit — this is a local environment change. Note in CHANGELOG that the operator ran the installer on the deployment host.
