# Live RunPod CPU stager smoke test

`integration/runpod_cpu_stager_smoke.py` is an explicitly invoked, billable
integration harness. It is not part of ordinary test discovery and refuses to
run without `--live`.

It uses the project coordinator, lifecycle service, RunPod lifecycle adapter,
signed CPU-staging contract, CPU client, and CPU worker protocol to:

1. create a uniquely named temporary network volume;
2. create one CPU Serverless endpoint attached to that exact volume;
3. configure the CPU endpoint with the required secret references and dynamic
   `STAGING_VOLUME_BINDING` environment variable;
4. submit exactly one signed, identity-pinned download;
5. validate the CPU result through the shared contract and record it; and
6. delete the exact recorded CPU endpoint and volume, confirming absence.

It never prints API keys, HMAC values, provider-token values, or endpoint
environment values. It does print the temporary session, volume, and endpoint
IDs so the operator can inspect them.

This harness exercises the CPU staging data plane without using RunPod S3. It
does not yet prove that a full CPU-managed creative session is S3-free: normal
plugin input transfer, local-model fallback, readiness records, output
retrieval, and cleanup still use the legacy S3 path. The architecture requires
those CPU-mode dependencies to be removed before CPU-managed staging is
considered complete; that future change must leave GPU-only S3 behavior
unchanged.

## Preconditions

- A RunPod API key with authority to create/delete a network volume and CPU
  Serverless endpoint is available in a local environment variable. Do not put
  it on the command line.
- Run the harness in the same Python environment as ComfyUI, or install its
  `aiohttp` transport dependency before invoking `--live`.
- A CPU Serverless template already points to an image built from
  [`worker-cpu/Dockerfile`](../worker-cpu/Dockerfile).
- In the RunPod administrative UI, create two stored secrets:
  - an HMAC secret whose value also exists locally in the environment variable
    named by `--signing-key-env`;
  - a valid Hugging Face or CivitAI token, selected by `--provider`.
- The template/endpoint configuration accepts stored-secret references in the
  Serverless `env` mapping. This harness uses the project assumption that the
  published reference syntax is consistent with Pods:

  ```text
  STAGING_REQUEST_HMAC_KEY={{ RUNPOD_SECRET_<hmac-secret-name> }}
  HF_TOKEN={{ RUNPOD_SECRET_<provider-secret-name> }}
  ```

  For `--provider civitai`, the second name is `CIVITAI_API_KEY` instead.
- Supply an HTTPS model URL, exact SHA-256, and byte size. Use an object you are
  authorized to download. The selected provider token is used by the worker,
  not sent by this harness in the job request.

## Invocation

Run from the repository root. Replace every placeholder with values for the
user's account and CPU staging template:

```sh
export RUNPOD_API_KEY='...'
export RUNONRUNPOD_CPU_STAGING_SIGNING_KEY='...'

python -m integration.runpod_cpu_stager_smoke \
  --live \
  --data-center '<RUNPOD_DATA_CENTER>' \
  --cpu-template-id '<CPU_STAGER_TEMPLATE_ID>' \
  --cpu-image 'registry.example/runonrunpod-cpu@sha256:<IMAGE_DIGEST>' \
  --hmac-secret-name '<RUNPOD_HMAC_SECRET_NAME>' \
  --provider hf \
  --provider-secret-name '<RUNPOD_HF_SECRET_NAME>' \
  --download-url 'https://huggingface.co/.../resolve/<COMMIT>/<FILE>' \
  --sha256 '<64_HEX_SHA256>' \
  --size '<BYTE_SIZE>'
```

Add `--pause-after-create` to stop immediately after the CPU endpoint and
volume are created. Inspect their compute type, min/max worker counts, shared
volume attachment, and environment-variable *names* in RunPod. Press Enter to
resume the single download and cleanup. Without the flag, the harness proceeds
non-interactively.

## Cleanup and failure behavior

Cleanup runs in `finally` after a successfully recorded session, including
after download failure or Ctrl-C. It deletes the CPU endpoint before the volume
and confirms each is absent through the project lifecycle adapter. If cleanup
fails, the script prints the session ID and returns nonzero; use the durable
state root supplied with `--state-root` to inspect/retry only the recorded
resources. A provider error during an uncertain create is intentionally not
guessed or deleted by name.

The harness makes real network calls only when explicitly invoked with `--live`.
