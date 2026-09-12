# RunPod data-center availability utility

`python -m coordinator.datacenter_availability` performs a read-only, live
RunPod catalog check. It identifies S3-capable data centers that also advertise
a Standard-performance network volume, a requested CPU flavor, and one or more
available GPUs.

The command requires a RunPod API key in `RUNPOD_API_KEY`. It never accepts an
API key as an argument, persists it, or includes it in command output.

```sh
export RUNPOD_API_KEY='...'

python -m coordinator.datacenter_availability \
  --region US \
  --cpu-flavor cpu3c \
  --gpu 'NVIDIA GeForce RTX 4090' \
  --gpu 'NVIDIA L40S' \
  --cheapest-gpus 3
```

`--gpu` is optional. When omitted, the command lists the cheapest available GPU
types without a type filter. `--cpu-flavor` defaults to `cpu3c`, and
`--cheapest-gpus` defaults to `1`. Repeat `--region` or `--datacenter` to narrow
the search. `--json` selects machine-readable output; human-readable output is
the default.

Pass `--debug-http` to write each catalog request and complete response
(including response headers and body) to standard error. Request credential
headers are redacted, including `RUNPOD_API_KEY`; normal results remain on
standard output. Catalog requests explicitly identify this utility with the
`ComfyUI-RunOnRunpod-DatacenterAvailability/0.3.1` User-Agent and include both
`CPU_AVAILABILITY` and `GPU_AVAILABILITY`, since RunPod otherwise omits those
availability sections from a data-center response.

The S3 filter uses RunPod's published [S3-compatible API data-center
table](https://docs.runpod.io/storage/s3-api) as the authoritative source. The
remaining capabilities come from live
`GET /v2/catalog/datacenters/{id}` responses. A listed resource is a current
catalog observation, not an allocation guarantee; only successful endpoint
provisioning proves capacity can be assigned.
