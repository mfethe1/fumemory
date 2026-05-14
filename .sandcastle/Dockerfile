FROM node:22-bookworm

RUN apt-get update && apt-get install -y \
  build-essential \
  curl \
  git \
  jq \
  python3 \
  python3-pip \
  python3-venv \
  && rm -rf /var/lib/apt/lists/*

# GitHub CLI is available for future workflows, but this runner does not
# inject GH_TOKEN by default.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
  && apt-get update && apt-get install -y gh \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/fumemory-deps
COPY pyproject.toml requirements.txt README.md ./
COPY memu ./memu
RUN python3 -m pip install --break-system-packages --disable-pip-version-check --upgrade pip \
  && python3 -m pip install --break-system-packages --disable-pip-version-check --prefer-binary -e ".[dev]"

RUN usermod -d /home/agent -m -l agent node
USER agent

RUN curl -fsSL https://claude.ai/install.sh | bash

ENV PATH="/home/agent/.local/bin:$PATH"
WORKDIR /home/agent

ENTRYPOINT ["sleep", "infinity"]
