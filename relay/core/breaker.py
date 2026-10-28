"""Per-tool circuit breaker: stop calling a tool that is clearly down.

WHAT IT IS  A shared trip switch, one row per tool in Postgres. After
            `breaker_threshold` consecutive failures the switch OPENs: every
            worker skips that tool for `breaker_open_seconds` instead of
            burning retries on it. Then exactly one worker sends one probe
            call (HALF_OPEN); if the probe succeeds the switch closes for
            everyone, if it fails the switch re-opens.
GOES IN     The tool's name. Thresholds come from Settings.
COMES OUT   allow(tool) -> bool: may this call go ahead right now?
            record_success / record_failure return nothing.
TOUCHES     The circuit_breakers table (migration 003). Every statement is a
            single atomic UPDATE/INSERT, so three workers racing stay
            consistent; the state is shared, which is why one worker
            discovering an outage protects the other two.
FAILS WHEN  Database errors propagate (the worker's retry loop handles
            blips). A missing row means "closed": healthy tools cost one
            SELECT per call and no writes.
"""

from relay.config import settings
from relay.db import pool


def allow(tool: str) -> bool:
    """True = the call may go ahead. False = circuit open, skip the call.

    When an OPEN breaker's waiting time has passed, exactly one caller wins
    the UPDATE below, becomes the probe (HALF_OPEN), and gets True; everyone
    else keeps getting False until the probe reports back. A probe whose
    worker died re-expires after another `breaker_open_seconds`, so a crash
    can't jam the breaker in HALF_OPEN forever.
    """
    wait = settings.breaker_open_seconds
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM circuit_breakers WHERE tool = %s", (tool,))
            row = cur.fetchone()
            if row is None or row[0] == "closed":
                return True

            # open or half_open: has it waited long enough for (another) probe?
            cur.execute(
                """
                UPDATE circuit_breakers
                SET state = 'half_open', updated_at = now()
                WHERE tool = %(tool)s
                  AND ( (state = 'open'
                         AND opened_at < now() - %(wait)s * interval '1 second')
                     OR (state = 'half_open'
                         AND updated_at < now() - %(wait)s * interval '1 second') )
                """,
                {"tool": tool, "wait": wait},
            )
            return cur.rowcount == 1  # 1 = this caller is the probe


def record_success(tool: str) -> None:
    """A call worked: close the breaker and forget the failures (if any)."""
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE circuit_breakers
            SET state = 'closed', consecutive_failures = 0,
                opened_at = NULL, updated_at = now()
            WHERE tool = %s AND (state <> 'closed' OR consecutive_failures > 0)
            """,
            (tool,),
        )  # healthy tool, no row or already clean -> updates nothing, writes nothing


def record_failure(tool: str) -> None:
    """A call failed: count it, and trip the breaker at the threshold.

    A failure while HALF_OPEN is the probe failing -> straight back to open.
    """
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO circuit_breakers (tool, consecutive_failures)
            VALUES (%(tool)s, 1)
            ON CONFLICT (tool) DO UPDATE SET
                consecutive_failures = circuit_breakers.consecutive_failures + 1,
                state = CASE
                    WHEN circuit_breakers.state = 'half_open'
                      OR circuit_breakers.consecutive_failures + 1 >= %(threshold)s
                    THEN 'open'
                    ELSE circuit_breakers.state
                END,
                opened_at = CASE
                    WHEN circuit_breakers.state = 'half_open'
                      OR circuit_breakers.consecutive_failures + 1 >= %(threshold)s
                    THEN now()
                    ELSE circuit_breakers.opened_at
                END,
                updated_at = now()
            """,
            {"tool": tool, "threshold": settings.breaker_threshold},
        )
