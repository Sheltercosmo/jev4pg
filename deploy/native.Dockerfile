FROM rust:1.96-bookworm AS rust

FROM postgres:17.11-bookworm AS build
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup
ENV PATH=/usr/local/cargo/bin:$PATH PGRX_BUILD_FLAGS=--locked
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libclang-dev pkg-config postgresql-server-dev-17 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked cargo-pgrx --version 0.19.2 \
    && cargo pgrx init --pg17 /usr/lib/postgresql/17/bin/pg_config
WORKDIR /build/native
COPY native/Cargo.toml native/Cargo.lock ./
COPY native/core ./core
COPY native/pg ./pg
WORKDIR /build/native/pg
RUN cargo pgrx install --release --pg-config /usr/lib/postgresql/17/bin/pg_config

FROM postgres:17.11-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/lib/postgresql/17/lib/jev_native.so /usr/lib/postgresql/17/lib/
COPY --from=build /usr/share/postgresql/17/extension/jev_native* /usr/share/postgresql/17/extension/
COPY sdd/postgres/jevsd_pg.control sdd/postgres/jevsd_pg--0.1.0.sql /usr/share/postgresql/17/extension/
COPY deploy/native-entrypoint.sh /usr/local/bin/jev-native-entrypoint
RUN chmod 755 /usr/local/bin/jev-native-entrypoint
ENTRYPOINT ["jev-native-entrypoint"]
CMD ["postgres"]
