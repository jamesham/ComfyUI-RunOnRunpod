#!/usr/bin/env python3
"""Generate a high-entropy secret for CPU staging request HMACs.

Uses only Python's standard library and works wherever this plugin's Python
runtime works, including Windows embedded Python distributions.
"""

from __future__ import annotations

import secrets


def generate_key() -> str:
    """Return a URL-safe 384-bit random secret suitable for the HMAC setting."""
    return secrets.token_urlsafe(48)


def main() -> int:
    print(generate_key())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
