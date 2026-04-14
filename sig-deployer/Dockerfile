FROM mcr.microsoft.com/powershell:7.4-ubuntu-22.04

LABEL maintainer="KawaConnect <dean@kawaconnect.com>"
LABEL description="Set-OutlookSignatures deployer — pushes roaming signatures via Graph API"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        cron \
        curl \
        ca-certificates \
        jq \
        logrotate \
    && rm -rf /var/lib/apt/lists/*

# Install PowerShell modules needed by Set-OutlookSignatures
RUN pwsh -Command " \
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
    Install-Module -Name MSAL.PS -Scope AllUsers -Force; \
    Install-Module -Name Microsoft.Graph.Authentication -Scope AllUsers -Force \
"

RUN mkdir -p /opt/set-outlooksignatures \
             /opt/templates \
             /opt/config \
             /var/log/sig-deployer

# Build context is repo root (see docker-compose.yml).
COPY src_Set-OutlookSignatures/        /opt/set-outlooksignatures/
COPY sig-deployer/config/graph-config.ps1 /opt/config/graph-config.ps1
COPY sig-deployer/entrypoint.sh        /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
