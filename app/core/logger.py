import logging
import sys

def setup_logging(log_level: str = "INFO"):
    """
    Configures structured logging for the application.
    """
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    # Suppress verbose third-party logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("memorae")
