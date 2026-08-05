"""Shared test setup: put the poller package dir on sys.path so the Lambda
modules (which import each other flatly, as they do when zipped) resolve."""

import os
import sys

POLLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if POLLER_DIR not in sys.path:
    sys.path.insert(0, POLLER_DIR)
