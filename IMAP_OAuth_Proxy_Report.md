# IMAP OAuth Proxy — Full Report & Learnings

**Date:** March 20-21, 2026
**Project:** Simple Inboxes — Microsoft IMAP Bulk Upload Solution
**Server:** 146.190.174.23 (domain-cleanup-imap-proxy on DigitalOcean)
**Domain:** imap.simpleinboxes.com
**SMTP CNAME:** smtp.simpleinboxes.com → smtp.office365.com

---

## 1. The Problem

Microsoft killed Basic Auth for IMAP in October 2022. SMTP still works with username + password, but IMAP requires OAuth 2.0.

When sending tools like Smartlead, Instantly, and Email Bison connect inboxes via bulk upload (CSV with SMTP host, IMAP host, username, password), SMTP connects fine but IMAP gets rejected by Microsoft because it sends a password instead of an OAuth token.

The only alternative was connecting inboxes one-by-one through each tool's OAuth flow — clicking "Connect via Microsoft" per inbox. This doesn't scale when provisioning 50+ mailboxes at a time across multiple tenants.

**Goal:** Make bulk IMAP upload work with Microsoft mailboxes so all sending tools can connect via host/port/username/password without knowing OAuth is involved.

---

## 2. The Solution — Architecture Overview

A proxy server sits between the sending tools and Microsoft:

```
Sending Tool (Smartlead/Instantly/Bison)
    │
    │  Connects with: username + password (password IGNORED)
    │  Host: imap.simpleinboxes.com:993 (SSL)
    ▼
Custom IMAP OAuth Proxy (DigitalOcean server)
    │
    │  Gets OAuth token using Azure AD app credentials
    │  Authenticates with: XOAUTH2 (OAuth 2.0)
    │  Host: outlook.office365.com:993 (SSL)
    ▼
Microsoft Exchange Online
```

The sending tool thinks it's talking to a normal IMAP server. Microsoft thinks it's talking to an authorized OAuth app. Neither knows about the other. The password is completely ignored by the proxy — it uses the app's client credentials instead.

---

## 3. What We Built

### 3.1 Azure AD App Registration (One-Time, Lives Forever)

Registered a single multi-tenant Azure AD application called "Simple Inboxes IMAP Proxy". This app lives permanently in the `octnineallfemalesportstako` tenant. It never needs to be recreated — each new tenant just authorizes this same app.

```
Home Tenant:      octnineallfemalesportstako.onmicrosoft.com
Client ID:        4abf3428-7905-494b-9184-bdca5d96e0d2
Client Secret:    $AZURE_CLIENT_SECRET (expires March 2028)
```

**Key permission:** `IMAP.AccessAsApp` (Application permission, not delegated)
**Permission GUID:** `5e5addcd-3e8d-4e90-baf5-964efab2b20a`

**CRITICAL:** Keep the home tenant alive. If it's deleted, the app is gone and everything breaks. Consider moving the app to a dedicated infrastructure tenant that's never used for mailboxes.

### 3.2 Custom IMAP Proxy (Replaced email-oauth2-proxy)

We initially used the open-source `email-oauth2-proxy` (3,660 lines, by simonrob on GitHub) but replaced it with a custom proxy (230 lines) because:

- email-oauth2-proxy encrypts cached tokens with the IMAP password, causing "incorrect password" errors when different tools or tests use different passwords
- For CCG flow, it forces `delete_account_token_on_password_error = False`, so wrong passwords never auto-clear
- It has 3,400+ lines of features we don't need (GUI, POP3, interactive OAuth, etc.)

**Custom proxy (`imap_oauth_proxy.py`):**
- 230 lines of Python, zero dependencies (stdlib only)
- Password is completely ignored — proxy uses app credentials from config.json
- Token caching in memory per tenant (auto-refreshes every hour)
- Config reload via SIGHUP (no restart needed to add tenants)
- asyncio-based, handles concurrent connections

**Location on server:** `/root/imap-proxy/imap_oauth_proxy.py`
**Config:** `/root/imap-proxy/config.json`

