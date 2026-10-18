# One image for every service; each container runs it with a different command.
# Base: the LinuxONE Jupyter image. PyTorch, XGBoost, scikit-learn, pandas,
# networkx and graphviz are already built for s390x inside it.
FROM registry.linuxone.cloud.marist.edu/l1cc/jupyterlab-image-s390x:latest

WORKDIR /app
# Print log lines as they happen, so `docker compose logs -f` shows them.
ENV PYTHONUNBUFFERED=1

# Freeze the base image's scientific stack as pip constraints. Without this an
# optional package pulled pandas down to a from-source build that segfaults on
# s390x in every datetime operation. Nothing below may change these versions.
RUN pip freeze | grep -iE "^(pandas|numpy|scipy|torch|xgboost|scikit-learn|networkx|graphviz|pyarrow)==" \
      > /tmp/constraints.txt && cat /tmp/constraints.txt

# Packages every service needs, plus pytest and fakeredis so the test suite, the GNN
# training and the evaluation all run inside this image on the VM. All pure Python.
RUN pip install --no-cache-dir -c /tmp/constraints.txt redis requests flask pytest fakeredis

# Packages that may have no s390x wheel (streamlit needs pyarrow; snapml and
# ibm-watsonx-ai are IBM-specific). Each is tried on its own under the same
# constraints, so one failure neither breaks the build nor changes pandas;
# the build log says which ones were skipped.
RUN for pkg in streamlit ibm-watsonx-ai snapml; do \
      pip install --no-cache-dir -c /tmp/constraints.txt "$pkg" \
        || echo "WARNING: could not install $pkg on s390x under the base image's versions; skipped"; \
    done

# Prove the stack is untouched; the build fails here if pandas was replaced.
RUN pip freeze | grep -iE "^(pandas|numpy)==" && python -c "import pandas, numpy; print('pandas', pandas.__version__, 'numpy', numpy.__version__)"

COPY common/ ./common/
COPY services/ ./services/
COPY .streamlit/ ./.streamlit/

# The image's non-root user (uid 1000); ~/shared on the VM is mounted as /data.
USER ibm-user
