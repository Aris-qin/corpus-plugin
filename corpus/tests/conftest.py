"""pytest bootstrap for the corpus test suite.

The corpus engine ships as a set of flat modules under ``corpus/`` (``db.py``,
``canonical.py``, ``chunker_markdown.py`` …) that import each other by bare name
(``import db``). Put the package directory on ``sys.path`` so both the legacy
chunker tests and the new contract tests resolve those imports regardless of the
directory pytest is invoked from.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORPUS_DIR = Path(__file__).resolve().parent.parent
if str(_CORPUS_DIR) not in sys.path:
    sys.path.insert(0, str(_CORPUS_DIR))

# ``test_chunker.py`` is a hand-run reporting script (its ``test_file(p)`` takes a
# required positional that pytest would mis-read as a fixture, and it points at an
# absolute review directory that only exists on the original workstation). It is
# not a pytest suite — skip collection instead of erroring the whole run.
collect_ignore = ["test_chunker.py"]
