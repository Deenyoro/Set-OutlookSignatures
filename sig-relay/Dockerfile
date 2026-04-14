# Pull via Google's Docker Hub mirror to avoid anonymous pull rate-limits on
# shared-IP runners. mirror.gcr.io/library/X is bit-identical to docker.io/library/X.
FROM mirror.gcr.io/library/ubuntu:22.04

LABEL maintainer="KawaConnect <dean@kawaconnect.com>"
LABEL description="KawaSig relay — Postfix + Python milter for server-side signature injection"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postfix \
        postfix-pcre \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        gcc \
        libmilter-dev \
        libmilter1.0.1 \
        ca-certificates \
        curl \
        openssl \
        supervisor \
        netcat-openbsd \
        logrotate \
    && rm -rf /var/lib/apt/lists/*

# Python venv for the milter (build context is repo root)
RUN python3 -m venv /opt/milter-venv
COPY sig-relay/milter/requirements.txt /opt/milter/requirements.txt
RUN /opt/milter-venv/bin/pip install --no-cache-dir -r /opt/milter/requirements.txt

COPY sig-relay/milter/         /opt/milter/
COPY sig-relay/entrypoint.sh   /entrypoint.sh
COPY sig-relay/supervisord.conf /etc/supervisor/conf.d/sig-relay.conf
RUN chmod +x /entrypoint.sh

# Directories
RUN mkdir -p /var/log/sig-relay /var/spool/postfix/milter

EXPOSE 25

ENTRYPOINT ["/entrypoint.sh"]
