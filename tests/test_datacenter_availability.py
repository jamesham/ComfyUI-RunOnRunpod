"""Hermetic coverage for the live RunPod data-center availability utility."""

import contextlib
import io
import unittest

from coordinator.datacenter_availability import (
    CATALOG_INCLUDES,
    S3_ENDPOINTS,
    USER_AGENT,
    _fetch_catalog,
    _fetch_data_center_ids,
    _fetch_gpu_catalog,
    run,
    select_data_centers,
)


class FakeCatalogResponse:
    status = 200
    headers = {"Content-Type": "application/json", "X-Request-ID": "request-1"}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class DataCenterAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.catalogs = {
            "US-CA-2": {
                "networkVolumeTypes": [{"id": "STANDARD", "available": True}],
                "cpuFlavors": [{"id": "cpu3c", "available": True}],
                "gpuTypes": [
                    {"id": "NVIDIA L40S", "available": True, "price": 0.53},
                    {"id": "NVIDIA GeForce RTX 4090", "available": True, "price": 0.31},
                    {"id": "NVIDIA A40", "available": False, "price": 0.20},
                ],
            },
            "EU-CZ-1": {
                "networkVolumeTypes": [{"id": "STANDARD", "available": True}],
                "cpuFlavors": [{"id": "cpu5c", "available": True}],
                "gpuTypes": [{"id": "NVIDIA GeForce RTX 4090", "available": True, "price": 0.40}],
            },
        }
        self.gpu_catalogs = {
            "NVIDIA L40S": {
                "price": {"secure": 0.72, "community": 0.53, "serverless": 1.19},
                "pool": "ADA_48",
            },
            "NVIDIA GeForce RTX 4090": {
                "price": {"secure": 0.44, "community": 0.31, "serverless": 1.10},
                "pool": "ADA_24",
            },
        }

    def fetch(self, data_center_id, _s3_endpoint):
        return self.catalogs[data_center_id]

    def fetch_gpu(self, gpu_type_id):
        return self.gpu_catalogs[gpu_type_id]

    def test_selects_n_cheapest_gpus_when_no_gpu_filter_is_given(self):
        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=(), cheapest_gpus=2,
            data_centers=("US-CA-2",), gpu_catalog_fetcher=self.fetch_gpu,
        )
        self.assertEqual(rejected, [])
        self.assertEqual([gpu.name for gpu in results[0].gpus], [
            "NVIDIA GeForce RTX 4090", "NVIDIA L40S",
        ])

    def test_filters_by_region_cpu_and_gpu_preferences(self):
        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=("NVIDIA L40S",),
            cheapest_gpus=1, regions=("US",), data_centers=("US-CA-2", "EU-CZ-1"),
            gpu_catalog_fetcher=self.fetch_gpu,
        )
        self.assertEqual([item.data_center_id for item in results], ["US-CA-2"])
        self.assertEqual(rejected, [])

    def test_rejects_unknown_s3_data_center_and_missing_capabilities(self):
        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=(), cheapest_gpus=1,
            data_centers=("EU-CZ-1", "NOT-A-DC"), gpu_catalog_fetcher=self.fetch_gpu,
        )
        self.assertEqual(results, [])
        self.assertEqual([item.data_center_id for item in rejected], ["EU-CZ-1", "NOT-A-DC"])
        self.assertIn("CPU flavor", rejected[0].reasons[0])
        self.assertIn("authoritative S3 table", rejected[1].reasons[0])

    def test_can_select_a_non_s3_data_center_when_s3_is_not_required(self):
        self.catalogs["US-GA-1"] = {
            "networkVolumeTypes": [{"id": "STANDARD", "available": True}],
            "cpuFlavors": [{"id": "cpu3c", "available": True}],
            "gpuTypes": [{"id": "NVIDIA GeForce RTX 4090", "available": True}],
        }
        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=(), cheapest_gpus=1,
            data_centers=("US-GA-1",), gpu_catalog_fetcher=self.fetch_gpu,
            s3_required=False,
        )
        self.assertEqual(rejected, [])
        self.assertEqual([item.data_center_id for item in results], ["US-GA-1"])
        self.assertIsNone(results[0].s3_endpoint)

    def test_cli_accepts_false_for_s3_required_and_marks_s3_unavailable(self):
        self.catalogs["US-GA-1"] = {
            "networkVolumeTypes": [{"id": "STANDARD", "available": True}],
            "cpuFlavors": [{"id": "cpu3c", "available": True}],
            "gpuTypes": [{"id": "NVIDIA GeForce RTX 4090", "available": True}],
        }
        stdout = io.StringIO()
        result = run(
            ["--datacenter", "US-GA-1", "--s3-required", "false"],
            environ={"RUNPOD_API_KEY": "test"}, fetcher=self.fetch,
            gpu_fetcher=self.fetch_gpu, stdout=stdout,
        )
        self.assertEqual(result, 0)
        self.assertIn("S3 endpoint: unavailable", stdout.getvalue())

    def test_cli_discovers_all_data_centers_when_s3_is_not_required(self):
        self.catalogs["US-GA-1"] = {
            "networkVolumeTypes": [{"id": "STANDARD", "available": True}],
            "cpuFlavors": [{"id": "cpu3c", "available": True}],
            "gpuTypes": [{"id": "NVIDIA GeForce RTX 4090", "available": True}],
        }
        stdout = io.StringIO()
        discovered = []
        result = run(
            ["--s3-required", "false"], environ={"RUNPOD_API_KEY": "test"},
            fetcher=self.fetch, gpu_fetcher=self.fetch_gpu,
            data_center_fetcher=lambda: discovered.append(True) or ("US-GA-1",),
            stdout=stdout,
        )
        self.assertEqual(result, 0)
        self.assertEqual(discovered, [True])
        self.assertIn("US-GA-1", stdout.getvalue())

    def test_cli_defaults_cpu_flavor_and_human_output(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = run(
                ["--datacenter", "US-CA-2"], environ={"RUNPOD_API_KEY": "test"},
                fetcher=self.fetch, gpu_fetcher=self.fetch_gpu, stdout=stdout, stderr=stderr,
            )
        self.assertEqual(result, 0)
        self.assertIn("Checked:", stdout.getvalue())
        self.assertIn("US-CA-2", stdout.getvalue())
        self.assertIn("NVIDIA GeForce RTX 4090", stdout.getvalue())
        self.assertIn("$1.1/GPU-hour (serverless)", stdout.getvalue())
        self.assertIn("serverless pool: ADA_24", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_json_and_missing_api_key(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.assertEqual(run(["--datacenter", "US-CA-2", "--json"], environ={}, stdout=stdout, stderr=stderr), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("RUNPOD_API_KEY", stderr.getvalue())
        self.assertIn("US-CA-2", S3_ENDPOINTS)

    def test_accepts_string_network_volume_types(self):
        self.catalogs["US-CA-2"]["networkVolumeTypes"] = ["STANDARD"]
        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=(), cheapest_gpus=1,
            data_centers=("US-CA-2",), gpu_catalog_fetcher=self.fetch_gpu,
        )
        self.assertEqual([item.data_center_id for item in results], ["US-CA-2"])
        self.assertEqual(rejected, [])

    def test_defaults_to_any_cpu_and_uses_selected_gpu_price_type(self):
        results, rejected = select_data_centers(
            self.fetch,
            cpu_flavors=(),
            gpu_preferences=(),
            cheapest_gpus=1,
            price_type="community",
            gpu_catalog_fetcher=self.fetch_gpu,
            data_centers=("US-CA-2",),
        )
        self.assertEqual(rejected, [])
        self.assertEqual(results[0].cpu_flavors, ("cpu3c",))
        self.assertEqual(results[0].gpus[0].name, "NVIDIA GeForce RTX 4090")
        self.assertEqual(results[0].gpus[0].price, 0.31)
        self.assertEqual(results[0].gpus[0].price_type, "community")
        self.assertEqual(results[0].gpus[0].serverless_pool_id, "ADA_24")

    def test_price_max_requires_the_cheapest_matching_gpu_to_be_strictly_cheaper(self):
        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=(), cheapest_gpus=2,
            price_type="serverless", price_max=1.11,
            gpu_catalog_fetcher=self.fetch_gpu, data_centers=("US-CA-2",),
        )
        self.assertEqual([item.data_center_id for item in results], ["US-CA-2"])
        self.assertEqual(rejected, [])

        results, rejected = select_data_centers(
            self.fetch, cpu_flavors=("cpu3c",), gpu_preferences=(), cheapest_gpus=2,
            price_type="serverless", price_max=1.10,
            gpu_catalog_fetcher=self.fetch_gpu, data_centers=("US-CA-2",),
        )
        self.assertEqual(results, [])
        self.assertIn("not less than price maximum $1.1", rejected[0].reasons[0])

    def test_cli_applies_price_max(self):
        stdout = io.StringIO()
        result = run(
            ["--datacenter", "US-CA-2", "--price-max", "1.11"],
            environ={"RUNPOD_API_KEY": "test"}, fetcher=self.fetch,
            gpu_fetcher=self.fetch_gpu, stdout=stdout,
        )
        self.assertEqual(result, 0)
        self.assertIn("US-CA-2", stdout.getvalue())

    def test_http_debug_outputs_catalog_exchange_without_bearer_value(self):
        debug = []
        captured = []

        def opener(request, *, timeout):
            captured.append((request, timeout))
            return FakeCatalogResponse(b'{"networkVolumeTypes": ["STANDARD"]}')

        result = _fetch_catalog("never-print-this", "US-CA-2", debug=debug.append, opener=opener)
        transcript = "\n".join(debug)
        self.assertEqual(result["networkVolumeTypes"], ["STANDARD"])
        self.assertEqual(captured[0][1], 20)
        request = captured[0][0]
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertIn(
            ">>> GET https://api.runpod.io/v2/catalog/datacenters/US-CA-2?include=CPU_AVAILABILITY%2CGPU_AVAILABILITY",
            transcript,
        )
        self.assertEqual(request.full_url.split("include=", 1)[1], "%2C".join(CATALOG_INCLUDES))
        self.assertIn(f">>> User-agent: {USER_AGENT}", transcript)
        self.assertIn(">>> Authorization: Bearer <redacted>", transcript)
        self.assertIn("<<< HTTP 200", transcript)
        self.assertIn("<<< X-Request-ID: request-1", transcript)
        self.assertIn('{"networkVolumeTypes": ["STANDARD"]}', transcript)
        self.assertNotIn("never-print-this", transcript)

    def test_data_center_listing_uses_catalog_endpoint_and_explicit_user_agent(self):
        captured = []

        def opener(request, *, timeout):
            captured.append((request, timeout))
            return FakeCatalogResponse(b'{"dataCenters": [{"id": "US-GA-1"}]}')

        result = _fetch_data_center_ids("never-print-this", opener=opener)
        self.assertEqual(result, ("US-GA-1",))
        self.assertEqual(captured[0][1], 20)
        self.assertEqual(
            captured[0][0].full_url,
            "https://api.runpod.io/v2/catalog/datacenters?include=CPU_AVAILABILITY%2CGPU_AVAILABILITY",
        )
        self.assertEqual(captured[0][0].get_header("User-agent"), USER_AGENT)

    def test_gpu_price_lookup_uses_encoded_gpu_type_and_explicit_user_agent(self):
        captured = []

        def opener(request, *, timeout):
            captured.append((request, timeout))
            return FakeCatalogResponse(b'{"price": {"serverless": 1.1}, "pool": "ADA_24"}')

        result = _fetch_gpu_catalog("never-print-this", "NVIDIA GeForce RTX 4090", opener=opener)
        self.assertEqual(result["pool"], "ADA_24")
        self.assertEqual(captured[0][1], 20)
        self.assertEqual(captured[0][0].full_url, "https://api.runpod.io/v2/catalog/gpus/NVIDIA%20GeForce%20RTX%204090")
        self.assertEqual(captured[0][0].get_header("User-agent"), USER_AGENT)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