### 3.3 DNS & SSL

- **IMAP:** A record `imap.simpleinboxes.com` → `146.190.174.23` (Cloudflare, proxy OFF)
- **SMTP:** CNAME `smtp.simpleinboxes.com` → `smtp.office365.com` (Cloudflare, proxy OFF)
- **SSL:** Let's Encrypt certificate at `/etc/letsencrypt/live/imap.simpleinboxes.com/`
- **CRITICAL:** Cloudflare proxy (orange cloud) must be OFF for both records — Cloudflare only proxies HTTP/HTTPS, not IMAP or SMTP

### 3.4 Tenant Onboarding Script

**Location:** `/Users/omermullick/Desktop/powershell scripts/onboard-tenant-imap.ps1`

Fully automated, no browser popups, no Selenium. Uses credential-based auth (ROPC) — requires that tenant admin accounts do NOT have MFA enabled.

---

## 4. Complete Setup Process

### One-Time Per Tenant (Onboarding Script)

Run the onboarding script with the tenant admin credentials. It does all of the following automatically, with no popups:

1. **Get Graph API token** — ROPC flow using admin username + password
2. **Create service principal** — registers the Azure app in the tenant
3. **Grant admin consent** — grants `IMAP.AccessAsApp` via Graph API
4. **Connect to Exchange Online** — `Connect-ExchangeOnline -Credential $creds`
5. **Register Exchange service principal** — `New-ServicePrincipal`
6. **Create security group** — `New-DistributionGroup -Name "IMAP Proxy Access v2" -Type Security`
7. **Create application access policy** — scoped to the security group
8. **Add tenant domain to `config.json`** on the proxy server
9. **Send SIGHUP to proxy** to reload config (no restart needed)

**Save the Service Principal Object ID** — it's different per tenant and needed for per-mailbox setup.

### Per Mailbox (Every Time You Create a New Inbox)

After your existing provisioning flow creates the mailbox, add these **three mandatory steps**:

```powershell
# 1. Add to security group (tells the access policy this mailbox is covered)
Add-DistributionGroupMember -Identity "IMAP Proxy Access v2" -Member "user@domain.com"

# 2. Grant FullAccess to the service principal (THIS IS CRITICAL — without it, auth succeeds but IMAP returns "not connected")
Add-MailboxPermission -Identity "user@domain.com" -User "<ServicePrincipalObjectId>" -AccessRights FullAccess -InheritanceType All

# 3. Ensure UPN matches the email address (only if your provisioning creates mismatched UPNs)
# Microsoft creates UPNs from display names: "Michael Torres" → MichaelTorres@domain.com
# But the SMTP address might be michael.torres@domain.com — these MUST match for IMAP to work
Set-MgUser -UserId "<userId>" -UserPrincipalName "user@domain.com"
```

**What each step does:**

| Step | What | Without It |
|---|---|---|
| Security group | Tells the access policy which mailboxes the app covers | Access policy doesn't apply to the mailbox |
| FullAccess permission | Gives the service principal actual access to mailbox contents | Auth succeeds but "User is authenticated but not connected" — can knock on the door but can't enter |
| UPN match | Makes IMAP login email match the Microsoft user identity | Microsoft can't map the IMAP login to the right mailbox |

### SMTP Setup (Per Mailbox)

SMTP goes directly to Microsoft (no proxy). Each mailbox needs:
- **Account enabled** — shared mailboxes are disabled by default
- **Password set** — via Graph API
- **SMTP auth enabled** — `Set-CASMailbox -Identity "user@domain.com" -SmtpClientAuthenticationDisabled $false`

---

## 5. Credentials for Sending Tools

For every mailbox connected via bulk upload:

```
SMTP Host:      smtp.simpleinboxes.com (or smtp.office365.com)
SMTP Port:      587 (TLS, NOT SSL)
SMTP Username:  user@domain.com
SMTP Password:  [the mailbox's actual password]

IMAP Host:      imap.simpleinboxes.com
IMAP Port:      993 (SSL)
IMAP Username:  user@domain.com
IMAP Password:  [anything — the proxy ignores it, so use the same as SMTP for simplicity]
```

