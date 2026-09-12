"""Read-only RunPod data-center capability and GPU-price selector.

Run with ``python -m coordinator.datacenter_availability``.  The command never
creates provider resources and reads ``RUNPOD_API_KEY`` only from its process
environment.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import sys
from typing import Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


CATALOG_BASE = "https://api.runpod.io/v2/catalog/datacenters"

# RunPod's published S3-compatible API availability table is the authoritative
# source for this filter until RunPod provides a capability field in its API.
# https://docs.runpod.io/storage/s3-api
S3_ENDPOINTS = {
    "EU-CZ-1": "https://s3api-eu-cz-1.runpod.io/",
    "EU-RO-1": "https://s3api-eu-ro-1.runpod.io/",
    "EUR-IS-1": "https://s3api-eur-is-1.runpod.io/",
    "EUR-NO-1": "https://s3api-eur-no-1.runpod.io/",
    "US-CA-2": "https://s3api-us-ca-2.runpod.io/",
    "US-GA-2": "https://s3api-us-ga-2.runpod.io/",
    "US-IL-1": "https://s3api-us-il-1.runpod.io/",
    "US-KS-2": "https://s3api-us-ks-2.runpod.io/",
    "US-MD-1": "https://s3api-us-md-1.runpod.io/",
    "US-MO-1": "https://s3api-us-mo-1.runpod.io/",
    "US-MO-2": "https://s3api-us-mo-2.runpod.io/",
    "US-NC-1": "https://s3api-us-nc-1.runpod.io/",
    "US-NC-2": "https://s3api-us-nc-2.runpod.io/",
    "US-NE-1": "https://s3api-us-ne-1.runpod.io/",
    "US-WA-1": "https://s3api-us-wa-1.runpod.io/",
}


class AvailabilityError(RuntimeError):
    """A safe-to-display catalog query or schema error."""


@dataclass(frozen=True)
class GpuOption:
    name: str
    price: float | None
    price_field: str | None
    availability: str
    preference_rank: int | None


@dataclass(frozen=True)
class DataCenterResult:
    data_center_id: str
    s3_endpoint: str
    cpu_flavors: tuple[str, ...]
    gpus: tuple[GpuOption, ...]


@dataclass(frozen=True)
class RejectedDataCenter:
    data_center_id: str
    reasons: tuple[str, ...]


Fetcher = Callable[[str, str], Mapping[str, object]]
DebugWriter = Callable[[str], None]


def _positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List RunPod data centers with Standard volumes, S3, CPU, and available GPUs.",
    )
    parser.add_argument(
        "--cpu-flavor", action="append", default=[], metavar="NAME",
        help="acceptable CPU flavor; repeat for fallbacks (default: cpu3c)",
    )
    parser.add_argument(
        "--gpu", action="append", default=[], metavar="NAME",
        help="optional acceptable GPU type; repeat in preference order",
    )
    parser.add_argument(
        "--cheapest-gpus", type=_positive_integer, default=1, metavar="N",
        help="cheapest available GPU options to show per data center (default: 1)",
    )
    parser.add_argument(
        "--region", action="append", default=[], metavar="PREFIX",
        help="limit to a region prefix such as US, EU, or AP; repeatable",
    )
    parser.add_argument(
        "--datacenter", action="append", default=[], metavar="ID",
        help="limit to an exact S3-capable RunPod data-center ID; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--debug-http", action="store_true",
        help="write catalog HTTP requests and responses to stderr (credentials are redacted)",
    )
    return parser.parse_args(argv)


def _redacted_header(name: str, value: str) -> str:
    if name.casefold() in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
        if name.casefold() == "authorization" and value.casefold().startswith("bearer "):
            return "Bearer <redacted>"
        return "<redacted>"
    return value


def _debug_request(debug: DebugWriter | None, request: Request) -> None:
    if debug is None:
        return
    debug(f">>> {request.get_method()} {request.full_url}")
    for name, value in request.header_items():
        debug(f">>> {name}: {_redacted_header(name, value)}")
    debug(">>>")


def _debug_response(
    debug: DebugWriter | None,
    status: int,
    headers: object,
    body: bytes,
) -> None:
    if debug is None:
        return
    debug(f"<<< HTTP {status}")
    items = headers.items() if hasattr(headers, "items") else ()
    for name, value in items:
        debug(f"<<< {name}: {_redacted_header(str(name), str(value))}")
    debug("<<<")
    debug(body.decode("utf-8", errors="replace"))


def _fetch_catalog(
    api_key: str,
    data_center_id: str,
    *,
    debug: DebugWriter | None = None,
    opener=urlopen,
) -> Mapping[str, object]:
    request = Request(
        f"{CATALOG_BASE}/{quote(data_center_id, safe='-')}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    _debug_request(debug, request)
    try:
        with opener(request, timeout=20) as response:
            body = response.read()
            _debug_response(debug, response.status, response.headers, body)
            value = json.loads(body.decode("utf-8"))
    except HTTPError as error:
        _debug_response(debug, error.code, error.headers, error.read())
        raise AvailabilityError(f"RunPod catalog request for {data_center_id} failed with HTTP {error.code}") from None
    except URLError as error:
        if debug is not None:
            debug(f"<<< transport error: {error.reason}")
        raise AvailabilityError(f"RunPod catalog request for {data_center_id} failed: {error.reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AvailabilityError(f"RunPod catalog response for {data_center_id} was not valid JSON") from error
    if not isinstance(value, Mapping):
        raise AvailabilityError(f"RunPod catalog response for {data_center_id} was not an object")
    return value


def _catalog_items(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, Mapping):
                result.append(item)
            elif isinstance(item, str) and item:
                result.append({"id": item})
        return result
    if isinstance(value, Mapping):
        result = []
        for name, item in value.items():
            if isinstance(item, Mapping):
                result.append({"id": name, **item})
            elif isinstance(item, (bool, int, float, str)):
                result.append({"id": name, "available": item})
        return result
    return []


def _item_name(item: Mapping[str, object]) -> str | None:
    for field in ("id", "type", "name", "displayName", "gpuTypeId", "gpuId", "cpuFlavorId"):
        value = item.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _availability(item: Mapping[str, object]) -> tuple[bool, str]:
    """Interpret common catalog availability forms without treating false as true."""
    for field in ("available", "isAvailable", "enabled", "supported"):
        value = item.get(field)
        if isinstance(value, bool):
            return value, "available" if value else "unavailable"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value > 0, "available" if value > 0 else "unavailable"
    for field in ("availableCount", "gpuAvailable", "count"):
        value = item.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value > 0, "available" if value > 0 else "unavailable"
    for field in ("stockStatus", "availability", "status"):
        value = item.get(field)
        if isinstance(value, str):
            normalized = value.casefold().replace("_", "-").replace(" ", "-")
            if normalized in {"available", "in-stock", "high", "medium", "low", "ready"}:
                return True, value
            if normalized in {"unavailable", "out-of-stock", "none", "disabled", "unavailable"}:
                return False, value
    # Catalog membership is capability evidence when no separate status exists.
    return True, "listed"


def _has_standard_volume(catalog: Mapping[str, object]) -> bool:
    for item in _catalog_items(catalog.get("networkVolumeTypes")):
        name = _item_name(item)
        available, _ = _availability(item)
        if name and name.casefold().replace("_", "-").replace(" ", "-") in {
            "standard", "standard-performance",
        } and available:
            return True
    return False


def _catalog_collection(catalog: Mapping[str, object], names: Iterable[str]) -> list[Mapping[str, object]]:
    for name in names:
        items = _catalog_items(catalog.get(name))
        if items:
            return items
    return []


def _matching_cpu_flavors(catalog: Mapping[str, object], requested: Sequence[str]) -> tuple[str, ...]:
    entries = _catalog_collection(catalog, ("cpuFlavors", "cpuTypes", "cpuAvailability"))
    matches = []
    requested_names = {name.casefold(): name for name in requested}
    for item in entries:
        name = _item_name(item)
        available, _ = _availability(item)
        if name and available and name.casefold() in requested_names:
            matches.append(requested_names[name.casefold()])
    return tuple(matches)


def _price(item: Mapping[str, object]) -> tuple[float | None, str | None]:
    """Return a comparable catalog price without assigning an unverified unit."""
    for field in (
        "activeCostPerSecond", "pricePerSecond", "costPerSecond", "price", "securePrice",
    ):
        value = item.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value), field
    return None, None


def _matching_gpus(
    catalog: Mapping[str, object], requested: Sequence[str], count: int,
) -> tuple[GpuOption, ...]:
    entries = _catalog_collection(catalog, ("gpuTypes", "gpus", "gpuAvailability"))
    preference = {name.casefold(): index for index, name in enumerate(requested)}
    options = []
    for item in entries:
        name = _item_name(item)
        available, status = _availability(item)
        if not name or not available or (preference and name.casefold() not in preference):
            continue
        price, price_field = _price(item)
        options.append(GpuOption(name, price, price_field, status, preference.get(name.casefold())))
    options.sort(key=lambda option: (
        option.price is None,
        option.price if option.price is not None else float("inf"),
        option.preference_rank if option.preference_rank is not None else float("inf"),
        option.name.casefold(),
    ))
    return tuple(options[:count])


def _matches_region(data_center_id: str, filters: Sequence[str]) -> bool:
    if not filters:
        return True
    normalized = data_center_id.upper()
    for value in filters:
        prefix = value.upper().rstrip("-")
        if normalized.startswith(f"{prefix}-"):
            return True
        if prefix == "EU" and normalized.startswith("EUR-"):
            return True
    return False


def select_data_centers(
    fetcher: Fetcher,
    *,
    cpu_flavors: Sequence[str],
    gpu_preferences: Sequence[str],
    cheapest_gpus: int,
    regions: Sequence[str] = (),
    data_centers: Sequence[str] = (),
) -> tuple[list[DataCenterResult], list[RejectedDataCenter]]:
    """Return live, S3-capable data centers that meet the requested filters."""
    if cheapest_gpus < 1:
        raise ValueError("cheapest_gpus must be positive")
    requested_ids = tuple(item.upper() for item in data_centers) or tuple(S3_ENDPOINTS)
    results: list[DataCenterResult] = []
    rejected: list[RejectedDataCenter] = []
    for data_center_id in requested_ids:
        reasons = []
        s3_endpoint = S3_ENDPOINTS.get(data_center_id)
        if s3_endpoint is None:
            rejected.append(RejectedDataCenter(data_center_id, ("not listed in the authoritative S3 table",)))
            continue
        if not _matches_region(data_center_id, regions):
            continue
        try:
            catalog = fetcher(data_center_id, s3_endpoint)
        except AvailabilityError as error:
            rejected.append(RejectedDataCenter(data_center_id, (str(error),)))
            continue
        if not _has_standard_volume(catalog):
            reasons.append("Standard-performance network volume is unavailable")
        cpu_matches = _matching_cpu_flavors(catalog, cpu_flavors)
        if not cpu_matches:
            reasons.append(f"no requested CPU flavor available ({', '.join(cpu_flavors)})")
        gpus = _matching_gpus(catalog, gpu_preferences, cheapest_gpus)
        if not gpus:
            reasons.append("no matching GPU is currently available")
        if reasons:
            rejected.append(RejectedDataCenter(data_center_id, tuple(reasons)))
        else:
            results.append(DataCenterResult(data_center_id, s3_endpoint, cpu_matches, gpus))
    return results, rejected


def _json_value(
    checked_at: str,
    results: Sequence[DataCenterResult],
    rejected: Sequence[RejectedDataCenter],
) -> str:
    return json.dumps({
        "checked_at": checked_at,
        "data_centers": [asdict(result) for result in results],
        "rejected": [asdict(result) for result in rejected],
    }, indent=2, sort_keys=True)


def _human_value(
    checked_at: str,
    results: Sequence[DataCenterResult],
    rejected: Sequence[RejectedDataCenter],
) -> str:
    lines = [f"Checked: {checked_at}", ""]
    for result in results:
        lines.extend((
            result.data_center_id,
            f"  S3 endpoint: {result.s3_endpoint}",
            f"  CPU flavors: {', '.join(result.cpu_flavors)}",
            "  Cheapest available GPUs:",
        ))
        for index, gpu in enumerate(result.gpus, start=1):
            price = "price unavailable" if gpu.price is None else f"{gpu.price:g} ({gpu.price_field})"
            lines.append(f"    {index}. {gpu.name}: {price}; {gpu.availability}")
    if rejected:
        if lines:
            lines.append("")
        lines.append("Rejected data centers:")
        lines.extend(f"  {item.data_center_id}: {'; '.join(item.reasons)}" for item in rejected)
    if not results and not rejected:
        lines.append("No data centers matched the requested filters.")
    return "\n".join(lines)


def run(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    fetcher: Fetcher | None = None,
    stdout=None,
    stderr=None,
) -> int:
    """Run the command, with injectable boundaries for hermetic tests."""
    arguments = _arguments(argv)
    environment = os.environ if environ is None else environ
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    api_key = environment.get("RUNPOD_API_KEY")
    if not api_key:
        print("RUNPOD_API_KEY is required", file=errors)
        return 2
    cpu_flavors = tuple(arguments.cpu_flavor or ("cpu3c",))
    debug = (lambda message: print(message, file=errors)) if arguments.debug_http else None
    actual_fetcher = fetcher or (
        lambda data_center_id, _s3: _fetch_catalog(api_key, data_center_id, debug=debug)
    )
    results, rejected = select_data_centers(
        actual_fetcher,
        cpu_flavors=cpu_flavors,
        gpu_preferences=tuple(arguments.gpu),
        cheapest_gpus=arguments.cheapest_gpus,
        regions=tuple(arguments.region),
        data_centers=tuple(arguments.datacenter),
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    value = _json_value(checked_at, results, rejected) if arguments.json else _human_value(checked_at, results, rejected)
    print(value, file=output)
    return 0 if results else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
