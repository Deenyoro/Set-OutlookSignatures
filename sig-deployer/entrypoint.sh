#!/bin/bash
set -e

echo "============================================="
echo "  sig-deployer starting"
echo "  $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "============================================="
echo "Tenant:   ${TENANT_ID}"
echo "Client:   ${CLIENT_ID}"
echo "Domains:  ${MANAGED_DOMAINS}"
echo "Schedule: ${DEPLOY_CRON}"
echo "Dry Run:  ${DEPLOY_DRY_RUN}"
echo "============================================="

# ---- Sanity-check HTM signature templates up front. ----
# Operator-facing: if a template is missing the dual-marker, double-signatures
# will result on server-side fallback. Warn before cron ever runs.
TEMPLATE_DIR="/opt/templates/signatures"
echo "Validating signature templates in ${TEMPLATE_DIR}..."
if [ ! -d "${TEMPLATE_DIR}" ]; then
    echo "WARN: template directory missing: ${TEMPLATE_DIR}"
elif ! ls "${TEMPLATE_DIR}"/*.htm >/dev/null 2>&1; then
    echo "WARN: no .htm templates found in ${TEMPLATE_DIR}"
else
    for tmpl in "${TEMPLATE_DIR}"/*.htm; do
        name="$(basename "$tmpl")"
        has_comment=0
        has_dataattr=0
        grep -q '<!--KAWASIG:' "$tmpl" 2>/dev/null && has_comment=1
        grep -q 'data-kawasig=' "$tmpl" 2>/dev/null && has_dataattr=1
        if [ "$has_comment$has_dataattr" = "11" ]; then
            echo "  OK    ${name}    (both markers present)"
        else
            echo "  WARN  ${name}    comment=${has_comment} data-attr=${has_dataattr} — re-inject risk"
        fi
    done
fi
if [ ! -f "${TEMPLATE_DIR}/_Signatures.ini" ]; then
    echo "WARN: missing ${TEMPLATE_DIR}/_Signatures.ini — SoS won't know which template applies to which user"
else
    if grep -qE '(^|[^#;])[[:space:]]*=[[:space:]]*(true|false|[A-Za-z0-9_-]+)[[:space:]]*$' "${TEMPLATE_DIR}/_Signatures.ini" 2>/dev/null; then
        echo "  NOTE  _Signatures.ini contains 'key = value' lines — SoS expects BARE tokens (DefaultNew, not DefaultNew = X)"
    fi
fi
echo "============================================="

# ---- Create the deploy script ----
cat > /opt/run-deploy.sh << 'SCRIPT_EOF'
#!/bin/bash
echo "========================================"
echo "Deploy run: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"

PWSH_ARGS=(
    "-SignatureTemplatePath" "/opt/templates/signatures"
    "-SignatureIniFile" "/opt/templates/signatures/_Signatures.ini"
    "-GraphConfigFile" "/opt/config/graph-config.ps1"
    "-GraphClientId" "${CLIENT_ID}"
    "-GraphOnly" "true"
    "-UseHtmTemplates" "true"
    "-CloudEnvironment" "AzurePublic"
    "-Verbose"
)

if [ "${DEPLOY_DRY_RUN}" = "true" ]; then
    echo "*** DRY RUN MODE ***"
    IFS=',' read -ra DOMAINS <<< "${MANAGED_DOMAINS}"
    SIM_USER="admin@${DOMAINS[0]}"
    PWSH_ARGS+=("-SimulateUser" "${SIM_USER}" "-SimulateMailboxes" "${SIM_USER}")
fi

pwsh -File "/opt/set-outlooksignatures/Set-OutlookSignatures.ps1" "${PWSH_ARGS[@]}" </dev/null
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "OK Deploy completed successfully"
else
    echo "FAIL Deploy failed with exit code: ${EXIT_CODE}"
fi
echo "========================================"
SCRIPT_EOF
chmod +x /opt/run-deploy.sh

# ---- Export env vars for cron ----
printenv | grep -E '^(TENANT_ID|CLIENT_ID|CLIENT_SECRET|MANAGED_DOMAINS|DEPLOY_|SIG_MARKER)' \
    >> /etc/environment

# ---- Write cron schedule ----
echo "${DEPLOY_CRON} root /opt/run-deploy.sh >> /var/log/sig-deployer/deploy.log 2>&1" \
    > /etc/cron.d/deploy-signatures
echo "" >> /etc/cron.d/deploy-signatures
chmod 0644 /etc/cron.d/deploy-signatures

# ---- Daily log rotation (deploy.log appends forever otherwise) ----
cat > /etc/logrotate.d/kawasig-deployer << 'LOGROT'
/var/log/sig-deployer/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
LOGROT
echo "0 3 * * * root /usr/sbin/logrotate /etc/logrotate.d/kawasig-deployer" \
    > /etc/cron.d/kawasig-logrotate
chmod 0644 /etc/cron.d/kawasig-logrotate

# ---- Run once immediately ----
echo "Running initial deployment..."
/opt/run-deploy.sh 2>&1 | tee /var/log/sig-deployer/deploy.log

# ---- Start cron in foreground ----
echo "Starting cron daemon..."
exec cron -f