**The IMAP password does not matter.** The proxy completely ignores it. The proxy authenticates to Microsoft using the Azure app's client credentials, not the user's password. Use the same password as SMTP for convenience.

---

## 6. Critical Learnings & Mistakes

### 6.1 Wrong Permission GUID (Cost 30+ minutes)

We initially used `dc890d15-9560-4a4c-9b7f-a736ec74ec40` which is `full_access_as_app` (EWS), NOT `IMAP.AccessAsApp`.

**Correct GUIDs for Office 365 Exchange Online (Resource: 00000002-0000-0ff1-ce00-000000000000):**

| Permission | GUID | Purpose |
|---|---|---|
| IMAP.AccessAsApp | 5e5addcd-3e8d-4e90-baf5-964efab2b20a | IMAP access |
| POP.AccessAsApp | cb842b43-da6e-4506-86fe-bb12199c656d | POP access |
| SMTP.SendAsApp | 7146a1f0-8703-45b3-9eae-527a64c00995 | SMTP sending |
| full_access_as_app | dc890d15-9560-4a4c-9b7f-a736ec74ec40 | EWS (NOT for IMAP) |

**How we caught it:** Decoded the JWT token and saw `roles: ['full_access_as_app']` instead of `roles: ['IMAP.AccessAsApp']`.

### 6.2 FullAccess Mailbox Permission is REQUIRED

**This was the biggest hidden requirement.** The access policy + security group alone is NOT enough. Each mailbox also needs:

```powershell
Add-MailboxPermission -Identity "user@domain.com" -User "<ServicePrincipalObjectId>" -AccessRights FullAccess -InheritanceType All
```

Without this, IMAP auth succeeds but returns "User is authenticated but not connected." We spent 10+ hours thinking this was a propagation delay before discovering the fix.

### 6.3 UPN Must Match SMTP Address

When creating mailboxes via PowerShell, Microsoft sets the UPN (User Principal Name) based on the display name, not the email address. Example:
- Display name: "Michael Torres"
- SMTP: `michael.torres@domain.com`
- UPN: `MichaelTorres@domain.com` (WRONG — missing the dot)

The IMAP XOAUTH2 authentication uses the email address, but Microsoft matches it against the UPN. If they don't match, IMAP fails with "authenticated but not connected."

**Fix:** Always set the UPN to match the SMTP address after creating the mailbox.

### 6.4 Room Mailboxes Work Fine

Both Room and Shared mailboxes work with the IMAP proxy. Mailbox type is NOT a blocker.

### 6.5 Self-Signed SSL Certificates Get Rejected

Self-signed certs worked for Smartlead but failed for Email Bison and caused silent failures in Instantly. Use Let's Encrypt.

### 6.6 Cloudflare Proxy Breaks IMAP

Cloudflare's orange cloud proxy only handles HTTP/HTTPS. IMAP on port 993 doesn't work through it. Set DNS to "DNS only" (grey cloud).

### 6.7 SMTP Port 587 Uses TLS, Not SSL

Port 587 = STARTTLS. Port 465 = SSL. Tools often default to SSL when you enter 587 — manually select TLS.

### 6.8 email-oauth2-proxy Password Caching

The open-source email-oauth2-proxy encrypts cached tokens with the IMAP password. Different passwords for the same mailbox cause "incorrect password" errors. For CCG flow, it forces `delete_account_token_on_password_error = False`, so the cache never auto-clears. This is why we built the custom proxy that ignores passwords entirely.

### 6.9 Missing `requests` Library

The email-oauth2-proxy needed the `requests` pip package to fetch OAuth tokens. Without it, the error was "check your internet connection" — misleading.

### 6.10 Don't Delete and Recreate Access Policies

Deleting and recreating the access policy causes propagation delays for ALL mailboxes, including ones that were previously working. Only do this as a last resort.

---

## 7. Tenant Onboarding — Full Automated Process

