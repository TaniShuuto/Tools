# -*- coding: utf-8 -*-
import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PHASE0 = os.path.dirname(_TESTS)
for path in (_PHASE0, _TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)
