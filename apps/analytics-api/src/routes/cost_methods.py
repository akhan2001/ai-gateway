"""GET /api/v1/cost-methods — does a date range mix two cost formulas?

On 2026-10-21 the gateway started pricing requests net of prompt caching
(tokenix docs/cost-methodology.md, cost_method_version 2). Rows before that
priced cached input at the full rate, so a chart or total spanning the switch
compares two formulas. The dashboard shows a note only when its range does.

The switch is found per workspace as its first row priced the new way. Every
such row has uncached_input_tokens set (sql/008); reading that instead of
cost_method_version (sql/010) keeps this working before 010 is deployed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ..auth import current_workspace
from ..db import db

router = APIRouter(prefix="/api/v1", tags=["cost-methods"])

# ponytail: MIN over the workspace's rows on every page load; fine at current
# volume, cache switched_at per workspace if this shows up in query times.
_SQL = """
WITH switch AS (
    SELECT MIN("timestamp") AS at
    FROM usage_records
    WHERE workspace_id = $1 AND uncached_input_tokens IS NOT NULL
)
SELECT switch.at AS switched_at,
       switch.at IS NOT NULL AND EXISTS (
           SELECT 1 FROM usage_records
           WHERE workspace_id = $1 AND priced
             AND "timestamp" >= $2 AND "timestamp" < switch.at
       ) AS crosses
FROM switch
"""


@router.get("/cost-methods")
async def cost_methods(
    since: datetime = Query(..., description="Start of the range the page shows (ISO 8601)"),
    workspace_id: UUID = Depends(current_workspace),
) -> dict:
    row = await db.fetchrow(_SQL, workspace_id, since)
    switched_at = row["switched_at"] if row else None
    return {
        "switched_at": switched_at.isoformat() if switched_at else None,
        "crosses": bool(row and row["crosses"]),
    }
