ARG POSTGRES_MAJOR=17
FROM rust:1.96-bookworm AS rust

FROM postgres:${POSTGRES_MAJOR}-bookworm AS build
ARG POSTGRES_MAJOR
RUN case "$POSTGRES_MAJOR" in 14|15|16|17) ;; *) echo "Supported PostgreSQL majors: 14-17" >&2; exit 1 ;; esac
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup
ENV PATH=/usr/local/cargo/bin:$PATH PGRX_BUILD_FLAGS=--locked
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libclang-dev pkg-config postgresql-server-dev-${POSTGRES_MAJOR} ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked cargo-pgrx --version 0.19.2 \
    && cargo pgrx init --pg${POSTGRES_MAJOR} /usr/lib/postgresql/${POSTGRES_MAJOR}/bin/pg_config
WORKDIR /build/native
COPY native/Cargo.toml native/Cargo.lock ./
COPY native/core ./core
COPY native/pg ./pg
WORKDIR /build/native/pg
RUN cargo pgrx install --release --no-default-features --features pg${POSTGRES_MAJOR} \
    --pg-config /usr/lib/postgresql/${POSTGRES_MAJOR}/bin/pg_config

FROM postgres:${POSTGRES_MAJOR}-bookworm
ARG POSTGRES_MAJOR
RUN apt-get update && apt-get install -y --no-install-recommends jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/lib/postgresql/${POSTGRES_MAJOR}/lib/jev_native.so /usr/lib/postgresql/${POSTGRES_MAJOR}/lib/
COPY --from=build /usr/share/postgresql/${POSTGRES_MAJOR}/extension/jev_native* /usr/share/postgresql/${POSTGRES_MAJOR}/extension/
COPY sdd/postgres/jevsd_pg.control sdd/postgres/jevsd_pg--0.1.0.sql /usr/share/postgresql/${POSTGRES_MAJOR}/extension/
COPY deploy/native-entrypoint.sh /usr/local/bin/jev-native-entrypoint
RUN chmod 755 /usr/local/bin/jev-native-entrypoint
ENTRYPOINT ["jev-native-entrypoint"]
CMD ["postgres"]
