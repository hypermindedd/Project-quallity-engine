# Self-contained runtime with every scanner, for local or self-hosted runs:
#   docker build -t pqe .
#   docker run --rm -v "$PWD:/work" pqe run /work --setup
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm pipx && rm -rf /var/lib/apt/lists/* \
 && npm install -g pnpm@11 \
 && pipx install semgrep && ln -s /root/.local/bin/semgrep /usr/local/bin/semgrep
WORKDIR /opt/pqe
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[security,ui]'
ENTRYPOINT ["pqe"]
