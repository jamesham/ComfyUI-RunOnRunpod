# CPU stager image

This is a CPU-only RunPod Serverless image. It receives a coordinator-signed
staging envelope, verifies its HMAC and deployment volume binding, downloads
only declared HTTPS model sources, verifies SHA-256 and byte size, and publishes
each file atomically into the mounted volume.

Required deployment configuration:

- Mount the creative-session network volume at `/runpod-volume` (or set
  `STAGING_VOLUME_DIR`).
- Set `STAGING_VOLUME_BINDING` to the coordinator's immutable binding value.
- Set `STAGING_REQUEST_HMAC_KEY` as a secret shared only with the coordinator.
- Configure provider credentials such as `HF_TOKEN` and `CIVITAI_API_KEY` as
  CPU deployment secrets, never browser settings.

Build from repository root:

```sh
docker build -f worker-cpu/Dockerfile -t runonrunpod-cpu-stager .
```

No CPU endpoint is deployed or enabled by default. The local plugin will use
this image only with a coordinator-provided signed request.
