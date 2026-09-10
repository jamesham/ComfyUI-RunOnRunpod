"""Local output installation without exposing incomplete downloads."""

import os
import tempfile
from contextlib import closing


class OutputRetrievalError(RuntimeError):
    """Outputs were not fully retrieved; remote copies must be retained."""


def validate_output_path(relative_path: str) -> str:
    """Return a portable relative output path safe for local installation."""
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or ":" in relative_path
        or "\0" in relative_path
        or any(part in ("", ".", "..") for part in relative_path.split("/"))
    ):
        raise OutputRetrievalError("Worker returned an unsafe output path; remote files were kept")
    return relative_path


def local_output_path(root: str, relative_path: str) -> str:
    """Resolve a validated POSIX-style worker path beneath the local root."""
    relative_path = validate_output_path(relative_path)
    root = os.path.realpath(os.path.abspath(root))
    destination = os.path.realpath(os.path.join(root, *relative_path.split("/")))
    if os.path.commonpath((root, destination)) != root:
        raise OutputRetrievalError("Worker output path escapes the local output directory")
    return destination


def download_file(client, bucket: str, key: str, dest: str):
    """Check S3's byte count and atomically install a completed download.

    ContentLength detects truncated transfers, not same-size corruption. Worker
    artifact checksums will be needed for end-to-end integrity verification.
    Existing local files remain untouched if reading or installation fails.
    """
    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    response = client.get_object(Bucket=bucket, Key=key)
    partial = None
    try:
        with closing(response["Body"]) as body:
            expected = response.get("ContentLength")
            if type(expected) is not int or expected < 0:
                raise OutputRetrievalError("Output response has no valid byte count")
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=parent, prefix=".runonrunpod-", suffix=".part",
                delete=False,
            ) as stream:
                partial = stream.name
                written = 0
                for chunk in body.iter_chunks(1024 * 1024):
                    written += len(chunk)
                    if written > expected:
                        raise OutputRetrievalError("Output exceeds its declared byte count")
                    stream.write(chunk)
                if written != expected:
                    raise OutputRetrievalError("Output transfer ended before all bytes arrived")
                stream.flush()
                os.fsync(stream.fileno())
        # Close both the response and local file before publishing (also needed
        # on Windows). A unique partial allows concurrent downloads safely.
        os.replace(partial, dest)
    finally:
        if partial is not None and os.path.exists(partial):
            os.unlink(partial)
