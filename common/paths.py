"""One place for where the shared files live inside the containers.

docker-compose mounts the VM's ~/shared folder as /data in every service, and
the team guide keeps data and models in two folders under it:

    ~/shared/data/    clean.csv, splits.json, fx_rates.json, HI-Small_Patterns.txt
    ~/shared/models/  scorer.json, feature_list.json, thresholds.json

so inside a container they are /data/data/... and /data/models/...

    from common.paths import DATA_DIR, MODELS_DIR
    clean = DATA_DIR / "clean.csv"

DATA_DIR and MODELS_DIR in the environment override them (the training
notebook reads the same two variables).
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/data"))
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/data/models"))