### Prerequisites
- PowerShell 7+ with `ExchangeOnlineManagement` module
- Tenant admin credentials (no MFA)
- The Azure app client ID (same for all tenants)

### Script Location
`/Users/omermullick/Desktop/powershell scripts/onboard-tenant-imap.ps1`

### What the Script Does (No Popups)
1. Gets Graph API token via ROPC (username + password, no browser)
2. Creates service principal in the tenant via Graph API
3. Grants `IMAP.AccessAsApp` admin consent via Graph API
4. Connects to Exchange Online via `Connect-ExchangeOnline -Credential $creds`
5. Registers Exchange service principal
6. Creates security group
7. Creates application access policy
8. Adds existing mailboxes to the group + grants FullAccess

### Adding the Tenant to the Proxy
After the onboarding script runs, add the tenant's domain to `config.json`:

```json
{
  "tenants": {
    "existingdomain.com": { ... },
    "newdomain.com": {
      "tenant_id": "new-tenant-id-here",
      "client_id": "4abf3428-7905-494b-9184-bdca5d96e0d2",
      "client_secret": "$AZURE_CLIENT_SECRET"
    }
  }
}
```

Then send SIGHUP to reload: `kill -HUP $(pgrep -f imap_oauth_proxy)`

---

## 8. Per-Mailbox Setup — Complete Checklist

When your provisioning flow creates a new mailbox on an already-onboarded tenant:

```powershell
# Connect to Exchange (credential auth, no popup)
$securePwd = ConvertTo-SecureString "password" -AsPlainText -Force
$creds = New-Object System.Management.Automation.PSCredential("admin@tenant.onmicrosoft.com", $securePwd)
Connect-ExchangeOnline -Credential $creds -ShowBanner:$false

# 1. Create the mailbox (your existing flow)
# ...

# 2. Enable SMTP auth
Set-CASMailbox -Identity "user@domain.com" -SmtpClientAuthenticationDisabled $false

# 3. Add to IMAP security group
Add-DistributionGroupMember -Identity "IMAP Proxy Access v2" -Member "user@domain.com"

# 4. Grant FullAccess to service principal (CRITICAL)
Add-MailboxPermission -Identity "user@domain.com" -User "<ServicePrincipalObjectId>" -AccessRights FullAccess -InheritanceType All

# 5. Ensure UPN matches email (if needed)
# Via Graph API: PATCH /users/{id} with { "userPrincipalName": "user@domain.com" }

# 6. Enable account + set password (via Graph API for shared mailboxes)
# PATCH /users/{id} with { "accountEnabled": true, "passwordProfile": { "password": "...", "forceChangePasswordNextSignIn": false } }
```

**After these steps, the mailbox should work within minutes — no 30-minute wait needed** (the long waits we experienced were due to missing the FullAccess step).

---

## 9. When Users Need to Reconnect Inboxes

**Must reconnect:**
1. Client secret is rotated (expires March 2028)
2. Proxy server IP changes (update DNS)
3. SSL certificate expires (auto-renews via certbot, but verify)

**Do NOT need to reconnect:**
- OAuth tokens refresh (automatic, every hour)
- New mailboxes added to other tenants
- Proxy config updated with new tenants (SIGHUP reload)
- Proxy restarts (tools auto-retry)
- Password changes (proxy ignores passwords)

---

## 10. Monitoring & Maintenance

### Proxy Health
- **Process:** Set up systemd for auto-restart (TODO)
- **Port:** `ss -tlnp | grep 993`
- **Logs:** `/root/imap-proxy/proxy_custom.log` or `journalctl` if using systemd

### SSL Certificate
- **Expiry:** Every 90 days, certbot auto-renews
- **Check:** `certbot certificates`
- **After renewal:** Restart proxy to pick up new cert, or set up certbot deploy hook

### Client Secret
- **Expires:** March 2028
- **Set a calendar reminder** 1 month before
- **Rotation:** Generate new secret in Azure AD → update `config.json` → SIGHUP proxy

