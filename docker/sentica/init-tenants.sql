-- Two local tenants. The migration role owns the memory schema. The runtime
-- role can read and write rows and cannot create objects or bypass row-level
-- security. This file is test data, not a secret.

CREATE ROLE tenant_a LOGIN NOBYPASSRLS;
CREATE ROLE tenant_b LOGIN NOBYPASSRLS;
CREATE ROLE tenant_a_ddl LOGIN NOBYPASSRLS;
CREATE ROLE tenant_b_ddl LOGIN NOBYPASSRLS;

CREATE DATABASE tenant_a OWNER postgres;
CREATE DATABASE tenant_b OWNER postgres;

REVOKE ALL ON DATABASE tenant_a FROM PUBLIC;
REVOKE ALL ON DATABASE tenant_b FROM PUBLIC;
REVOKE CREATE ON DATABASE tenant_a FROM PUBLIC;
REVOKE CREATE ON DATABASE tenant_b FROM PUBLIC;
GRANT CONNECT ON DATABASE tenant_a TO tenant_a;
GRANT CONNECT ON DATABASE tenant_a TO tenant_a_ddl;
GRANT CONNECT ON DATABASE tenant_b TO tenant_b;
GRANT CONNECT ON DATABASE tenant_b TO tenant_b_ddl;

ALTER ROLE tenant_a SET statement_timeout = '120s';
ALTER ROLE tenant_b SET statement_timeout = '120s';
ALTER ROLE tenant_a_ddl SET statement_timeout = '120s';
ALTER ROLE tenant_b_ddl SET statement_timeout = '120s';

\c tenant_a
ALTER SCHEMA public OWNER TO postgres;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION tenant_a_ddl;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA memory;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA memory;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO tenant_a;
CREATE TABLE public.outside_memory (id int);
REVOKE ALL ON TABLE public.outside_memory FROM PUBLIC;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_a;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_a_ddl;
ALTER ROLE tenant_a IN DATABASE tenant_a SET search_path TO memory;
ALTER ROLE tenant_a_ddl IN DATABASE tenant_a SET search_path TO memory;

\c tenant_b
ALTER SCHEMA public OWNER TO postgres;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION tenant_b_ddl;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA memory;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA memory;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE ON SCHEMA memory TO tenant_b;
CREATE TABLE public.outside_memory (id int);
REVOKE ALL ON TABLE public.outside_memory FROM PUBLIC;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_b;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_b_ddl;
ALTER ROLE tenant_b IN DATABASE tenant_b SET search_path TO memory;
ALTER ROLE tenant_b_ddl IN DATABASE tenant_b SET search_path TO memory;
