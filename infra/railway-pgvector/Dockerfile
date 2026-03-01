FROM pgvector/pgvector:pg16

ENV POSTGRES_DB=memu
ENV POSTGRES_USER=memu
ENV POSTGRES_PASSWORD=memu

# Auto-enable pgvector on startup
COPY init-pgvector.sql /docker-entrypoint-initdb.d/

EXPOSE 5432
