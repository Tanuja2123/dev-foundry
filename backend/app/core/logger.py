import logging
import sys

# Configure clean, human-readable logging without JSON bloat
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)

logger = logging.getLogger("DevFoundry")
