# ===========================================================================
# Dockerfile -- Cross-Platform Container for macOS (Apple Silicon M1-M4 / Intel)
#               and Linux Reproducibility
#
# Automatically invoked by ./run_all.sh when run on macOS (Darwin), or can be
# run manually via:
#   docker build --platform linux/amd64 -t hint-gather-revised .
#   docker run --rm -it --platform linux/amd64 -v "$PWD:/workspace" -w /workspace hint-gather-revised ./run_all.sh
# ===========================================================================

FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    xz-utils \
    bzip2 \
    patch \
    build-essential \
    python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
CMD ["./run_all.sh", "all"]
