"""Trusted controller's offline admission ledger for zero-cost provider requests."""
from __future__ import annotations

from opencognitiveos.contracts.models import DataClass
from opencognitiveos.errors import BudgetExceededError, PolicyDeniedError
from opencognitiveos.kernel.authority import ExecutionAuthority, _parse_utc


class CloudQuotaLedger:
    def __init__(self, authority: ExecutionAuthority, *, controller_capability: object):
        if controller_capability is None:
            raise ValueError("A controller capability is required.")
        self.authority = authority
        self._capability = controller_capability
        with authority.store.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS cloud_pools (
                pool TEXT PRIMARY KEY, capacity INTEGER NOT NULL CHECK(capacity > 0),
                expires TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS cloud_grants (
                task TEXT NOT NULL REFERENCES tasks(unique_key), provider TEXT NOT NULL,
                pool TEXT NOT NULL REFERENCES cloud_pools(pool), token_limit INTEGER NOT NULL,
                PRIMARY KEY(task,provider))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS cloud_reservations (
                task TEXT NOT NULL, request_id TEXT NOT NULL, provider TEXT NOT NULL,
                tokens INTEGER NOT NULL, execution_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                PRIMARY KEY(task,request_id),
                FOREIGN KEY(task,provider) REFERENCES cloud_grants(task,provider))""")

    def grant(self, *, capability, task, provider, pool, pool_tokens, task_tokens, expires):
        if capability is not self._capability:
            raise PolicyDeniedError("Cloud approval requires controller authority.")
        if (not provider or not pool or type(pool_tokens) is not int or type(task_tokens) is not int
                or not 0 < task_tokens <= pool_tokens or _parse_utc(expires) <= self.authority.clock.now_utc()):
            raise PolicyDeniedError("Invalid cloud quota terms.")
        with self.authority.store.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE unique_key=?", (task,)).fetchone()
            if row is None:
                raise PolicyDeniedError("Unknown task.")
            spec = self.authority._spec_from_row(row)
            if (not spec.cloud_approved or provider not in spec.allowed_providers
                    or spec.data_class != DataClass.PUBLIC or spec.budget.max_cost_usd != 0):
                raise PolicyDeniedError("Task does not authorize public zero-cost cloud work.")
            conn.execute("INSERT OR IGNORE INTO cloud_pools VALUES (?,?,?)", (pool, pool_tokens, expires))
            existing = conn.execute("SELECT capacity, expires FROM cloud_pools WHERE pool=?", (pool,)).fetchone()
            if tuple(existing) != (pool_tokens, expires):
                raise PolicyDeniedError("Pool terms conflict.")
            conn.execute("INSERT INTO cloud_grants VALUES (?,?,?,?)", (task, provider, pool, task_tokens))

    def reserve(self, *, capability, task, provider, request_id, max_tokens, guaranteed_cost_microusd,
                worker_id, execution_id, epoch):
        """Return True once. Unknown usage stays reserved; replay never permits a second dispatch."""
        if capability is not self._capability:
            raise PolicyDeniedError("Only the trusted provider adapter may attest request cost.")
        if (type(guaranteed_cost_microusd) is not int or guaranteed_cost_microusd != 0
                or type(max_tokens) is not int or max_tokens <= 0
                or not isinstance(request_id, str) or not 0 < len(request_id) <= 128):
            raise PolicyDeniedError("Missing bounded request or guaranteed zero cost.")
        with self.authority.store.transaction() as conn:
            row = conn.execute("""SELECT t.fencing_epoch,t.status,g.pool,g.token_limit,p.capacity,p.expires
                FROM tasks t JOIN cloud_grants g ON g.task=t.unique_key
                JOIN cloud_pools p ON p.pool=g.pool WHERE t.unique_key=? AND g.provider=?""",
                (task, provider)).fetchone()
            now = self.authority.clock.now_utc()
            if row is None or row["status"] != "RUNNING" or _parse_utc(row["expires"]) <= now:
                raise PolicyDeniedError("Inactive cloud grant.")
            self.authority._validate_active_lease(conn, unique_key=task, current_epoch=row["fencing_epoch"],
                worker_id=worker_id, execution_id=execution_id, epoch=epoch, now_dt=now)
            old = conn.execute("SELECT provider,tokens,execution_id,epoch FROM cloud_reservations WHERE task=? AND request_id=?",
                               (task, request_id)).fetchone()
            if old is not None:
                if tuple(old) != (provider, max_tokens, execution_id, epoch):
                    raise PolicyDeniedError("Conflicting reservation replay.")
                return False
            totals = conn.execute("""SELECT COALESCE(SUM(r.tokens),0),
                COALESCE(SUM(CASE WHEN r.task=? AND r.provider=? THEN r.tokens ELSE 0 END),0)
                FROM cloud_reservations r JOIN cloud_grants g ON g.task=r.task AND g.provider=r.provider
                WHERE g.pool=?""", (task, provider, row["pool"])).fetchone()
            if totals[0]+max_tokens > row["capacity"] or totals[1]+max_tokens > row["token_limit"]:
                raise BudgetExceededError("Cloud quota exhausted.")
            conn.execute("INSERT INTO cloud_reservations VALUES (?,?,?,?,?,?)",
                         (task, request_id, provider, max_tokens, execution_id, epoch))
            return True
