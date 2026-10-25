# Sandbox image for dynamic skill analysis (fork, see docs/SANDBOX.md).
# The agent runs as an unprivileged user under strace; honey files and the skill are
# copied in per run by the orchestrator, never baked into the image.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends strace curl git ca-certificates jq procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /work/out /opt/sandbox \
    && chown -R agent:agent /work

COPY harness.py /opt/sandbox/harness.py

USER agent
WORKDIR /home/agent/project
ENTRYPOINT ["strace", "-f", "-qq", "-s", "256", "-e", "trace=openat,execve,connect,unlinkat,renameat2,sendto", "-o", "/work/out/strace.log", "python3", "/opt/sandbox/harness.py"]
