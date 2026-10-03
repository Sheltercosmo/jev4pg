FROM postgres:17.11-bookworm
COPY sdd/postgres/jevsd_pg.control sdd/postgres/jevsd_pg--0.1.0.sql /usr/share/postgresql/17/extension/
