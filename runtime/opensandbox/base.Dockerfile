ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS runtime-builder
ARG BASE_IMAGE
RUN case "$BASE_IMAGE" in sha256:????????????????????????????????????????????????????????????????|*@sha256:????????????????????????????????????????????????????????????????) ;; *) echo 'BASE_IMAGE must be immutable' >&2; exit 1;; esac

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/video-use/helpers \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY wheels /wheels
COPY pyproject.toml /src/pyproject.toml
COPY LICENSE /src/LICENSE
COPY webapp /src/webapp
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-index --find-links=/wheels '/src[web]'

FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN case "$BASE_IMAGE" in sha256:????????????????????????????????????????????????????????????????|*@sha256:????????????????????????????????????????????????????????????????) ;; *) echo 'BASE_IMAGE must be immutable' >&2; exit 1;; esac

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/video-use:/opt/video-use/helpers \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_PATH=/opt/runtime-assets/node_modules \
    NUMBA_CACHE_DIR=/tmp/numba-cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/runtime-assets/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    CHROME_PATH=/usr/local/bin/chromium \
    U2NET_HOME=/opt/runtime-assets/rembg \
    HOME=/state

RUN addgroup --gid 10001 video-use && \
    adduser --uid 10001 --gid 10001 --disabled-password --gecos '' --home /state video-use && \
    install -d -o video-use -g video-use -m 0700 /workspace /state /run/video-use && \
    test -x /usr/bin/ffmpeg && test -x /usr/bin/ffprobe
COPY --from=runtime-builder /opt/venv /opt/venv
COPY runtime/public /opt/video-use
COPY runtime/skills /opt/video-use/skills
COPY runtime/skill_smoke.py /usr/local/bin/sandbox-skill-smoke
COPY runtime/skill_probe.py /opt/video-use/skill_probe.py
COPY runtime/drawio_wrapper.sh /usr/local/bin/drawio
COPY runtime/editppt_wrapper.sh /usr/local/bin/editppt
COPY references /opt/video-use/references
COPY helpers/edit_media.py helpers/edit_plan.py helpers/edit_captions.py /opt/video-use/helpers/
COPY helpers/preview_style.py helpers/image_edit.py helpers/render.py /opt/video-use/helpers/
COPY helpers/grade.py helpers/timeline_view.py helpers/pack_transcripts.py /opt/video-use/helpers/
COPY helpers/canvas_render.py helpers/transcribe.py /opt/video-use/helpers/
COPY runtime/local-assets /opt/runtime-assets
RUN find /opt/video-use -type d -exec chmod 0555 {} + && \
    find /opt/video-use -type f -exec chmod 0444 {} + && \
    find /opt/runtime-assets -type d -exec chmod 0555 {} + && \
    find /opt/runtime-assets -type f -exec chmod 0444 {} + && \
    chmod 0755 /opt/runtime-assets/node_modules/esbuild/bin/esbuild \
      /usr/local/bin/sandbox-skill-smoke \
      /usr/local/bin/drawio /usr/local/bin/editppt \
      /opt/runtime-assets/bin/node \
      /opt/runtime-assets/node_modules/pnpm/pnpm \
      /opt/runtime-assets/node_modules/typescript/bin/tsc \
      /opt/runtime-assets/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell \
      /opt/runtime-assets/ms-playwright/ffmpeg-1011/ffmpeg-linux && \
    ln -s /opt/runtime-assets/node_modules/esbuild/bin/esbuild /usr/local/bin/esbuild && \
    ln -s /opt/runtime-assets/bin/node /usr/local/bin/node && \
    ln -s /opt/runtime-assets/node_modules/pnpm/pnpm /usr/local/bin/pnpm && \
    ln -s /opt/runtime-assets/node_modules/typescript/bin/tsc /usr/local/bin/tsc && \
    ln -s /opt/runtime-assets/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell /usr/local/bin/chromium && \
    test -x /usr/local/bin/node && test -x /usr/local/bin/esbuild && test -x /usr/local/bin/chromium && \
    PYTHONPATH=/opt/video-use/skills/image-to-editable-ppt/cli python /opt/video-use/skill_probe.py

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/opt/venv/bin/python","-m","webapp.runtime_bootstrap"]
CMD ["--config","/run/video-use/runtime.json"]
