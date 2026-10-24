# postgres

PostgreSQL 18 with pgvector 0.8.7: data, the job queue and embeddings (D-014, D-019). The official pgvector image, pinned by digest.

- **Layout:** one database, `scc`. Each module has its own schema and two roles, created by its migrate step (`scc_common.db.bootstrap`): `scc_<module>_owner` owns the tables, and `scc_<module>_app` (the login the module uses) can read and write rows in its own schema only.

  | Module | Schema | App role |
  |---|---|---|
  | web | `auth` | `scc_web_app` |
  | core | `core` | `scc_core_app` |
  | gateway | `gateway` | `scc_gateway_app` |

- **Secrets:** `postgres_superuser_password` (used only by the migrate steps) and one `db_<module>_password` per module.
- **Health:** native, `pg_isready`.
- **Data:** the `pgdata` volume at `/var/lib/postgresql` (the PostgreSQL 18 image layout).
