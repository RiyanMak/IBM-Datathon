"""Entry point used by docker-compose: python -m services.rings.main

With no arguments it reads the ring threshold from /data/models/thresholds.json
and groups the `scored` stream into rings on the `rings` stream. See rings.py
for the options (--once, --ring-threshold, --window-hours, ...).
"""

from services.rings.rings import main

if __name__ == "__main__":
    main()
