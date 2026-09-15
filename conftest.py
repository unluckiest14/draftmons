"""Puts the project root on sys.path so `import draftmons` works in tests.

pytest adds rootdir to sys.path only when there is no installed package; this
makes it explicit rather than depending on that behaviour, so `pytest` works
the same from the root or from inside tests/.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
