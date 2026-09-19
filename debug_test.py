import sys
sys.path.insert(0, r'C:\Users\HomePC\Desktop\Veronica')

from tests.test_vision_model import TestVisionJobExecutor
import unittest

# Create a test suite with just one test
suite = unittest.TestSuite()
test_case = TestVisionJobExecutor()
test_case.setUp()
suite.addTest(TestVisionJobExecutor('test_submit_when_busy_returns_none'))

# Run the test
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)