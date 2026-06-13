"""
conftest.py
Pytest configuration — adds project root to sys.path so all
src.* imports work from any test file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
