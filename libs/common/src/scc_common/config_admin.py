"""Read-only views of a module's config store for the admin pages (P3.10, D-048).

Activation and rollback stay on the CLI (D-035, D-048): nothing here writes. core and gateway mount
`router()` for web's admin pages; web reads its own store with `overview()` and `version()` directly.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from scc_common.config_store import Store

if (
    TYPE_CHECKING
):  # scc_common doesn't depend on psycopg-pool; the modules that mount the router do.
    from psycopg_pool import AsyncConnectionPool

VersionName = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9.-]{2,63}$")]


class EvalResult(BaseModel):
    version: str
    run_id: str
    split: str
    run_date: date
    passed: float
    cases: int
    thresholds_met: dict[str, bool]
    attached_at: datetime
    attached_by: str


class VersionSummary(BaseModel):
    version: str
    source: str
    created_at: datetime
    created_by: str
    reason: str
    active: bool
    latest_eval: EvalResult | None


class AuditEntry(BaseModel):
    at: datetime
    actor: str
    action: str
    version: str
    previous_version: str | None
    reason: str


class ConfigOverview(BaseModel):
    """Every version, oldest first, and the newest audit entries first."""

    module: str
    evaluated: bool
    active: str | None
    versions: list[VersionSummary]
    history: list[AuditEntry]


class ConfigVersion(VersionSummary):
    sha256: str
    body_toml: str
    body: dict
    evals: list[EvalResult]


async def _rows(conn: AsyncConnection, query: str, params=()) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def _evals(conn: AsyncConnection, store: Store, version: str | None = None):
    where = "WHERE version = %s" if version else ""
    rows = await _rows(
        conn,
        f"SELECT version, run_id, split, run_date, passed, cases, thresholds_met, attached_at,"
        f" attached_by FROM {store.table('evals')} {where} ORDER BY run_date DESC, id DESC",
        (version,) if version else (),
    )
    return [EvalResult(**r) for r in rows]


async def overview(
    conn: AsyncConnection, store: Store, history_limit: int = 50
) -> ConfigOverview:
    active = await _rows(conn, f"SELECT version FROM {store.table('active')}")
    active_name = active[0]["version"] if active else None
    latest: dict[str, EvalResult] = {}
    if store.evaluated:
        for result in await _evals(conn, store):
            latest.setdefault(result.version, result)
    versions = [
        VersionSummary(
            **r,
            active=r["version"] == active_name,
            latest_eval=latest.get(r["version"]),
        )
        for r in await _rows(
            conn,
            f"SELECT version, source, created_at, created_by, reason FROM {store.table('versions')}"
            " ORDER BY created_at, version",
        )
    ]
    history = await _rows(
        conn,
        f"SELECT at, actor, action, version, previous_version, reason FROM {store.table('audit')}"
        " ORDER BY id DESC LIMIT %s",
        (history_limit,),
    )
    return ConfigOverview(
        module=store.schema,
        evaluated=store.evaluated,
        active=active_name,
        versions=versions,
        history=[AuditEntry(**h) for h in history],
    )


async def version(
    conn: AsyncConnection, store: Store, name: str
) -> ConfigVersion | None:
    rows = await _rows(
        conn,
        f"SELECT v.version, v.source, v.created_at, v.created_by, v.reason, v.sha256, v.body_toml,"
        f" v.body, a.version IS NOT NULL AS active FROM {store.table('versions')} v"
        f" LEFT JOIN {store.table('active')} a USING (version) WHERE v.version = %s",
        (name,),
    )
    if not rows:
        return None
    evals = await _evals(conn, store, name) if store.evaluated else []
    return ConfigVersion(
        **rows[0], latest_eval=evals[0] if evals else None, evals=evals
    )


def router(
    store: Store, pool_of: Callable[[Request], AsyncConnectionPool]
) -> APIRouter:
    """`GET /admin/config` and `GET /admin/config/versions/{version}` for one module's store."""
    api = APIRouter(prefix="/admin/config", tags=["admin"])

    @api.get("")
    async def config_overview(request: Request) -> ConfigOverview:
        """Every config version, the active one, eval results and the audit trail. Read-only."""
        async with pool_of(request).connection() as conn:
            return await overview(conn, store)

    @api.get("/versions/{name}", responses={404: {"description": "No such version"}})
    async def config_version(name: VersionName, request: Request) -> ConfigVersion:
        """One version's TOML text and every eval result attached to it."""
        async with pool_of(request).connection() as conn:
            found = await version(conn, store, name)
        if found is None:
            raise HTTPException(
                404, {"code": "not_found", "message": f"No version {name}."}
            )
        return found

    return api
