#!/usr/bin/env python3
"""Simple test runner for Layer 1 implementation."""

import sys
import os
import unittest

# Add the project to the path
sys.path.insert(0, '/home/node/.openclaw/workspace/prototypes/weather-engine-source')

# Set up environment
os.environ["ALERT_DB_PATH"] = "/tmp/test_layer1_alerts.db"

# Clean up any existing test database
if os.path.exists("/tmp/test_layer1_alerts.db"):
    os.unlink("/tmp/test_layer1_alerts.db")

# Run the tests
loader = unittest.TestLoader()
suite = loader.discover('tests', pattern='test_layer1_*.py')

# Run with verbosity
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

# Clean up
if os.path.exists("/tmp/test_layer1_alerts.db"):
    os.unlink("/tmp/test_layer1_alerts.db")

# Exit with appropriate code
sys.exit(0 if result.wasSuccessful() else 1)
