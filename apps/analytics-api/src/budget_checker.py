"""Hourly budget-threshold check, dispatching one email per crossed threshold.

Mirrors the gateway's ACPI refresh loop (`apps/gateway/src/services/acpi.py`):
a start/stop pair around an `asyncio.create_task` loop that never lets one bad
iteration kill the background task.

Three thresholds per workspace: the workspace's own `alert_pct` ("warning"),
100% ("exceeded"), and 120% ("critical"). Each is sent at most once per
workspace per calendar month — `budget_alerts_sent` is the dedup ledger, keyed
on (workspace_id, alert_type, month), so re-running this loop hourly cannot
double-send.
"""

from __future__ import annotations

import asyncio
import logging
import os
from calendar import monthrange
from datetime import datetime, timezone

from .db import db, f

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600


def _dashboard_url() -> str:
    return os.getenv("TOKENIX_DASHBOARD_URL", "https://tokenixindex.com/forecast")


def _send_email(to_email: str, pct_used: float, current_spend: float, limit: float, projected: float) -> bool:
    """Send the alert via SendGrid. Returns False (and logs) rather than raising,
    so one bad send doesn't stop the rest of the batch or crash the loop."""
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        log.warning("SENDGRID_API_KEY not set; skipping budget alert email to %s", to_email)
        return False

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    subject = f"⚠ Tokenix: You've used {pct_used:.0f}% of your monthly AI budget"
    body = (
        f"Your workspace has used {pct_used:.0f}% of its monthly AI budget.\n\n"
        f"Current spend:     ${current_spend:,.2f}\n"
        f"Monthly limit:     ${limit:,.2f}\n"
        f"Projected month-end spend: ${projected:,.2f}\n\n"
        f"View your usage: {_dashboard_url()}\n"
    )
    message = Mail(
        from_email=os.getenv("SENDGRID_FROM_EMAIL", "alerts@tokenixindex.com"),
        to_emails=to_email,
        subject=subject,
        plain_text_content=body,
    )
    try:
        SendGridAPIClient(api_key).send(message)
        return True
    except Exception:
        log.exception("failed to send budget alert email to %s", to_email)
        return False


async def _check_workspace(budget: dict) -> None:
    workspace_id = budget["workspace_id"]
    limit = f(budget["monthly_limit_usd"])
    if limit <= 0:
        return

    row = await db.fetchrow(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS spend
        FROM usage_records
        WHERE workspace_id = $1 AND priced
          AND "timestamp" >= date_trunc('month', NOW())
        """,
        workspace_id,
    )
    current_spend = f(row["spend"] if row else 0)

    now = datetime.now(timezone.utc)
    days_in_month = monthrange(now.year, now.month)[1]
    day_of_month = now.day
    projected_eom = (current_spend / day_of_month) * days_in_month if day_of_month else 0.0
    pct_used = (current_spend / limit) * 100

    month_key = now.strftime("%Y-%m")
    thresholds = [
        ("warning", float(budget["alert_pct"])),
        ("exceeded", 100.0),
        ("critical", 120.0),
    ]

    for alert_type, threshold_pct in thresholds:
        if pct_used < threshold_pct:
            continue

        already_sent = await db.fetchrow(
            "SELECT 1 FROM budget_alerts_sent WHERE workspace_id = $1 AND alert_type = $2 AND month = $3",
            workspace_id,
            alert_type,
            month_key,
        )
        if already_sent is not None:
            continue

        sent = _send_email(
            budget["alert_email"], pct_used, current_spend, limit, projected_eom
        )
        if not sent:
            continue  # don't record the ledger row; retry next hour

        await db.fetchrow(
            """
            INSERT INTO budget_alerts_sent (workspace_id, alert_type, month)
            VALUES ($1, $2, $3)
            ON CONFLICT (workspace_id, alert_type, month) DO NOTHING
            RETURNING id
            """,
            workspace_id,
            alert_type,
            month_key,
        )


async def check_all_budgets() -> None:
    budgets = await db.fetch(
        "SELECT workspace_id, monthly_limit_usd, alert_pct, alert_email FROM budgets"
    )
    for budget in budgets:
        try:
            await _check_workspace(budget)
        except Exception:  # one workspace's failure must not block the rest
            log.exception("budget check failed for workspace %s", budget["workspace_id"])


class BudgetChecker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def _run_forever(self) -> None:
        while True:
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
            try:
                await check_all_budgets()
            except Exception:  # never let the hourly loop die
                log.exception("budget checker iteration failed")

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


budget_checker = BudgetChecker()
