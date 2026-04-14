#!/bin/bash
set -e

echo "============================================="
echo "  sig-relay starting"
echo "  $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "============================================="
echo "Hostname: ${RELAY_HOSTNAME}"
echo "Domains:  ${MANAGED_DOMAINS}"
echo "Milter fail: ${MILTER_FAIL_ACTION}"
echo "============================================="

# ---- Generate Postfix main.cf ----
cat > /etc/postfix/main.cf << EOF
# KawaSig Relay — auto-generated, do not edit
smtpd_banner = \$myhostname ESMTP KawaSig Relay
biff = no
append_dot_mydomain = no

myhostname = ${RELAY_HOSTNAME}
mydomain = ${RELAY_HOSTNAME#*.}
myorigin = \$mydomain
mydestination =
mynetworks = ${POSTFIX_MYNETWORKS:-127.0.0.0/8 [::1]/128 172.17.0.0/16 172.18.0.0/16 172.19.0.0/16 172.20.0.0/16}
relayhost =
inet_interfaces = all
inet_protocols = ipv4

# Recipient restrictions:
#  - permit_mynetworks: the docker host + localhost (for loopback tests)
#  - permit_sasl_authenticated: for auth'd connections (not used by default)
#  - check_sender_access: only accept mail FROM our managed domains
#  - reject_unauth_destination: block open-relay abuse
#
# Exchange Online IPs change too often to hardcode; Microsoft publishes the
# current list at https://learn.microsoft.com/en-us/microsoft-365/enterprise/urls-and-ip-address-ranges
# In production set POSTFIX_MYNETWORKS in .env to those CIDRs (comma/space
# separated) OR restrict at the firewall so only MS EOP can reach :25.
smtpd_recipient_restrictions =
    permit_mynetworks,
    check_sender_access hash:/etc/postfix/sender_access,
    reject_unauth_destination

# TLS inbound (from Exchange). Enforce modern protocols only — TLS 1.0 and
# 1.1 are deprecated; every MX worth talking to speaks 1.2 by 2026.
smtpd_use_tls = yes
smtpd_tls_cert_file = ${RELAY_TLS_CERT}
smtpd_tls_key_file = ${RELAY_TLS_KEY}
smtpd_tls_security_level = may
smtpd_tls_protocols = >=TLSv1.2
smtpd_tls_mandatory_protocols = >=TLSv1.2
smtpd_tls_ciphers = high
smtpd_tls_mandatory_ciphers = high
smtpd_tls_loglevel = 1
smtpd_tls_received_header = yes

# TLS outbound (to destination MX). Same protocol floor; still opportunistic
# (may) — a receiver that can't speak TLS 1.2 gets plaintext rather than a
# delivery failure.
smtp_tls_security_level = may
smtp_tls_protocols = >=TLSv1.2
smtp_tls_mandatory_protocols = >=TLSv1.2
smtp_tls_ciphers = high
smtp_tls_mandatory_ciphers = high
smtp_tls_loglevel = 1

# Preserve MIME structure end-to-end. Without this, postfix's cleanup may
# rewrite 8BITMIME -> 7bit which further breaks DKIM and confuses downstream
# validators.
disable_mime_output_conversion = yes

# Milter — postfix smtpd runs chrooted in /var/spool/postfix on Debian/Ubuntu,
# so the socket path must be relative to that chroot.
smtpd_milters = unix:/milter/sig-milter.sock
non_smtpd_milters = unix:/milter/sig-milter.sock
milter_default_action = ${MILTER_FAIL_ACTION}
milter_protocol = 6

# Logging
maillog_file = /var/log/sig-relay/postfix.log

# Limits — 150MB matches Exchange Online's outbound cap.
message_size_limit = ${MAX_MESSAGE_SIZE:-157286400}
maximal_queue_lifetime = 1d
bounce_queue_lifetime = 1d

# Per-source rate limits so a single rogue host can't exhaust the relay.
# These apply to ALL clients including Exchange — set high enough that normal
# EOP traffic is never throttled, low enough that a bad actor is slowed.
anvil_rate_time_unit = 60s
smtpd_client_connection_count_limit = ${SMTPD_CONN_LIMIT:-50}
smtpd_client_connection_rate_limit = ${SMTPD_CONN_RATE:-300}
smtpd_client_message_rate_limit = ${SMTPD_MSG_RATE:-600}
EOF

# ---- Allow senders from any MANAGED_DOMAINS — regardless of source IP ----
# Postfix smtpd runs chrooted to /var/spool/postfix on Debian/Ubuntu, so the
# hash table needs to exist INSIDE that chroot to be readable. Build it in
# /etc/postfix then mirror to the chroot path.
: > /etc/postfix/sender_access
IFS=',' read -ra DOMS <<< "${MANAGED_DOMAINS}"
for d in "${DOMS[@]}"; do
    d="${d// /}"
    [ -n "$d" ] && echo "$d OK" >> /etc/postfix/sender_access
done
postmap /etc/postfix/sender_access
mkdir -p /var/spool/postfix/etc/postfix
cp /etc/postfix/sender_access /etc/postfix/sender_access.db \
   /var/spool/postfix/etc/postfix/

# ---- Milter socket directory ----
mkdir -p /var/spool/postfix/milter
chmod 777 /var/spool/postfix/milter

# ---- In-container log rotation (postfix.log + milter.log + deploy.log) ----
cat > /etc/logrotate.d/kawasig << 'LOGROT'
/var/log/sig-relay/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
LOGROT

# ---- Start supervisor (Postfix + milter) ----
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/sig-relay.conf
