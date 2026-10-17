"""Entry point used by docker-compose: python -m services.casewriter.main

With no arguments it runs the real service on Redis. See casewriter.py for
the --file and --push-fake test modes.
"""

from services.casewriter.casewriter import main

if __name__ == "__main__":
    main()
