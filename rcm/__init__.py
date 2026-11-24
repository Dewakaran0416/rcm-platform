"""RCM Platform — end-to-end revenue cycle, built module by module.

Modules: 0 foundation, 1 scrubber + 837P, 2 835 + posting, 3 denial prediction,
4 denial recovery, 5 claim status, 6 analytics. Run: python -m rcm pipeline
"""
from .generator import Config, Dataset, generate

__version__ = "0.2.0"
__all__ = ["Config", "Dataset", "generate"]
