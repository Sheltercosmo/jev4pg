ARG POSTGRES_MAJOR=17
FROM postgres:${POSTGRES_MAJOR}-bookworm
ARG POSTGRES_MAJOR
RUN case "$POSTGRES_MAJOR" in 14|15|16|17) ;; *) echo "Supported PostgreSQL majors: 14-17" >&2; exit 1 ;; esac
COPY sdd/postgres/jevsd_pg.control sdd/postgres/jevsd_pg--0.1.0.sql /usr/share/postgresql/${POSTGRES_MAJOR}/extension/
