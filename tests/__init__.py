"""
Test case import hook for mocking Trac dependencies
"""

import sys
from unittest.mock import MagicMock

# Mock pkg_resources before any Trac imports
if "pkg_resources" not in sys.modules:
    pkg_resources = MagicMock()
    pkg_resources.DistributionNotFound = Exception
    pkg_resources.get_distribution = MagicMock(return_value=MagicMock(version="1.6.0"))

    # Simple version comparison for mocking
    class MockVersion:
        def __init__(self, version_string):
            self.version_string = str(version_string)

        def __lt__(self, other):
            if isinstance(other, MockVersion):
                return self.version_string < other.version_string
            return str(self.version_string) < str(other)

        def __eq__(self, other):
            if isinstance(other, MockVersion):
                return self.version_string == other.version_string
            return str(self.version_string) == str(other)

    pkg_resources.parse_version = MockVersion
    sys.modules["pkg_resources"] = pkg_resources
