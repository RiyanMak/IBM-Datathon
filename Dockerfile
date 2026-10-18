# One image for every service; each container runs it with a different command.
# Base: the LinuxONE Jupyter image. PyTorch, XGBoost, scikit-learn, pandas,
# networkx and graphviz are already built for s390x inside it.
FROM registry.linuxone.cloud.marist.edu/l1cc/jupyterlab-image-s390x:latest

WORKDIR /app
# Print log lines as they happen, so `docker compose logs -f` shows them.
ENV PYTHONUNBUFFERED=1

# Packages every service needs, plus pytest and fakeredis so the test suite, the GNN
# training and the evaluation all run inside this image on the VM. All pure Python.
RUN pip install --no-cache-dir redis requests flask pytest fakeredis

# Packages that may have no s390x wheel (streamlit needs pyarrow; snapml and
# ibm-watsonx-ai are IBM-specific). Each is tried on its own so one failure
# does not break the build; the build log says which ones were skipped.
RUN for pkg in streamlit ibm-watsonx-ai snapml; do \
      pip install --no-cache-dir "$pkg" || echo "WARNING: could not install $pkg on s390x; skipped"; \
    done

COPY common/ ./common/
COPY services/ ./services/

# The image's non-root user (uid 1000); ~/shared on the VM is mounted as /data.
USER ibm-user
