# CPU staging HMAC key

CPU-managed staging uses one shared secret to prove that a staging or artifact
request came from your ComfyUI plugin rather than an arbitrary caller. Generate
one key, then place the exact same value in both locations below. Treat it like
a password: do not commit it, paste it into workflow JSON, or share it.

## Generate the key

This repository includes a standard-library Python generator, so no
platform-specific crypto utility is required:

- Windows with a normal Python installation: `py tools\generate_hmac_key.py`
- Windows with ComfyUI embedded Python: `python_embeded\python.exe tools\generate_hmac_key.py`
- macOS or Linux: `python3 tools/generate_hmac_key.py`

The command prints a new URL-safe, 384-bit random value. Copy it before closing
the terminal; the script deliberately does not write it to disk.

## Configure RunPod

In the RunPod web UI, create a stored secret such as `cpu-stager-hmac` with the
generated value. In the CPU section of the managed-profile JSON in the plugin,
map that secret to the worker's required environment-variable name:

```json
"environment": {
  "STAGING_REQUEST_HMAC_KEY": "{{ RUNPOD_SECRET_cpu-stager-hmac }}"
}
```

The profile contains the stored-secret reference, not the HMAC value. Keep
provider tokens in separate RunPod stored secrets and reference them the same
way, for example `HF_TOKEN` or `CIVITAI_API_KEY`.

## Configure the plugin

Open **Settings → Run on Runpod** and paste the generated value into **CPU
staging HMAC key**. It is sent only with the current request to the ComfyUI
backend so it can sign CPU worker jobs; it is not written to coordinator recipe
or session records. Your browser's settings store may retain the setting, so
protect the local ComfyUI user account accordingly.

If ComfyUI is remote, use HTTPS. The browser must send this key, the RunPod API
key, and any GPU-only credentials to the ComfyUI server, and a plain HTTP
connection can expose them in transit.
