# Primp backend

Django 5.2 + Django Ninja API for the Primp booking marketplace. Run all commands from `backend/`.

## Setup with Docker

```bash
cp .env.example .env
docker compose up --build
```

## Setup without Docker

Requires PostgreSQL 16 with PostGIS (`postgis`, `btree_gist`, `pg_trgm`), Redis, GDAL/GEOS/PROJ.

```bash
sudo apt-get install postgresql-16-postgis-3 redis-server libgdal-dev libgeos-dev libproj-dev
make install
```

Create a database user and a test database with the extensions (the test DB is created by pytest).

## Commands

`make lint typecheck test cov run worker beat migrate openapi`
