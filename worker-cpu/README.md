# CPU stager image

This is a CPU-only RunPod Serverless image. It receives authenticated staging
and artifact requests, validates their deployment volume binding, downloads
only declared HTTPS model sources, and publishes each file
atomically into the mounted volume. Artifact requests also support bounded
HTTPS chunks for local-model fallback, workflow inputs, output retrieval, and
per-job cleanup; these operations do not use RunPod S3.

Required deployment configuration:

- Mount the creative-session network volume at `/runpod-volume` (or set
  `STAGING_VOLUME_DIR`).
- Set `STAGING_VOLUME_BINDING` to the coordinator's immutable binding value.
- Configure provider credentials such as `HF_TOKEN` and `CIVITAI_API_KEY` as
  RunPod stored-secret environment mappings, never browser settings or job
  payload fields.

Build from repository root:

```sh
docker build -f worker-cpu/Dockerfile -t runonrunpod-cpu-stager .
```

No CPU endpoint is deployed or enabled by default. The local plugin will use
this image only with a coordinator-provided request. CPU-managed artifact
transfer requires a managed session so the coordinator can authorize the
endpoint and volume binding; a static browser-supplied endpoint
configuration fails closed.
