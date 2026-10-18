"""Entry point used by docker-compose: python -m services.scorer.main

With no arguments it loads the model from /data/models, warms up from
/data/data/clean.csv and scores the `txns` stream onto `scored`. See scorer.py
for the options (--once, --no-warmup, --consumer, ...).
"""

from services.scorer.scorer import main

if __name__ == "__main__":
    main()
