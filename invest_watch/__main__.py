"""Allow ``python -m invest_watch ...`` (scheduler invocation, specs §12.1)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())