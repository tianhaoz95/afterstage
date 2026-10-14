import logging

from rich.logging import RichHandler

logging.basicConfig(level=logging.INFO, format="%(message)s", datefmt="%H:%M:%S",
                    handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=False)])
log = logging.getLogger("afterstage")
