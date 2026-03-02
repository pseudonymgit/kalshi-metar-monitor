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
        self.assertEqual(response.get_json(), {"execution_domain": "production"})


if __name__ == "__main__":
    unittest.main()
