FROM ubuntu:24.04

# util-linux provides setsid, which the adapter uses to start envd in its own
# process group; ca-certificates is not needed because envd trusts WebPKI roots
# plus the operator CA file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
