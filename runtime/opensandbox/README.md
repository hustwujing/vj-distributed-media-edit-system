# OpenSandbox Agent runtime images

These images are build inputs, not deployment manifests. Runtime layers are
built offline and must receive immutable `image@sha256:...` bases plus a
reviewed `wheels/` set. The foundation layer is the only networked build: it
installs OS packages from a Python base and must be pinned by digest before it
is accepted by the runtime build.

1. Build `foundation.Dockerfile` with an immutable `PYTHON_IMAGE`.
2. Record the local foundation digest, then build `base.Dockerfile` offline with
   that digest as `BASE_IMAGE`.
3. Publish the base output by digest as the CPU Command Runtime and record the same
   digest as `RUNTIME_BASE` for Agent images.
4. Build each Harness image independently. `codex` can be built alone without
   preparing the Claude and DeepSeek SDK wheels.
5. Sign, scan and publish by digest; floating tags are not runtime inputs.

`deploy/build-agent-runtime.sh` accepts `DOCKER=/path/to/docker` for hosts where
the Docker CLI is not on `PATH`. Local build inputs under `wheels/` and `vendor/`
are deliberately excluded from Git.

`DEBIAN_MIRROR` may point the foundation build at a reachable package mirror;
it changes only the image build and never rewrites the host package sources.

Local chained builds may use Docker's immutable `sha256:<image-id>`. Published
profiles must use the registry's `image@sha256:<manifest>` reference because a
local image ID is not portable across nodes.

The image contains no Provider, database, object-storage or Gateway credential.
Attempt configuration and short-lived tokens are injected under
`/run/video-use` after sandbox creation.
