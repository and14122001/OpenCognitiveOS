"""Module entrypoint for `python -m opencognitiveos`."""

import sys

from opencognitiveos.interfaces.cli import main

if __name__ == "__main__":
    sys.exit(main())
