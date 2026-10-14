-- Two local tenants. An admin role owns each database. The tenant role
-- can use only the memory schema. This file is test data, not a secret.

CREATE ROLE tenant_a LOGIN;
CREATE ROLE tenant_b LOGIN;
CREATE ROLE sentica_ddl NOLOGIN NOBYPASSRLS;
GRANT sentica_ddl TO tenant_a;
GRANT sentica_ddl TO tenant_b;

CREATE DATABASE tenant_a OWNER postgres;
CREATE DATABASE tenant_b OWNER postgres;

REVOKE ALL ON DATABASE tenant_a FROM PUBLIC;
REVOKE ALL ON DATABASE tenant_b FROM PUBLIC;
REVOKE CREATE ON DATABASE tenant_a FROM tenant_a;
REVOKE CREATE ON DATABASE tenant_b FROM tenant_b;
GRANT CONNECT ON DATABASE tenant_a TO tenant_a;
GRANT CONNECT ON DATABASE tenant_b TO tenant_b;

ALTER ROLE tenant_a SET statement_timeout = '120s';
ALTER ROLE tenant_b SET statement_timeout = '120s';

\c tenant_a
ALTER SCHEMA public OWNER TO postgres;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION tenant_a;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA memory;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA memory;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA memory TO tenant_a;
CREATE TABLE public.outside_memory (id int);
REVOKE ALL ON TABLE public.outside_memory FROM PUBLIC;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_a;
ALTER ROLE tenant_a IN DATABASE tenant_a SET search_path TO memory;

\c tenant_b
ALTER SCHEMA public OWNER TO postgres;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION tenant_b;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA memory;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA memory;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA memory TO tenant_b;
CREATE TABLE public.outside_memory (id int);
REVOKE ALL ON TABLE public.outside_memory FROM PUBLIC;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_b;
ALTER ROLE tenant_b IN DATABASE tenant_b SET search_path TO memory;
