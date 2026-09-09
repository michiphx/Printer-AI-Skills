"""Logging for the printer-ai CLI.

stdout carries the CLI payload (often JSON that a caller parses), so log records
must never go there. By default the logger is silent: a NullHandler keeps
logging's "last resort" handler from dumping records onto stderr and confusing
users. Set PRINTER_AI_DEBUG=1 (or PRINTER_AI_LOG_LEVEL=DEBUG/INFO/WARNING/...)
to get diagnostics on stderr.
"""

import logging
import os
import sys

logger = logging.getLogger("printer-ai")
# Stay out of any logging configuration the host application may have set up
logger.propagate = False

_level_name = os.environ.get("PRINTER_AI_LOG_LEVEL", "").strip().upper()
if not _level_name and os.environ.get("PRINTER_AI_DEBUG", "").strip() not in ("", "0", "false", "False"):
    _level_name = "DEBUG"

if _level_name:
    _level = getattr(logging, _level_name, None)
    if not isinstance(_level, int):
        _level = logging.DEBUG
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(_level)
else:
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
