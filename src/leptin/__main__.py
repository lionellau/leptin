"""Enable ``python -m leptin ...`` as an alias for the ``leptin`` CLI."""

from leptin.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
