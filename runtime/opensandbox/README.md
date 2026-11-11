# OpenSandbox Agent runtime images

Repository development, safety boundaries and the transition from local-only
evidence to live validation are defined in
[`docs/development/change-opensandbox-runtime.md`](../../docs/development/change-opensandbox-runtime.md).

These images are build inputs, not deployment manifests. Runtime layers are
built offline and must receive immutable `image@sha256:...` bases plus a
reviewed `wheels/` set. The foundation layer is the only networked build: it
installs OS packages from a Python base and must be pinned by digest before it
is accepted by the runtime build.

The repository also has an isolated live deployment. Its current routing uses
the Command Runtime through OpenSandbox/gVisor while CPU Agents remain on the
local bwrap backend. The Codex, Claude and DeepSeek images are real release
artifacts, but their presence does not mean remote Agent execution is enabled.
See the root [`README.md`](../../README.md) for the deployed topology and the
runtime development guide for the evidence required before changing that
routing.

1. Build `foundation.Dockerfile` with an immutable `PYTHON_IMAGE`.
2. Record the local foundation digest, then build `base.Dockerfile` offline with
   that digest as `BASE_IMAGE`.
3. Publish the base output by digest as the CPU Command Runtime and record the same
   digest as `RUNTIME_BASE` for Agent images.
4. Build each Harness image independently with the `codex`, `claude` or
   `deepseek` target. `codex` does not require the SDK wheels.
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

## Local release evidence

The following commands are read-only with respect to OpenSandbox, deployed
services and databases. They inspect local Docker images and write ignored
reports under `runtime/build/`:

```bash
python deploy/runtime_release.py --freeze-bundle
DOCKER=/path/to/docker python deploy/runtime_release.py --max-resident 30
DOCKER=/path/to/docker python deploy/verify_runtime_images.py
python deploy/runtime_security_report.py
```

`release-spec.json` freezes the local image/profile/Harness/Gateway capability
matrix. `runtime/public/bundle.json` freezes the exact public Skill Bundle and
Tool Schema. Runtime bootstrap verifies both digests before it activates the
control endpoint. Gateway routes in a concrete attempt must be a subset of the
profile's model routes plus the task's authorized business capabilities.

The capacity report reserves host CPU, memory and free workspace disk before
evaluating command-only, mixed and Agent-only pools. It is a scheduling model,
not evidence that 30 real sandboxes started successfully.

Publication still requires registry manifest digests, signing, review of raw
SBOM/vulnerability/license evidence, and an isolated live sandbox campaign.
The current Aliyun Debian mirror is not a historical snapshot; the generated
OS package lock records exactly what was built, but a registry publication
must additionally freeze a package snapshot or archive the verified `.deb`
inputs before claiming byte-for-byte reproducibility.

## Isolated deployment validation

Run deployment and live validation through the namespace-pinning wrappers from
the repository root:

```bash
./deploy/sandbox_cluster.sh status
./deploy/sandbox_cluster.sh deploy
./deploy/sandbox_campaign.sh run --cases C06 --jobs 1 --order risk
./deploy/sandbox_opensandbox_recovery.sh
```

Do not invoke `dynamic_cluster.py` directly for this environment: its defaults
belong to the source deployment. The wrappers freeze the `sandbox-agent`
service namespace, dedicated database, Redis/BOSS prefixes, ports, executor
roots and evidence paths.

A successful Command case must record the source and configuration release,
registry manifest digest, Bundle digest, Attempt/Generation, Sandbox ID,
execution location, artifacts and cleanup receipt. Cleanup recovery additionally
requires the Sandbox API to return 404 after deletion. A `targeted_only`
deployment proves only its named retest; full campaign claims require a
matching full-gate release and the complete image matrix required by the
development guide.
