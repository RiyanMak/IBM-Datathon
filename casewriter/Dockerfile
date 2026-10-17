# Case writer container (for Pair 3's docker-compose).
# If the team builds every service FROM the LinuxONE Jupyter image instead,
# just copy the COPY / RUN / CMD lines into that setup.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
CMD ["python3", "-u", "casewriter.py", "--redis"]
