# Installation ownership

Catalog version 3 extends this decision with [explicit schema isolation](0007-catalog-isolation.md). The public-layout rules below apply to validated legacy installations.

## Decision

Check the target before applying application migrations. A read-only CLI check and the transactional migration share one installation inspector. A version row alone cannot authorize changes to other objects.

Fresh installations reject reserved table, guard-function and data-schema collisions. Existing installations must match the supported version's columns, primary keys, owners, tenant policies and guard-function signatures. Runtime logins must not belong to administrative roles or the installation owner. Catalog version 1 has an explicit contract excluding the two tables introduced by version 2.

Run the check under the migration advisory lock before creating roles or objects. Resolve built-in functions through `pg_catalog`, create metadata in `public` explicitly, and qualify security DDL. Do not repair conflicting policies or take ownership automatically. The CLI reports object-level conflicts without row contents or credentials.

## Limits

These checks establish installation compatibility, not protection against a malicious database administrator. They do not establish full index, foreign-key or trigger-body integrity, provider readiness, recovery readiness or shared-database isolation. The supported deployment still uses a dedicated database. A concurrent database administrator can change objects independently of the application advisory lock; coordinate administrative changes during deployment.

## Execution placement

This is an installation prerequisite, outside query planning and provider execution. No JEV stage, provider call or additional query-time barrier is introduced. Independent work retains its shared DAG and existing output states.

## Validation

Exercise fresh and repeated installation, populated release upgrades, unmanaged collisions, inherited administrator membership, custom and Chinese search paths, changed ownership and policies, and inconsistent catalog markers against PostgreSQL. A refusal must preserve existing rows, ACLs and policies. These are deployment-contract checks, not language-accuracy measurements.
