# KawaSig — Self-Hosted M365 Email Signature Platform

Multi-client, Docker-first email-signature platform for Microsoft 365.
Pairs [Set-OutlookSignatures](https://github.com/Set-OutlookSignatures/Set-OutlookSignatures)
(client-side push via Graph) with a Postfix + Python milter relay (server-side fallback)
so **every outbound email carries a properly placed, dynamically-templated signature** —
no matter which mail client the user sent from, no double signatures, perfectly placed on
replies and forwards.

Three containers. One `docker compose up`. Define a template, drop it in, restart — done.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Quick Start](#2-quick-start)
3. [Repository Layout](#3-repository-layout)
4. [Microsoft 365 / Entra ID Setup](#4-microsoft-365--entra-id-setup)
5. [Exchange Online Mail Flow Setup](#5-exchange-online-mail-flow-setup)
6. [DNS, SPF, TLS Setup](#6-dns-spf-tls-setup)
7. [Populating User Profiles](#7-populating-user-profiles)
8. [Adding a New Client / Domain](#8-adding-a-new-client--domain)
9. [Template Variable Reference](#9-template-variable-reference)
10. [Operations](#10-operations)
11. [Testing](#11-testing)
12. [Monitoring & Health Checks](#12-monitoring--health-checks)
13. [Troubleshooting](#13-troubleshooting)
14. [Security Notes](#14-security-notes)
15. [License](#15-license)

---

## 1. Architecture

```
                     +-------------------------------+
                     |   Microsoft Entra ID          |
                     |   (one app registration)      |
                     |                               |
                     |   App: KawaSig Platform       |
                     |   App permissions:            |
                     |     User.Read.All             |
                     |     Mail.ReadWrite            |
                     |     MailboxSettings.ReadWrite |
                     |     Organization.Read.All     |
                     |   Auth: Client credentials    |
                     +---------------+---------------+
                                     |
              +----------------------+----------------------+
              |                                             |
              v                                             v
   +-------------------+                       +-------------------------+
   |  sig-deployer     |                       |  sig-relay              |
   |                   |                       |                         |
   |  PowerShell 7     |                       |  Postfix + Python       |
   |  cron */30 min    |                       |  milter (pymilter)      |
   |                   |                       |                         |
   |  For each user:   |                       |  For each inbound mail: |
   |  1 query Graph    |                       |  1 envelope-from check  |
   |  2 render HTM     |                       |  2 marker present?      |
   |  3 push roaming   |                       |    yes -> pass through  |
   |    signature      |                       |  3 query Graph (cached) |
   |  4 set defaults   |                       |  4 render Jinja2        |
   |    (new+reply)    |                       |  5 inject at boundary   |
   |                   |                       |    (Outlook/iOS/Gmail)  |
   |  Outlook Win/Mac/ |                       |  6 forward to MX        |
   |  Web/New Outlook  |                       |                         |
   +-------------------+                       |  iOS Mail, Apple Mail,  |
                                               |  Gmail app, anything    |
                                               |  that didn't get the    |
                                               |  client-side push       |
                                               +-----------+-------------+
                                                           |
                                                           v
                                               +-----------------------+
                                               |  redis (cache)        |
                                               |  Graph profiles, 1h   |
                                               +-----------------------+
```

### Failover chain

```
1. sig-deployer (client-side, Outlook roaming signatures)
     - inserted AFTER the reply, BEFORE the quoted thread
     - handles ~90% of outbound email
     - if cron fails, users keep last-pushed signature

2. sig-relay (server-side fallback)
     - Exchange Online outbound connector routes mail through this host
     - milter detects missing <!--KAWASIG:domain--> marker and injects
     - if the relay is down, Exchange falls back to direct MX

3. Direct MX delivery (no signature)
     - email always delivers
     - worst case: no signature
     - mail flow is NEVER blocked
```

### User freedom

If a user customises their signature and **keeps ANY of the three markers** below,
the relay sees it and passes through. If they remove all three, the relay re-injects
on their next message. They are never blocked from customising.

The markers are redundant by design:

| Marker                               | Where                | Survives            |
|--------------------------------------|----------------------|---------------------|
| `<!--KAWASIG:domain-->`              | HTML body            | Most paths          |
| `data-kawasig="domain"`              | HTML attribute       | Outlook reply/fwd   |
| `[KAWASIG:domain]`                   | text/plain body      | text-mode clients   |

Outlook is known to strip HTML comments on certain reply/forward paths, so the
`data-kawasig` attribute on the signature's outermost `<table>` is the primary
defence against double-signatures.

---

## 2. Quick Start

```bash
# 1. Clone
cd /opt
git clone <your-fork-url> kawasig
cd kawasig

# 2. Configure
cp .env.example .env
nano .env
# Fill in: TENANT_ID, CLIENT_ID, CLIENT_SECRET, MANAGED_DOMAINS, RELAY_HOSTNAME

# 3. TLS cert (Let's Encrypt)
sudo apt install -y certbot
sudo certbot certonly --standalone -d $(grep ^RELAY_HOSTNAME .env | cut -d= -f2)
sudo cp /etc/letsencrypt/live/<host>/fullchain.pem sig-relay/certs/
sudo cp /etc/letsencrypt/live/<host>/privkey.pem   sig-relay/certs/
sudo chmod 644 sig-relay/certs/fullchain.pem
sudo chmod 600 sig-relay/certs/privkey.pem

# 4. Build + start
docker compose up -d --build
# or: ./scripts/build-and-up.sh

# 5. Verify
docker compose ps
docker compose logs -f
```

Hardware: 2 vCPU, 2 GB RAM, 20 GB disk minimum. Ports: inbound 25 (from Exchange Online),
outbound 25 (to destination MX), outbound 443 (Graph + OAuth). Verify your hosting
provider does not block outbound port 25 — many cloud VPS providers do.

---

## 3. Repository Layout

```
.
├── docker-compose.yml            # 3-container stack
├── .env.example                  # copy to .env, fill in
├── README.md                     # this file
│
├── sig-deployer/                 # Container 1
│   ├── Dockerfile
│   ├── entrypoint.sh             # generates cron, kicks initial deploy
│   └── config/graph-config.ps1   # headless client-credentials Graph auth
│
├── sig-relay/                    # Container 2
│   ├── Dockerfile
│   ├── entrypoint.sh             # generates Postfix main.cf at runtime
│   ├── supervisord.conf          # supervises postfix + milter
│   ├── certs/                    # TLS cert+key (gitignored)
│   └── milter/
│       ├── sig_milter.py         # main milter (envelope + body inspection)
│       ├── graph_client.py       # MSAL + Redis cache (lazy auth init)
│       ├── body_parser.py        # reply-boundary detection
│       ├── template_renderer.py  # Jinja2 rendering
│       ├── requirements.txt
│       └── tests/                # pytest unit tests + HTML fixtures
│
├── redis/redis.conf              # Container 3 — cache config
│
├── templates/
│   ├── signatures/               # Set-OutlookSignatures HTM templates ($CurrentUser...$)
│   │   ├── TRE-Company.htm
│   │   ├── KawaConnect-Company.htm
│   │   └── _Signatures.ini       # template -> domain mapping
│   └── relay/                    # Jinja2 templates for milter ({{ var }}), one per domain
│       ├── treconstruction.net.html
│       └── kawaconnect.com.html
│
├── scripts/
│   ├── build-and-up.sh           # cp .env.example .env (if missing); build; up
│   ├── health-check.sh           # cron-friendly status checker (optional alert webhook)
│   ├── test-relay.sh             # send 3 test messages through the relay
│   ├── rotate-secret.sh          # update CLIENT_SECRET in .env, restart
│   ├── update-users.ps1          # bulk-populate Entra user fields from CSV
│   └── users.csv.example
│
└── src_Set-OutlookSignatures/    # upstream Set-OutlookSignatures (untouched)
```

---

## 4. Microsoft 365 / Entra ID Setup

### 4.1 Register the application

1. Go to <https://entra.microsoft.com>, sign in as **Global Administrator** or
   **Application Administrator**.
2. **Identity → Applications → App registrations → + New registration**.
3. **Name:** `KawaSig Platform`. **Account type:** *Accounts in this organizational
   directory only*. **Redirect URI:** leave blank. Click **Register**.
4. From the Overview page, copy:
   - **Application (client) ID** → put in `.env` as `CLIENT_ID`
   - **Directory (tenant) ID** → put in `.env` as `TENANT_ID`

### 4.2 Create a client secret

1. **Certificates & secrets → Client secrets → + New client secret**.
2. **Description:** `kawasig-prod`. **Expires:** 24 months. Click **Add**.
3. **Immediately copy the Value** (not the Secret ID) → `CLIENT_SECRET` in `.env`. The
   value is shown once; if you miss it, delete and recreate.
4. Set a calendar reminder two weeks before expiry. When you rotate, run
   `./scripts/rotate-secret.sh <new-value>`.

### 4.3 Add API permissions

1. **API permissions → + Add a permission → Microsoft Graph → Application permissions**.
2. Add each of:

   | Permission                     | Why                                                   |
   |--------------------------------|-------------------------------------------------------|
   | `User.Read.All`                | read displayName, jobTitle, phone, etc. for all users |
   | `Mail.ReadWrite`               | write roaming signatures into Exchange Online mailbox |
   | `MailboxSettings.ReadWrite`    | set the default signature for new/reply/forward       |
   | `Organization.Read.All`        | read tenant configuration                             |

3. Click **Grant admin consent for &lt;tenant&gt;** → **Yes**. Verify all rows show green
   checkmarks.

### 4.4 (Optional) Scope to specific mailboxes

If you don't want the app to access ALL mailboxes:

```powershell
Install-Module ExchangeOnlineManagement -Force
Connect-ExchangeOnline -UserPrincipalName admin@yourdomain.com

New-DistributionGroup -Name "KawaSig-Scope" -Type Security
Add-DistributionGroupMember -Identity "KawaSig-Scope" -Member john@yourdomain.com
Add-DistributionGroupMember -Identity "KawaSig-Scope" -Member jane@yourdomain.com

New-ApplicationAccessPolicy `
    -AppId "<your-client-id>" `
    -PolicyScopeGroupId "KawaSig-Scope" `
    -AccessRight RestrictAccess `
    -Description "KawaSig signature platform access"

Test-ApplicationAccessPolicy -Identity john@yourdomain.com -AppId "<your-client-id>"
# Expected: Granted
```

### 4.5 Multi-tenant deployments

If the client domains are in **different M365 tenants**:

- **Recommended (small MSP):** register a separate Entra app in each tenant, run a
  separate `.env` and Docker stack per tenant. Simpler and more secure.
- **Alternative:** register the app as multi-tenant (*Accounts in any organizational
  directory*) and configure cross-tenant access. Complex; admin consent required in
  each tenant.

---

## 5. Exchange Online Mail Flow Setup

### 5.1 Create the outbound connector

1. <https://admin.exchange.microsoft.com> → **Mail flow → Connectors → + Add a connector**.
2. **Connection from:** `Office 365`. **Connection to:** `Partner organization`.
3. **Name:** `KawaSig Relay`. Description: routes outbound mail through self-hosted
   signature relay.
4. **Use of connector:** *Only when email messages are sent to these domains* → add `*`.
5. **Routing:** *Route email through these smart hosts* → add your relay hostname
   (e.g. `mail-relay.treconstruction.net`).
6. **Security:** *Always use TLS* → *Issued by a trusted certificate authority*.
7. Validate with an external email address. **Save**.

### 5.2 Create the mail flow rule

1. **Mail flow → Rules → + Add a rule → Create a new rule**.
2. **Name:** `Route to KawaSig Relay`.
3. **Apply if:** *The sender domain is* `<each managed domain>` **AND** *the recipient
   is external*.
4. **Do the following:** *Redirect to connector* `KawaSig Relay`.
5. **Except if:** *The message headers include* `X-KawaSig-Processed` (loop-prevention
   in case the relay re-injects to Exchange).
6. **Mode:** Enforce. **Priority:** 0. **Save**.

### 5.3 Disable any existing transport-rule disclaimer

If you currently have an Exchange transport rule that appends a signature/disclaimer,
**disable it before enabling the new system** or you will get double signatures.

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@yourdomain.com
Get-TransportRule | Select-Object Name, State, Priority
Disable-TransportRule -Identity "<old-rule-name>" -Confirm:$false
Get-TransportRule -Identity "<old-rule-name>" | Select-Object Name, State
# State should be: Disabled
```

---

## 6. DNS, SPF, TLS Setup

### 6.1 DNS A record

```
mail-relay.<your-domain>.   IN  A  <your-server-public-ip>
```

### 6.2 Reverse DNS (PTR)

Ask your hosting/ISP provider to set the PTR record for the server's IP back to
`mail-relay.<your-domain>`. Without this, destination mail servers may flag your relay
as spam.

### 6.3 SPF

Add the relay IP to the SPF TXT record of every managed domain.

```
; before (Exchange Online only):
example.com.   IN  TXT  "v=spf1 include:spf.protection.outlook.com -all"

; after (Exchange Online + relay):
example.com.   IN  TXT  "v=spf1 ip4:<relay-ip> include:spf.protection.outlook.com -all"
```

### 6.4 Exchange Online inbound IPs

Exchange Online's IP ranges change regularly. The relay handles this with two layers:

1. **`mynetworks`** — localhost + private docker ranges by default; add EOP CIDRs via
   `POSTFIX_MYNETWORKS` in `.env` if you want IP-based trust.
2. **`check_sender_access hash:/etc/postfix/sender_access`** — generated at startup from
   `MANAGED_DOMAINS`. Any sender `@managed.com` is allowed regardless of source IP.
   This is how EOP mail gets in: the Exchange connector presents as a sender from one
   of your domains, which matches the map.

Authoritative EOP CIDR list: <https://learn.microsoft.com/en-us/microsoft-365/enterprise/urls-and-ip-address-ranges>.

Production recommendation: firewall inbound 25 to only accept packets from EOP CIDRs
at the OS/cloud level. The `check_sender_access` map is defense-in-depth, not the
primary access control.

### 6.5 TLS auto-renewal

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/kawasig.sh > /dev/null << 'EOF'
#!/bin/bash
CERT_DIR="/opt/kawasig/sig-relay/certs"
DOMAIN="mail-relay.your-domain.com"
cp "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" "${CERT_DIR}/fullchain.pem"
cp "/etc/letsencrypt/live/${DOMAIN}/privkey.pem"  "${CERT_DIR}/privkey.pem"
chmod 644 "${CERT_DIR}/fullchain.pem"
chmod 600 "${CERT_DIR}/privkey.pem"
docker compose -f /opt/kawasig/docker-compose.yml restart sig-relay
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/kawasig.sh
sudo certbot renew --dry-run
```

---

## 7. Populating User Profiles

The signatures pull data from Entra ID. If a user's `displayName`, `jobTitle`,
`businessPhones[0]`, etc. are empty, the rendered signature will have blank spots.

### Inspect a user

```powershell
Connect-MgGraph -Scopes "User.Read.All"
Get-MgUser -UserId "john@yourdomain.com" `
    -Property displayName,jobTitle,businessPhones,mail,mobilePhone,department,officeLocation,streetAddress,city,state,postalCode `
    | Format-List
```

### Update one user

```powershell
Update-MgUser -UserId "john@yourdomain.com" `
    -JobTitle "Project Manager" `
    -BusinessPhones @("+1 (412) 555-0100") `
    -Department "Operations" `
    -OfficeLocation "Suite 3A" `
    -StreetAddress "1800 Pine Hollow Rd" `
    -City "McKees Rocks" `
    -State "PA" `
    -PostalCode "15136" `
    -Country "US"
```

### Bulk update from CSV

```bash
cp scripts/users.csv.example scripts/users.csv
# edit scripts/users.csv with real values
pwsh ./scripts/update-users.ps1 -CsvPath scripts/users.csv
```

---

## 8. Adding a New Client / Domain

```bash
# 1. Add to .env
sed -i 's/^MANAGED_DOMAINS=.*/&,newclient.com/' .env

# 2. Set-OutlookSignatures HTM template (client-side). The template MUST carry
#    BOTH the comment AND a data-kawasig="<domain>" attribute on the outermost
#    element — Outlook reliably preserves data-* attributes but may strip HTML
#    comments on some reply/forward paths. Belt + braces = no double signatures.
cat > templates/signatures/NewClient-Company.htm << 'EOF'
<!--KAWASIG:newclient.com-->
<table data-kawasig="newclient.com">
  ...$CurrentUserDisplayName$...$CurrentUserMail$...
</table>
<!--/KAWASIG:newclient.com-->
EOF

# 3. Mapping — Set-OutlookSignatures INI uses BARE tokens, no "= value".
# Filter matching is LITERAL, no wildcards. Use an Entra ID group or list
# users explicitly.
cat >> templates/signatures/_Signatures.ini << 'EOF'

[NewClient-Company.htm]
DefaultNew
DefaultReplyFwd
EntraID KawaSig-NewClient@newclient.com
# Or per-user lines instead of the group:
# user1@newclient.com
# user2@newclient.com
EOF

# 4. Relay Jinja2 template (server-side) — filename MUST be the domain,
#    same dual-marker requirement as the HTM template above.
cat > templates/relay/newclient.com.html << 'EOF'
<!--KAWASIG:newclient.com-->
<table data-kawasig="newclient.com">
  ...{{ display_name }}...{{ email }}...
</table>
<!--/KAWASIG:newclient.com-->
EOF

# 5. Restart (sig-relay reads templates at startup)
docker compose restart sig-deployer sig-relay
```

That's it — no code changes.

---

## 9. Template Variable Reference

The same Graph attribute is named differently in each system. Both templates render
to the same visual output.

| Graph attribute    | Set-OutlookSignatures HTM      | Relay Jinja2 HTML  |
|--------------------|--------------------------------|--------------------|
| displayName        | `$CurrentUserDisplayName$`     | `{{ display_name }}` |
| givenName          | `$CurrentUserGivenName$`       | `{{ given_name }}`   |
| surname            | `$CurrentUserSurname$`         | `{{ surname }}`      |
| jobTitle           | `$CurrentUserTitle$`           | `{{ job_title }}`    |
| companyName        | `$CurrentUserCompany$`         | `{{ company }}`      |
| department         | `$CurrentUserDepartment$`      | `{{ department }}`   |
| businessPhones[0]  | `$CurrentUserTelephoneNumber$` | `{{ phone }}`        |
| mobilePhone        | `$CurrentUserMobile$`          | `{{ mobile }}`       |
| mail               | `$CurrentUserMail$`            | `{{ email }}`        |
| officeLocation     | `$CurrentUserOffice$`          | `{{ office }}`       |
| streetAddress      | `$CurrentUserStreetAddress$`   | `{{ street }}`       |
| city               | `$CurrentUserCity$`            | `{{ city }}`         |
| state              | `$CurrentUserState$`           | `{{ state }}`        |
| postalCode         | `$CurrentUserPostalCode$`      | `{{ postal_code }}`  |
| country            | `$CurrentUserCountry$`         | `{{ country }}`      |

The HTM template is pushed into mailboxes by sig-deployer; the Jinja2 template is
rendered server-side by the milter when the marker is missing. Keep them visually
identical so the user sees the same thing whether the signature was inserted client- or
server-side.

---

## 10. Operations

```bash
docker compose ps                              # status of all 3 containers
docker compose logs -f                         # tail everything
docker compose logs -f sig-relay               # tail relay only
docker compose logs --tail=200 sig-deployer    # last 200 lines of deployer

docker compose restart sig-deployer sig-relay  # re-read .env / templates
docker compose down                            # stop everything
docker compose up -d --build                   # rebuild and restart

# Force a deployer run NOW (don't wait for the cron)
docker exec kawasig-deployer /opt/run-deploy.sh

# Inspect the milter socket
docker exec kawasig-relay ls -la /var/spool/postfix/milter/

# Check Redis cache contents
docker exec kawasig-redis redis-cli KEYS 'kawasig:profile:*'
docker exec kawasig-redis redis-cli GET 'kawasig:profile:john@yourdomain.com'
```

---

## 11. Testing

### Unit tests (no Docker, no secrets)

```bash
python3 -m pytest sig-relay/milter/tests/ -v
```

### End-to-end via SMTP

```bash
# From the host (bridge IP must be in mynetworks):
sudo apt install -y swaks
./scripts/test-relay.sh

# Or from inside the container (always works):
docker exec kawasig-relay python3 -c "
import smtplib
from email.mime.text import MIMEText
m = MIMEText('<html><body><p>Test inject.</p></body></html>', 'html')
m['Subject']='test'; m['From']='user@yourdomain.com'; m['To']='external@example.com'
s = smtplib.SMTP('127.0.0.1', 25)
s.sendmail(m['From'], [m['To']], m.as_string()); s.quit()
"
docker compose logs --tail=20 sig-relay
```

Expected log lines:

- Inject path: `No signature found for <addr>, injecting...` followed by either
  `Signature injected: <addr> -> <recipient>` or
  `No profile for <addr>, passing through` (the latter means Graph returned 404 / no
  user, so nothing was injected — mail still delivered).
- Passthrough: silent — the milter ACCEPTs without writing a log line.
- Skip: `Skipping: <external-addr>` at debug level.

### Force a real M365 user lookup

After filling in real credentials and grants in `.env`:

```bash
docker exec kawasig-relay /opt/milter-venv/bin/python3 -c "
from graph_client import GraphClient
import json
print(json.dumps(GraphClient().get_user_profile('user@yourdomain.com'), indent=2))
"
```

---

## 12. Monitoring & Health Checks

Drop the included script into cron:

```bash
sudo tee /etc/cron.d/kawasig-health > /dev/null << 'EOF'
*/5 * * * * root /opt/kawasig/scripts/health-check.sh
EOF
```

Set `ALERT_WEBHOOK_URL` in `.env` to a Slack/Discord/Teams webhook for failure
notifications. The script checks all three containers and probes SMTP on port 25.

---

## 13. Troubleshooting

| Symptom                                            | Cause / Fix                                                                                       |
|----------------------------------------------------|---------------------------------------------------------------------------------------------------|
| Relay log: `Token acquisition failed`              | Wrong CLIENT_ID/SECRET/TENANT_ID. Verify `.env` matches Entra portal.                             |
| Relay log: `User not found in Graph`               | User email doesn't match Entra UPN. Check `mail` vs `userPrincipalName` in Entra.                 |
| Relay log: `No template for domain`                | Missing `templates/relay/<domain>.html`. Filename must equal the domain (lowercase).              |
| Relay log: `Loaded template for <domain> (0 bytes)` | Template file exists but is empty. Check `templates/relay/<domain>.html`, rewrite, `docker compose restart sig-relay`. |
| Signature has blank fields                         | Entra user profile fields empty. See `update-users.ps1`.                                          |
| Deployer logs nothing useful, exits code 14        | `Problem connecting to Microsoft Graph`. Check TENANT_ID / CLIENT_ID / CLIENT_SECRET. MSAL `validate_authority=False` is off for the milter, so the error is loud there; deployer validates through Set-OutlookSignatures. |
| Deployer runs but no signatures are deployed       | Check `_Signatures.ini` — tags must be bare (`DefaultNew`, not `DefaultNew = X`). Filter matching is LITERAL: use Entra group `EntraID <name>@<domain>` or explicit user emails, NOT domain wildcards. |
| Relay rejects production Exchange mail with 554    | Source IP not in `mynetworks`. Either firewall 25 to EOP-only and leave restrictive mynetworks, or add EOP CIDRs via `POSTFIX_MYNETWORKS`. sender_access already allows mail FROM `MANAGED_DOMAINS` regardless of IP. |
| Double signatures on replies                       | Old transport rule still active. Disable it in Exchange admin (see §5.3).                         |
| Postfix log: `connect to Milter ... Permission denied` | Socket umask. Already handled by `os.umask(0o000)` in `sig_milter.py`. Restart relay.          |
| Postfix log: `connect to Milter ... No such file or directory` | smtpd is chrooted in `/var/spool/postfix`; main.cf must use `unix:/milter/sig-milter.sock` (not the absolute path). Already fixed in `entrypoint.sh`. |
| Exchange can't connect to relay                    | Firewall blocking inbound 25 from Exchange Online IP ranges (see §6.4).                           |
| Email stuck in queue                               | Outbound 25 blocked by hosting provider. Common on cloud VPS — request unblock or move to a VPS that allows it. |
| Relay not injecting for some client (e.g. iOS)     | Reply-boundary regex doesn't match. Inspect `sig-relay/milter/body_parser.py` and add a pattern.  |
| sig-deployer exits with code 14                    | "Problem connecting to Microsoft Graph" — placeholder credentials, or app permissions not granted. |
| sig-deployer hangs ~120s on startup                | Set-OutlookSignatures shows a free-tier "welcome" splash; clears automatically.                   |
| Redis connection refused                           | `docker compose up -d redis`.                                                                     |
| Build fails: `No space left on device`             | `docker system prune -af` and free disk; `docker compose build` needs ~2GB.                       |

---

## 14. Security Notes

- `.env` and `sig-relay/certs/*.pem` are gitignored. Never commit them.
- Rotate `CLIENT_SECRET` every 24 months: `./scripts/rotate-secret.sh <new-value>`.
- The relay accepts SMTP from any IP by default. In production, restrict
  `mynetworks` and/or use firewall rules to allow only Exchange Online IP ranges.
- The milter never blocks mail flow: on any internal failure it returns ACCEPT and
  the message is delivered without modification (`MILTER_FAIL_ACTION=accept`).
- `MILTER_FAIL_ACTION=tempfail` will make Postfix 4xx-defer messages when the milter
  is down — Exchange will retry. Use only if you understand the trade-off.

### DKIM / SPF / DMARC behavior

**Exchange Online DKIM-signs mail before routing through our connector.** When the
milter modifies the body, the inbound DKIM signature becomes invalid. To prevent
receivers from logging a `dkim=fail`, the milter **strips `DKIM-Signature` headers
before forwarding** whenever it modifies the body. You see this in logs as:

```
Stripped N inbound DKIM-Signature header(s)
```

Implications for DMARC alignment at the recipient:

- **SPF alignment** must pass. That means your domain's SPF TXT record MUST
  include the relay's public IP — see §6.3.
- **DKIM alignment** will no longer pass for messages we inject into, because we
  removed the signature. DMARC policy evaluation will fall back to SPF-only.
- Pass-through messages (marker already present) are **not modified**, so their
  original DKIM signatures remain valid.
- If you need DKIM alignment on injected mail too, add an OpenDKIM milter to the
  relay and sign with your domain's private key after our milter runs. That's a
  future addition; the current shipped stack does not DKIM-sign.

**Loop prevention** is belt-and-braces: the milter stamps `X-KawaSig-Processed:
<domain>` on every managed-sender message, and any inbound message that *already*
carries that header is skipped (no re-injection). Combined with the Exchange
transport rule's "except if X-KawaSig-Processed is set" exception, there is no
way for a message to loop through the relay.

---

## 15. License

Set-OutlookSignatures core (under `src_Set-OutlookSignatures/`) is licensed under the
[European Union Public License (EUPL) 1.2](https://joinup.ec.europa.eu/collection/eupl/eupl-text-eupl-12).

The KawaSig containers (Dockerfiles, milter, helper scripts, templates) are original
work by KawaConnect LLC.
