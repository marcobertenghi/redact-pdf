"""Allow ``python -m redact_pdf``."""

import sys

from redact_pdf.cli import main

sys.exit(main())
