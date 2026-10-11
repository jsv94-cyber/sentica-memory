-- Two local tenants. Each role can reach only its own database and the
-- memory schema inside it. This file is test data, not a production secret.

CREATE ROLE tenant_a LOGIN;
CREATE ROLE tenant_b LOGIN;

CREATE DATABASE tenant_a OWNER tenant_a;
CREATE DATABASE tenant_b OWNER tenant_b;

REVOKE ALL ON DATABASE tenant_a FROM PUBLIC;
REVOKE ALL ON DATABASE tenant_b FROM PUBLIC;
GRANT CONNECT ON DATABASE tenant_a TO tenant_a;
GRANT CONNECT ON DATABASE tenant_b TO tenant_b;

\c tenant_a
CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION tenant_a;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA memory;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA memory;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA memory TO tenant_a;
CREATE TABLE public.outside_memory (id int);
REVOKE ALL ON TABLE public.outside_memory FROM PUBLIC;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_a;
ALTER ROLE tenant_a IN DATABASE tenant_a SET search_path TO memory;

\c tenant_b
CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION tenant_b;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA memory;
CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA memory;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA memory FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA memory TO tenant_b;
CREATE TABLE public.outside_memory (id int);
REVOKE ALL ON TABLE public.outside_memory FROM PUBLIC;
REVOKE ALL ON TABLE public.outside_memory FROM tenant_b;
ALTER ROLE tenant_b IN DATABASE tenant_b SET search_path TO memory;
