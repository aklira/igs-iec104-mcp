# The runtime image (IMPLEMENTATION.md §11). Build it with the runtime directory as the context:
#   docker build --file docker/runtime.Dockerfile runtime

# Build with the pinned toolchain and the OpenSSL development files (the default TLS backend).
FROM rust:1.99.0-trixie AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN cargo build --locked --release --package igs-iec104-runtime

# Run on a minimal image with the OpenSSL runtime library, as a non-root user.
FROM debian:trixie-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3t64 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system runtime \
    && useradd --system --gid runtime --no-create-home --shell /usr/sbin/nologin runtime
COPY --from=build /src/target/release/igs-iec104-runtime /usr/local/bin/igs-iec104-runtime
USER runtime
ENTRYPOINT ["/usr/local/bin/igs-iec104-runtime"]
