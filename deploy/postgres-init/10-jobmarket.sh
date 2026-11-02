#!/bin/sh
# Runs once, when the database volume is first created (docker-entrypoint-initdb.d). Creates
# the three application logins and the application database owned by the ingestion login,
# which is not a superuser and cannot create databases or roles, plus Airflow's own login and
# database (its run history), which the application logins cannot reach.
#
# Passwords are read inside psql with \getenv, so they never appear on a command line, and
# the :'name' form quotes them as SQL literals.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
\getenv owner DB_USER
\getenv owner_password DB_PASSWORD
\getenv analytics_password ANALYTICS_DB_PASSWORD
\getenv private_password PRIVATE_DB_PASSWORD
\getenv database DB_NAME
\getenv airflow_password AIRFLOW_DB_PASSWORD
CREATE ROLE :"owner" LOGIN PASSWORD :'owner_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE jobmarket_analytics LOGIN PASSWORD :'analytics_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE jobmarket_private LOGIN PASSWORD :'private_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE :"database" OWNER :"owner";
CREATE ROLE airflow LOGIN PASSWORD :'airflow_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE airflow OWNER airflow;
REVOKE CONNECT ON DATABASE airflow FROM PUBLIC;
SQL