### Access Policy
- **Do NOT delete and recreate** — it causes propagation delays for all mailboxes
- **New mailboxes:** Add to group + FullAccess permission (no policy changes needed)
- **Verify:** `Test-ApplicationAccessPolicy -Identity "user@domain.com" -AppId "4abf3428-7905-494b-9184-bdca5d96e0d2"`

---

## 11. Infrastructure Summary

| Component | Detail |
|---|---|
| Azure AD App | "Simple Inboxes IMAP Proxy" — Client ID: 4abf3428-7905-494b-9184-bdca5d96e0d2 |
| App Home Tenant | octnineallfemalesportstako.onmicrosoft.com (DO NOT DELETE) |
| Permission | IMAP.AccessAsApp (5e5addcd-3e8d-4e90-baf5-964efab2b20a) |
| Proxy Server | 146.190.174.23 (DigitalOcean, domain-cleanup-imap-proxy) |
| IMAP Domain | imap.simpleinboxes.com (Cloudflare DNS, proxy OFF) |
| SMTP Domain | smtp.simpleinboxes.com (CNAME → smtp.office365.com, proxy OFF) |
| SSL | Let's Encrypt, auto-renews |
| Software | Custom imap_oauth_proxy.py (230 lines, zero dependencies) |
| Config | /root/imap-proxy/config.json |
| Log | /root/imap-proxy/proxy_custom.log |
| OAuth Token Lifetime | 1 hour (auto-refreshed, no user action) |
| Client Secret Expiry | March 2028 |
| Tested With | Smartlead, Instantly, Email Bison |

---

## 12. Scaling Considerations

### Single App vs Multiple Apps
For 50,000+ inboxes, consider splitting across 3-5 Azure AD apps on different infrastructure tenants to avoid single point of failure. The proxy config already supports this — each domain can point to a different client_id/client_secret.

### Server Capacity
The proxy is extremely lightweight. A $5/month droplet handles 5,000+ mailboxes easily. For production uptime, add:
- systemd service (auto-restart on crash)
- Optional: second server with DNS failover for 99.9%+ uptime

### If Proxy Goes Down
- **Sending (SMTP)** is UNAFFECTED — goes directly to Microsoft
- **Reading (IMAP)** stops — sending tools can't check for replies
- **No emails are lost** — they sit in the Microsoft mailbox until the proxy is back

### Tenant Threshold (5.7.705)
Microsoft's per-tenant outbound spam threshold cannot be configured at the tenant level. Mitigations:
- Per-mailbox limits via `Set-HostedOutboundSpamFilterPolicy` (requires `Enable-OrganizationCustomization`)
- Limit mailboxes per tenant (10-15 is safe)
- Warm up new tenants gradually
- For full control: route outbound through an SMTP relay (Lunatro-style) — separate project

---

## 13. Files & Locations

| File | Location | Purpose |
|---|---|---|
| Custom proxy | `/root/imap-proxy/imap_oauth_proxy.py` (server) | The IMAP OAuth proxy |
| Proxy config | `/root/imap-proxy/config.json` (server) | Tenant → credential mapping |
| SSL cert | `/etc/letsencrypt/live/imap.simpleinboxes.com/` (server) | TLS certificate |
| Onboarding script | `/Users/omermullick/Desktop/powershell scripts/onboard-tenant-imap.ps1` (local) | Automated tenant setup |
| This report | `/Users/omermullick/Downloads/Projects/imap-proxy/IMAP_OAuth_Proxy_Report.md` (local) | Documentation |
| Old proxy (unused) | `/root/imap-proxy/emailproxy.py` (server) | Replaced by custom proxy |

---

## 14. TODO

1. **Set up systemd service** — auto-restart proxy on crash/reboot
2. **Certbot deploy hook** — auto-restart proxy on cert renewal
3. **Update onboarding script** — add FullAccess mailbox permission step and UPN fix
4. **Multi-tenant rollout** — onboard all active tenants
5. **Integrate into provisioning pipeline** — add group membership + FullAccess + UPN fix to mailbox creation flow
6. **Consider multiple Azure AD apps** — for 50k+ scale, split across 3-5 apps
7. **Monitor** — health checks, alerting if proxy goes down
