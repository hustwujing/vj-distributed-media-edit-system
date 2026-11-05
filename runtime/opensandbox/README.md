# OpenSandbox Agent runtime images

These images are build inputs, not deployment manifests. Builds are offline and
must receive immutable `image@sha256:...` bases plus a reviewed `wheels/` set.

1. Build `base.Dockerfile` with `BASE_IMAGE`.
2. Publish that output by digest as the CPU Command Runtime and record the same
   digest as `RUNTIME_BASE` for Agent images.
3. Build each Harness image independently.
4. Sign, scan and publish by digest; floating tags are not runtime inputs.

The image contains no Provider, database, object-storage or Gateway credential.
Attempt configuration and short-lived tokens are injected under
`/run/video-use` after sandbox creation.
