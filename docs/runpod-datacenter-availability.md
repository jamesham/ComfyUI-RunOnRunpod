# RunPod data-center availability utility

`python -m coordinator.datacenter_availability` performs a read-only, live
RunPod catalog check. By default it identifies S3-capable data centers that
also advertise a Standard-performance network volume, any available CPU flavor
(or a requested one), and one or more available GPUs. It performs an additional
live GPU-catalog lookup for each GPU type so pricing and serverless-pool IDs are
current.

The command requires a RunPod API key in `RUNPOD_API_KEY`. It never accepts an
API key as an argument, persists it, or includes it in command output.

```sh
export RUNPOD_API_KEY='...'

python -m coordinator.datacenter_availability \
  --region US \
  --cpu-flavor cpu3c \
  --gpu 'NVIDIA GeForce RTX 4090' \
  --gpu 'NVIDIA L40S' \
  --price-type serverless \
  --cheapest-gpus 3
```

`--gpu` is optional. When omitted, the command lists the cheapest available GPU
types without a type filter. Omitting `--cpu-flavor` accepts any available CPU
flavor; repeat it to constrain the result to acceptable CPU flavors. The
`--price-type` selector determines both price reporting and ranking, and accepts
`serverless` (the default), `secure`, or `community`. Each GPU result includes
its serverless pool ID, or `none` when it has no serverless pool.
Prices are RunPod catalog list prices in USD per GPU-hour; they are associated
with the GPU type or serverless pool, not individually negotiated allocation
prices for a particular data center.
`--cheapest-gpus` defaults to `1`. Repeat `--region` or `--datacenter` to narrow
the search. `--json` selects machine-readable output; human-readable output is
the default.

`--s3-required` accepts `true` (the default) or `false`. With `true`, a data
center must appear in RunPod's S3-compatible API table. With `false`, the
utility discovers candidates from the live RunPod catalog instead, so S3 is not
a selection criterion and a selected data center may or may not support it.
Results without S3 report a null endpoint in JSON and `unavailable` in
human-readable output.

Pass `--debug-http` to write each catalog request and complete response
(including response headers and body) to standard error. Request credential
headers are redacted, including `RUNPOD_API_KEY`; normal results remain on
standard output. Catalog requests explicitly identify this utility with the
`ComfyUI-RunOnRunpod-DatacenterAvailability/0.3.1` User-Agent and include both
`CPU_AVAILABILITY` and `GPU_AVAILABILITY`, since RunPod otherwise omits those
availability sections from a data-center response. GPU price and pool lookups
use `GET /v2/catalog/gpus/{id}`.

The S3 filter uses RunPod's published [S3-compatible API data-center
table](https://docs.runpod.io/storage/s3-api) as the authoritative source. The
remaining capabilities come from live
`GET /v2/catalog/datacenters/{id}` responses. A listed resource is a current
catalog observation, not an allocation guarantee; only successful endpoint
provisioning proves capacity can be assigned.
