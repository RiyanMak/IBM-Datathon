"""Entry point used by docker-compose: python -m services.replayer.main

With no arguments it replays the test period from /data/data/clean.csv
(~/shared/data/clean.csv on the VM) onto Redis. See replayer.py for the options
(--dry-run, --resume, --speed, ...).
"""

from services.replayer.replayer import main

if __name__ == "__main__":
    main()
