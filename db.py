# db.py
import aiosqlite
import time
import json

DB_PATH = "torn_companies.db"

INIT_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS companies (
  company_id INTEGER PRIMARY KEY,
  api_key TEXT NOT NULL,
  name TEXT,
  alert_channel_id INTEGER,   -- addiction/inactivity alerts
  report_channel_id INTEGER,  -- optional summaries/reports
  last_updated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS company_profile (
  company_id INTEGER PRIMARY KEY,
  company_type INTEGER,
  rating INTEGER,
  director INTEGER,
  employees_hired INTEGER,
  employees_capacity INTEGER,
  daily_income INTEGER,
  daily_customers INTEGER,
  weekly_income INTEGER,
  weekly_customers INTEGER,
  days_old INTEGER,
  updated_at INTEGER,
  FOREIGN KEY(company_id) REFERENCES companies(company_id)
);

CREATE TABLE IF NOT EXISTS company_detailed (
  company_id INTEGER PRIMARY KEY,
  company_funds INTEGER,
  company_bank INTEGER,
  popularity INTEGER,
  efficiency INTEGER,
  environment INTEGER,
  trains_available INTEGER,
  advertising_budget INTEGER,
  upgrades_json TEXT,
  value INTEGER,
  updated_at INTEGER,
  FOREIGN KEY(company_id) REFERENCES companies(company_id)
);

CREATE TABLE IF NOT EXISTS company_stock (
  company_id INTEGER PRIMARY KEY,
  stock_json TEXT,
  updated_at INTEGER,
  FOREIGN KEY(company_id) REFERENCES companies(company_id)
);

CREATE TABLE IF NOT EXISTS employees (
  employee_id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL,
  name TEXT,
  position TEXT,
  days_in_company INTEGER,
  wage INTEGER,
  manual_labor INTEGER,
  intelligence INTEGER,
  endurance INTEGER,
  eff_working_stats INTEGER,
  eff_settled_in INTEGER,
  eff_merits INTEGER,
  eff_director_education INTEGER,
  eff_inactivity INTEGER,
  eff_total INTEGER,
  last_action_status TEXT,
  last_action_relative TEXT,
  status_state TEXT,
  status_desc TEXT,
  status_color TEXT,
  status_until INTEGER,
  addiction REAL,              -- numeric if available from user endpoint
  updated_at INTEGER,
  FOREIGN KEY(company_id) REFERENCES companies(company_id)
);

CREATE TABLE IF NOT EXISTS company_news (
  id TEXT PRIMARY KEY,         -- Torn gives unique keys like "ND0It8..."
  company_id INTEGER NOT NULL,
  news_text TEXT,
  timestamp INTEGER,
  seen INTEGER DEFAULT 0,
  FOREIGN KEY(company_id) REFERENCES companies(company_id)
);

-- Track what alerts we've already sent so we don't spam the same level
CREATE TABLE IF NOT EXISTS alert_state (
  company_id INTEGER NOT NULL,
  employee_id INTEGER NOT NULL,
  inactivity_level INTEGER DEFAULT 0,  -- 0 none, 1 >=18h, 2 >=24h, 3 >=48h
  addiction_flag INTEGER DEFAULT 0,    -- 0 not flagged, 1 flagged
  PRIMARY KEY (company_id, employee_id)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_links (
  discord_id TEXT PRIMARY KEY,     -- snowflake as string
  torn_id INTEGER,
  torn_name TEXT,
  company_id INTEGER,              -- last known company they were seen in
  verified INTEGER DEFAULT 0,      -- 1 if confirmed by a mod or via command
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_member_links_torn_id ON member_links(torn_id);


CREATE TABLE IF NOT EXISTS company_role_map (
  company_id INTEGER PRIMARY KEY,
  role_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS company_position_role_map (
  company_id INTEGER NOT NULL,
  position TEXT NOT NULL,
  role_id INTEGER NOT NULL,
  PRIMARY KEY (company_id, position)
);
CREATE INDEX IF NOT EXISTS idx_emp_company_position ON employees(company_id, position);

CREATE TABLE IF NOT EXISTS company_thresholds (
  company_id INTEGER PRIMARY KEY,
  inactivity_ping_hours REAL DEFAULT 18.0,
  inactivity_warn_hours REAL DEFAULT 24.0,
  inactivity_lastwarn_hours REAL DEFAULT 48.0,
  addiction_alert BOOLEAN DEFAULT 1,
  FOREIGN KEY(company_id) REFERENCES companies(company_id)
);

-- Normalize current stock into rows (one per item)
CREATE TABLE IF NOT EXISTS company_stock_items (
  company_id INTEGER NOT NULL,
  item_name TEXT NOT NULL,
  cost INTEGER,
  rrp INTEGER,
  price INTEGER,
  in_stock INTEGER,
  on_order INTEGER,
  sold_amount INTEGER,
  sold_worth INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (company_id, item_name)
);

-- Metrics history (periodic snapshots)
CREATE TABLE IF NOT EXISTS company_metrics_history (
  company_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                 -- snapshot time (epoch)
  employees_hired INTEGER,
  employees_capacity INTEGER,
  daily_income INTEGER,
  daily_customers INTEGER,
  weekly_income INTEGER,
  weekly_customers INTEGER,
  popularity INTEGER,
  efficiency INTEGER,
  environment INTEGER,
  trains_available INTEGER,
  company_funds INTEGER,
  company_bank INTEGER,
  value INTEGER,
  PRIMARY KEY (company_id, ts)
);

-- Stock history (one row per item per snapshot)
CREATE TABLE IF NOT EXISTS company_stock_history (
  company_id INTEGER NOT NULL,
  item_name TEXT NOT NULL,
  ts INTEGER NOT NULL,
  price INTEGER,
  in_stock INTEGER,
  on_order INTEGER,
  sold_amount INTEGER,
  sold_worth INTEGER,
  PRIMARY KEY (company_id, item_name, ts)
);

-- Effectiveness history per employee (periodic snapshots)
CREATE TABLE IF NOT EXISTS employee_effectiveness_history (
  employee_id INTEGER NOT NULL,
  company_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  working_stats INTEGER,
  settled_in INTEGER,
  merits INTEGER,
  director_education INTEGER,
  inactivity INTEGER,
  total INTEGER,
  PRIMARY KEY (employee_id, ts)
);

-- Optional: last action/inactivity history (trend)
CREATE TABLE IF NOT EXISTS employee_last_action_history (
  employee_id INTEGER NOT NULL,
  company_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  last_action_status TEXT,
  last_action_relative TEXT,
  PRIMARY KEY (employee_id, ts)
);

CREATE TABLE IF NOT EXISTS company_report_state (
  company_id INTEGER PRIMARY KEY,
  last_report_date TEXT   -- 'YYYY-MM-DD' in America/Detroit
);


CREATE INDEX IF NOT EXISTS idx_metrics_hist_company_ts ON company_metrics_history(company_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_stock_hist_company_ts   ON company_stock_history(company_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_eff_hist_company_ts     ON employee_effectiveness_history(company_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_la_hist_company_ts      ON employee_last_action_history(company_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_news_company_ts ON company_news(company_id, timestamp);


"""

async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, coltype: str):
    """If a column is missing, add it with ALTER TABLE. Safe to call repeatedly."""
    cur = await conn.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    if column not in cols:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        await _ensure_column(db, "employees", "eff_management", "INTEGER")
        await _ensure_column(db, "employees", "addiction", "REAL")
        await _ensure_column(db, "company_thresholds", "addiction_threshold", "INTEGER DEFAULT 1")
        await _ensure_column(db, "alert_state", "addiction_last_value", "INTEGER")
        await _ensure_column(db, "alert_state", "addiction_last_ts", "INTEGER")

        await db.commit()

# ----- companies -----
async def upsert_company(company_id: int, api_key: str, name: str | None = None,
                         alert_channel_id: int | None = None,
                         report_channel_id: int | None = None):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO companies (company_id, api_key, name, alert_channel_id, report_channel_id, last_updated)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
          api_key=excluded.api_key,
          name=COALESCE(excluded.name, companies.name),
          alert_channel_id=COALESCE(excluded.alert_channel_id, companies.alert_channel_id),
          report_channel_id=COALESCE(excluded.report_channel_id, companies.report_channel_id)
        """, (company_id, api_key, name, alert_channel_id, report_channel_id, now))
        await db.commit()

async def get_companies():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM companies ORDER BY company_id")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def get_company(company_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def set_company_name(company_id: int, name: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE companies SET name = ? WHERE company_id = ?", (name, company_id))
        await db.commit()

# ----- profile/detailed/stock/employees/news -----
async def update_company_profile(company_id: int, profile: dict):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_profile (company_id, company_type, rating, director, employees_hired,
          employees_capacity, daily_income, daily_customers, weekly_income, weekly_customers, days_old, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
          company_type=excluded.company_type, rating=excluded.rating, director=excluded.director,
          employees_hired=excluded.employees_hired, employees_capacity=excluded.employees_capacity,
          daily_income=excluded.daily_income, daily_customers=excluded.daily_customers,
          weekly_income=excluded.weekly_income, weekly_customers=excluded.weekly_customers,
          days_old=excluded.days_old, updated_at=excluded.updated_at
        """, (
            company_id,
            profile.get("company_type"), profile.get("rating"), profile.get("director"),
            profile.get("employees_hired"), profile.get("employees_capacity"),
            profile.get("daily_income"), profile.get("daily_customers"),
            profile.get("weekly_income"), profile.get("weekly_customers"),
            profile.get("days_old"), now
        ))
        await db.commit()

async def update_company_detailed(company_id: int, detailed: dict):
    now = int(time.time())
    upgrades_json = json.dumps(detailed.get("upgrades") or {})
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_detailed (company_id, company_funds, company_bank, popularity, efficiency,
          environment, trains_available, advertising_budget, upgrades_json, value, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
          company_funds=excluded.company_funds, company_bank=excluded.company_bank,
          popularity=excluded.popularity, efficiency=excluded.efficiency, environment=excluded.environment,
          trains_available=excluded.trains_available, advertising_budget=excluded.advertising_budget,
          upgrades_json=excluded.upgrades_json, value=excluded.value, updated_at=excluded.updated_at
        """, (
            company_id,
            detailed.get("company_funds"), detailed.get("company_bank"), detailed.get("popularity"),
            detailed.get("efficiency"), detailed.get("environment"), detailed.get("trains_available"),
            detailed.get("advertising_budget"), upgrades_json, detailed.get("value"), now
        ))
        await db.commit()

async def update_company_stock(company_id: int, stock_map: dict | None):
    now = int(time.time())
    stock_json = json.dumps(stock_map or {})
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_stock (company_id, stock_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
          stock_json=excluded.stock_json, updated_at=excluded.updated_at
        """, (company_id, stock_json, now))
        await db.commit()

async def upsert_employees(company_id: int, employees_dict: dict):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        for emp_id_str, emp in (employees_dict or {}).items():
            emp_id = int(emp_id_str)
            eff = emp.get("effectiveness", {}) or {}
            la = emp.get("last_action", {}) or {}
            st = emp.get("status", {}) or {}
            eff_addiction_raw = eff.get("addiction")
            addiction_value = 0.0
            if isinstance(eff_addiction_raw, (int, float)):
                addiction_value = float(abs(eff_addiction_raw))
            await db.execute("""
            INSERT INTO employees (employee_id, company_id, name, position, days_in_company, wage,
              manual_labor, intelligence, endurance,
              eff_working_stats, eff_settled_in, eff_merits, eff_director_education, eff_inactivity, eff_total,
              eff_management,
              last_action_status, last_action_relative,
              status_state, status_desc, status_color, status_until,
              addiction, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET
              company_id=excluded.company_id,
              name=excluded.name, position=excluded.position, days_in_company=excluded.days_in_company, wage=excluded.wage,
              manual_labor=excluded.manual_labor, intelligence=excluded.intelligence, endurance=excluded.endurance,
              eff_working_stats=excluded.eff_working_stats, eff_settled_in=excluded.eff_settled_in, eff_merits=excluded.eff_merits,
              eff_director_education=excluded.eff_director_education, eff_inactivity=excluded.eff_inactivity, eff_total=excluded.eff_total,
              eff_management=excluded.eff_management,
              last_action_status=excluded.last_action_status, last_action_relative=excluded.last_action_relative,
              status_state=excluded.status_state, status_desc=excluded.status_desc, status_color=excluded.status_color, status_until=excluded.status_until,
              addiction=excluded.addiction, updated_at=excluded.updated_at
            """, (
                emp_id, company_id,
                emp.get("name"), emp.get("position"), emp.get("days_in_company"), emp.get("wage"),
                emp.get("manual_labor"), emp.get("intelligence"), emp.get("endurance"),
                eff.get("working_stats"), eff.get("settled_in"), eff.get("merits"),
                # keep your old 'director_education' if present; Torn payload may use 'management' instead
                eff.get("director_education", None if eff.get("management") is not None else None),
                eff.get("inactivity"), eff.get("total"),
                eff.get("management"),  # <-- new column
                la.get("status"), la.get("relative"),
                st.get("state"), st.get("description"), st.get("color"), st.get("until"),
                addiction_value,  # <-- derived from effectiveness
                now
            ))
        await db.commit()

async def update_employee_addiction(employee_id: int, addiction_value: float | None):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE employees SET addiction = ?, updated_at = ? WHERE employee_id = ?",
            (addiction_value, now, employee_id)
        )
        await db.commit()

async def upsert_news(company_id: int, news_map: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        for news_id, obj in (news_map or {}).items():
            await db.execute("""
            INSERT INTO company_news (id, company_id, news_text, timestamp, seen)
            VALUES (?, ?, ?, ?, COALESCE((SELECT seen FROM company_news WHERE id = ?), 0))
            ON CONFLICT(id) DO UPDATE SET
              company_id=excluded.company_id,
              news_text=excluded.news_text,
              timestamp=excluded.timestamp
            """, (news_id, company_id, obj.get("news"), obj.get("timestamp"), news_id))
        await db.commit()

async def get_employees_by_company(company_id: int, limit: int | None = None):
    sql = "SELECT * FROM employees WHERE company_id = ? ORDER BY name COLLATE NOCASE"
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, (company_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def get_employee_ids_by_company(company_id: int) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT employee_id FROM employees WHERE company_id = ?", (company_id,))
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

async def get_unseen_news(company_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM company_news WHERE company_id = ? AND seen = 0 ORDER BY timestamp ASC",
                               (company_id,))
        return [dict(r) for r in await cur.fetchall()]

async def mark_news_seen(ids: list[str]):
    if not ids:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany("UPDATE company_news SET seen = 1 WHERE id = ?", [(i,) for i in ids])
        await db.commit()

# ----- alert state -----
async def get_alert_state(company_id: int, employee_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT company_id, employee_id, inactivity_level, addiction_flag,
                   addiction_last_value, addiction_last_ts
            FROM alert_state
            WHERE company_id = ? AND employee_id = ?
        """, (company_id, employee_id))
        row = await cur.fetchone()
        if not row:
            return {"company_id": company_id, "employee_id": employee_id,
                    "inactivity_level": 0, "addiction_flag": 0,
                    "addiction_last_value": None, "addiction_last_ts": None}
        d = dict(row)
        d["inactivity_level"] = int(d.get("inactivity_level") or 0)
        d["addiction_flag"]   = int(d.get("addiction_flag") or 0)
        d["addiction_last_value"] = None if d.get("addiction_last_value") is None else int(d["addiction_last_value"])
        d["addiction_last_ts"]    = None if d.get("addiction_last_ts") is None else int(d["addiction_last_ts"])
        return d


async def set_inactivity_level(company_id: int, employee_id: int, level: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO alert_state (company_id, employee_id, inactivity_level, addiction_flag)
        VALUES (?, ?, ?, COALESCE((SELECT addiction_flag FROM alert_state WHERE company_id = ? AND employee_id = ?), 0))
        ON CONFLICT(company_id, employee_id) DO UPDATE SET inactivity_level = excluded.inactivity_level
        """, (company_id, employee_id, level, company_id, employee_id))
        await db.commit()

async def set_addiction_flag(company_id: int, employee_id: int, flagged: bool,
                             last_value: int | None = None, last_ts: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO alert_state (company_id, employee_id, inactivity_level, addiction_flag,
                                 addiction_last_value, addiction_last_ts)
        VALUES (?, ?, 0, ?, ?, ?)
        ON CONFLICT(company_id, employee_id) DO UPDATE SET
          addiction_flag = excluded.addiction_flag,
          addiction_last_value = COALESCE(excluded.addiction_last_value, alert_state.addiction_last_value),
          addiction_last_ts = COALESCE(excluded.addiction_last_ts, alert_state.addiction_last_ts)
        """, (company_id, employee_id, 1 if flagged else 0, last_value, last_ts))
        await db.commit()
# ----- thresholds -----

async def get_thresholds(company_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT inactivity_ping_hours, inactivity_warn_hours, inactivity_lastwarn_hours,
                   addiction_alert, addiction_threshold
            FROM company_thresholds WHERE company_id = ?
        """, (company_id,))
        row = await cur.fetchone()
        if not row:
            return {
                "inactivity_ping_hours": 18.0,
                "inactivity_warn_hours": 24.0,
                "inactivity_lastwarn_hours": 48.0,
                "addiction_alert": 1,
                "addiction_threshold": 1,
            }
        d = dict(row)
        return {
            "inactivity_ping_hours": float(d["inactivity_ping_hours"]),
            "inactivity_warn_hours": float(d["inactivity_warn_hours"]),
            "inactivity_lastwarn_hours": float(d["inactivity_lastwarn_hours"]),
            "addiction_alert": int(d["addiction_alert"]),
            "addiction_threshold": int(d["addiction_threshold"] if d["addiction_threshold"] is not None else 1),
        }


async def set_thresholds(company_id: int,
                         ping_h: float | None,
                         warn_h: float | None,
                         lastwarn_h: float | None,
                         addiction_alert: bool | None,
                         addiction_threshold: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_thresholds (company_id, inactivity_ping_hours, inactivity_warn_hours,
                                        inactivity_lastwarn_hours, addiction_alert, addiction_threshold)
        VALUES (?, COALESCE(?, 18.0), COALESCE(?, 24.0), COALESCE(?, 48.0),
                COALESCE(?, 1), COALESCE(?, 1))
        ON CONFLICT(company_id) DO UPDATE SET
          inactivity_ping_hours = COALESCE(?, company_thresholds.inactivity_ping_hours),
          inactivity_warn_hours = COALESCE(?, company_thresholds.inactivity_warn_hours),
          inactivity_lastwarn_hours = COALESCE(?, company_thresholds.inactivity_lastwarn_hours),
          addiction_alert = COALESCE(?, company_thresholds.addiction_alert),
          addiction_threshold = COALESCE(?, company_thresholds.addiction_threshold)
        """, (
            company_id,                       # INSERT
            ping_h, warn_h, lastwarn_h,
            (1 if (addiction_alert is None or addiction_alert) else 0),
            addiction_threshold,
            # UPDATE
            ping_h, warn_h, lastwarn_h,
            (1 if addiction_alert else 0) if addiction_alert is not None else None,
            addiction_threshold
        ))
        await db.commit()



import time
# --- Member links ---
async def upsert_member_link(discord_id: int, torn_id: int | None, torn_name: str | None,
                             company_id: int | None, verified: bool):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO member_links (discord_id, torn_id, torn_name, company_id, verified, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
          torn_id=COALESCE(excluded.torn_id, member_links.torn_id),
          torn_name=COALESCE(excluded.torn_name, member_links.torn_name),
          company_id=COALESCE(excluded.company_id, member_links.company_id),
          verified=MAX(member_links.verified, excluded.verified),
          updated_at=excluded.updated_at
        """, (str(discord_id), torn_id, torn_name, company_id, 1 if verified else 0, now))
        await db.commit()

async def get_member_link_by_discord(discord_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM member_links WHERE discord_id = ?", (str(discord_id),))
        row = await cur.fetchone()
        return dict(row) if row else None

async def get_employee_by_name_ci(torn_name: str):
    """Case-insensitive exact-name match across all companies; returns first match (dict) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM employees WHERE LOWER(name) = LOWER(?) LIMIT 1
        """, (torn_name,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def get_employee_by_id(emp_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM employees WHERE employee_id = ?", (emp_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

# --- Company ↔ Role map ---
async def set_company_role_map(company_id: int, role_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_role_map (company_id, role_id)
        VALUES (?, ?)
        ON CONFLICT(company_id) DO UPDATE SET role_id = excluded.role_id
        """, (company_id, role_id))
        await db.commit()

async def get_company_role_id(company_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role_id FROM company_role_map WHERE company_id = ?", (company_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def get_member_link_by_torn_id(torn_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM member_links WHERE torn_id = ? LIMIT 1",
            (torn_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_company_role_maps() -> list[tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT company_id, role_id FROM company_role_map")
        rows = await cur.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]


async def set_position_role_map(company_id: int, position: str, role_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_position_role_map (company_id, position, role_id)
        VALUES (?, ?, ?)
        ON CONFLICT(company_id, position) DO UPDATE SET role_id = excluded.role_id
        """, (company_id, position, role_id))
        await db.commit()

async def delete_position_role_map(company_id: int, position: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM company_position_role_map WHERE company_id = ? AND position = ?",
                         (company_id, position))
        await db.commit()

async def get_position_role_id(company_id: int, position: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role_id FROM company_position_role_map WHERE company_id = ? AND position = ?",
                               (company_id, position))
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def get_all_position_role_maps(company_id: int) -> list[tuple[str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute("SELECT position, role_id FROM company_position_role_map WHERE company_id = ?",
                                (company_id,))
        rows = await rows.fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]

async def get_all_company_role_maps() -> list[tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT company_id, role_id FROM company_role_map")
        rows = await cur.fetchall()
        return [(int(r[0]), int(r[1])) for r in rows]

async def get_employee_work_stats(company_id: int | None = None, limit: int = 25) -> list[dict]:
    """
    Return employee name/ID, company_id, manual_labor, intelligence, endurance,
    and a computed total = ML+INT+END. If company_id is None, returns across all companies.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if company_id is None:
            sql = """
            SELECT
              e.employee_id,
              e.company_id,
              e.name,
              COALESCE(e.manual_labor, 0) AS manual_labor,
              COALESCE(e.intelligence, 0) AS intelligence,
              COALESCE(e.endurance, 0) AS endurance,
              (COALESCE(e.manual_labor,0)+COALESCE(e.intelligence,0)+COALESCE(e.endurance,0)) AS total_ws
            FROM employees e
            ORDER BY total_ws DESC
            LIMIT ?
            """
            cur = await db.execute(sql, (int(limit),))
        else:
            sql = """
            SELECT
              e.employee_id,
              e.company_id,
              e.name,
              COALESCE(e.manual_labor, 0) AS manual_labor,
              COALESCE(e.intelligence, 0) AS intelligence,
              COALESCE(e.endurance, 0) AS endurance,
              (COALESCE(e.manual_labor,0)+COALESCE(e.intelligence,0)+COALESCE(e.endurance,0)) AS total_ws
            FROM employees e
            WHERE e.company_id = ?
            ORDER BY total_ws DESC
            LIMIT ?
            """
            cur = await db.execute(sql, (int(company_id), int(limit)))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


import time, json, aiosqlite

async def replace_company_stock_items(company_id: int, stock_map: dict):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        # Clear existing items for this company, then insert fresh
        await db.execute("DELETE FROM company_stock_items WHERE company_id = ?", (company_id,))
        for name, item in (stock_map or {}).items():
            await db.execute("""
            INSERT INTO company_stock_items (company_id, item_name, cost, rrp, price, in_stock, on_order, sold_amount, sold_worth, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                company_id, name,
                item.get("cost"), item.get("rrp"), item.get("price"),
                item.get("in_stock"), item.get("on_order"),
                item.get("sold_amount"), item.get("sold_worth"),
                now
            ))
        await db.commit()

async def insert_company_metrics_snapshot(company_id: int, profile: dict | None, detailed: dict | None):
    now = int(time.time())
    p = profile or {}
    d = detailed or {}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT OR IGNORE INTO company_metrics_history
        (company_id, ts, employees_hired, employees_capacity, daily_income, daily_customers, weekly_income, weekly_customers,
         popularity, efficiency, environment, trains_available, company_funds, company_bank, value)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            company_id, now,
            p.get("employees_hired"), p.get("employees_capacity"),
            p.get("daily_income"), p.get("daily_customers"),
            p.get("weekly_income"), p.get("weekly_customers"),
            d.get("popularity"), d.get("efficiency"), d.get("environment"),
            d.get("trains_available"), d.get("company_funds"), d.get("company_bank"), d.get("value")
        ))
        await db.commit()

async def insert_company_stock_snapshot(company_id: int, stock_map: dict):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        for name, item in (stock_map or {}).items():
            await db.execute("""
            INSERT OR IGNORE INTO company_stock_history
            (company_id, item_name, ts, price, in_stock, on_order, sold_amount, sold_worth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                company_id, name, now,
                item.get("price"), item.get("in_stock"), item.get("on_order"),
                item.get("sold_amount"), item.get("sold_worth"),
            ))
        await db.commit()

async def insert_employee_effectiveness_snapshots(company_id: int, employees_dict: dict):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        for emp_id_str, emp in (employees_dict or {}).items():
            emp_id = int(emp_id_str)
            eff = emp.get("effectiveness", {}) or {}
            await db.execute("""
            INSERT OR IGNORE INTO employee_effectiveness_history
            (employee_id, company_id, ts, working_stats, settled_in, merits, director_education, inactivity, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                emp_id, company_id, now,
                eff.get("working_stats"), eff.get("settled_in"), eff.get("merits"),
                eff.get("director_education"), eff.get("inactivity"), eff.get("total")
            ))
            la = emp.get("last_action") or {}
            await db.execute("""
            INSERT OR IGNORE INTO employee_last_action_history
            (employee_id, company_id, ts, last_action_status, last_action_relative)
            VALUES (?, ?, ?, ?, ?)
            """, (emp_id, company_id, now, la.get("status"), la.get("relative")))
        await db.commit()

# Convenience getters for commands
async def get_current_stock(company_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT item_name, price, in_stock, on_order, sold_amount, sold_worth, updated_at
            FROM company_stock_items WHERE company_id = ? ORDER BY item_name
        """, (company_id,))
        return [dict(r) for r in await cur.fetchall()]

async def get_recent_metrics(company_id: int, limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM company_metrics_history WHERE company_id = ? ORDER BY ts DESC LIMIT ?
        """, (company_id, limit))
        return [dict(r) for r in await cur.fetchall()]

async def get_employee_eff_history(employee_id: int, limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM employee_effectiveness_history WHERE employee_id = ? ORDER BY ts DESC LIMIT ?
        """, (employee_id, limit))
        return [dict(r) for r in await cur.fetchall()]

async def get_employee_effectiveness_rank(company_id: int | None = None, limit: int = 25, sort_by: str = "total") -> list[dict]:
    """
    Returns rows ordered by chosen effectiveness metric.
    sort_by ∈ {"total","working","settled","merits","director","inactivity"}
    """
    col_map = {
        "total": "eff_total",
        "working": "eff_working_stats",
        "settled": "eff_settled_in",
        "merits": "eff_merits",
        "director": "eff_director_education",
        "inactivity": "eff_inactivity"
    }
    sort_col = col_map.get(sort_by, "eff_total")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if company_id is None:
            sql = f"""
            SELECT employee_id, company_id, name,
                   eff_working_stats, eff_settled_in, eff_merits, eff_director_education, eff_inactivity, eff_total
            FROM employees
            ORDER BY {sort_col} DESC NULLS LAST, name COLLATE NOCASE
            LIMIT ?
            """
            cur = await db.execute(sql, (int(limit),))
        else:
            sql = f"""
            SELECT employee_id, company_id, name,
                   eff_working_stats, eff_settled_in, eff_merits, eff_director_education, eff_inactivity, eff_total
            FROM employees
            WHERE company_id = ?
            ORDER BY {sort_col} DESC NULLS LAST, name COLLATE NOCASE
            LIMIT ?
            """
            cur = await db.execute(sql, (int(company_id), int(limit)))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def get_last_report_date(company_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT last_report_date FROM company_report_state WHERE company_id = ?", (company_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_last_report_date(company_id: int, ymd: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO company_report_state (company_id, last_report_date)
        VALUES (?, ?)
        ON CONFLICT(company_id) DO UPDATE SET last_report_date = excluded.last_report_date
        """, (company_id, ymd))
        await db.commit()
