from redos_pdf_combiner.launcher import main
from multiprocessing import freeze_support
import sys


if __name__ == "__main__":
    freeze_support()
    if '--selfcheck' in sys.argv:
        from redos_pdf_combiner.selfcheck import main as selfcheck
        raise SystemExit(selfcheck())
    raise SystemExit(main())
