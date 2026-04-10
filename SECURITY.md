# IMAP Proxy Security Notice

## Auth Bypass (Critical)
This IMAP proxy **ignores the LOGIN password** and authenticates to Microsoft using OAuth2 client_credentials. As a result, **anyone who can reach port 993** can read **any configured mailbox** by logging in with a known email address. This is a live, exploitable auth bypass.

## Required Redesign (Post-Launch)
The proxy must be redesigned to validate a **per-mailbox app password** issued by our app **before** opening the upstream OAuth bridge. High-level requirements:
- Our app issues a unique, revocable app password per mailbox.
- The proxy validates that password server-side for the mailbox being accessed.
- Only after validation does the proxy acquire an OAuth token and open the upstream IMAP connection.

Do **not** re-enable this proxy until the redesign above is complete.

## Tactical Pre-Launch Block (Immediate)
Until the redesign is complete, you must **block external access** and **stop the systemd service** on the droplet. See `EMERGENCY_BLOCK.md` for copy-paste commands.
