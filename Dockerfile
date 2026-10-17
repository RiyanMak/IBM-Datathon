# One image for every service; each container runs it with a different command.
# Base: the LinuxONE Jupyter image (PyTorch, XGBoost, scikit-learn already built
# for s390x). Fill in the exact image name and tag from the VM at hour 0.
FROM <linuxone-jupyter-image>:<tag>

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY services/ ./services/

USER jovyan
