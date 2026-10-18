"""Entry point used by docker-compose: python -m services.gnn.main

With no arguments it runs the stream service (reads `candidates`, writes kept
rings to `rings`). See rescorer.py for --file, --push-fake and the options.
"""

from services.gnn.rescorer import main

if __name__ == "__main__":
    main()
