import unittest

import app as app_module

app = app_module.app


class ExecutionDomainEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module._autostart_fallback_done = True
        self.client = app.test_client()

    def test_execution_domain_defaults_to_production(self):
        response = self.client.get("/execution-domain")
        self.assertEqual(response.status_code, 200)
        # core normalizes the production execution domain to its canonical short
        # form "prod" (core/kalshi_monitor.py::_current_kalshi_execution_domain).
        self.assertEqual(response.get_json(), {"execution_domain": "prod"})


if __name__ == "__main__":
    unittest.main()
