# CPU stager image

This is a CPU-only RunPod Serverless image. It receives coordinator-signed
staging and artifact envelopes, verifies their HMAC and deployment volume
binding, downloads only declared HTTPS model sources, and publishes each file
atomically into the mounted volume. Artifact envelopes also support bounded
HTTPS chunks for local-model fallback, workflow inputs, output retrieval, and
per-job cleanup; these operations do not use RunPod S3.

Required deployment configuration:

- Mount the creative-session network volume at `/runpod-volume` (or set
  `STAGING_VOLUME_DIR`).
- Set `STAGING_VOLUME_BINDING` to the coordinator's immutable binding value.
- Set `STAGING_REQUEST_HMAC_KEY` as a RunPod stored-secret environment mapping
  shared only with the coordinator, for example
  `STAGING_REQUEST_HMAC_KEY={{ RUNPOD_SECRET_cpu_staging_hmac }}`.
- Configure provider credentials such as `HF_TOKEN` and `CIVITAI_API_KEY` as
  RunPod stored-secret environment mappings, never browser settings or job
  payload fields.

Build from repository root:

```sh
docker build -f worker-cpu/Dockerfile -t runonrunpod-cpu-stager .
```

No CPU endpoint is deployed or enabled by default. The local plugin will use
this image only with a coordinator-provided signed request. CPU-managed
artifact transfer requires a managed session so the coordinator can authorize
the endpoint, volume binding, and HMAC key; a static browser-supplied endpoint
configuration fails closed.
