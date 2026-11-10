ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ARG PYTHON_IMAGE
ARG DEBIAN_MIRROR=http://deb.debian.org

RUN case "$PYTHON_IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) echo 'PYTHON_IMAGE must be immutable' >&2; exit 1;; esac && \
    sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg file fonts-wqy-zenhei git jq libcairo2 libgomp1 procps ripgrep socat \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
      libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 \
      libxext6 libxfixes3 libxkbcommon0 libxrandr2 && \
    rm -rf /var/lib/apt/lists/* && \
    test -x /usr/bin/ffmpeg && test -x /usr/bin/ffprobe
