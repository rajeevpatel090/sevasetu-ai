# -*- coding: utf-8 -*-
"""
Database adapter for Sewa Setu.
Tries Postgres first (per config/env); falls back seamlessly to SQLite
when running locally without Postgres/Docker running.
"""

import os
import sqlite3
from datetime import datetime, date
import psycopg2

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "sewasetu_local.db")


class SQLiteCursorWrapper:
    def __init__(self, cur, conn):
        self.cur = cur
        self.conn = conn
        self._last_returning = None

    def execute(self, query, params=None):
        params = params or ()
        clean_params = []
        for p in params:
            if isinstance(p, (datetime, date)):
                clean_params.append(str(p))
            elif isinstance(p, (tuple, list)):
                pass
            else:
                clean_params.append(p)

        q = query
        # Handle "IN %s" with tuples
        if "IN %s" in q:
            for p in params:
                if isinstance(p, (tuple, list)):
                    placeholders = ", ".join(["?"] * len(p))
                    q = q.replace("IN %s", "IN (" + placeholders + ")", 1)
                    clean_params = []
                    for item in params:
                        if isinstance(item, (tuple, list)):
                            clean_params.extend(item)
                        elif isinstance(item, (datetime, date)):
                            clean_params.append(str(item))
                        else:
                            clean_params.append(item)
                    break

        self._last_returning = None
        if " RETURNING " in q:
            parts = q.split(" RETURNING ")
            sql_body = parts[0]
            ret_col = parts[1].strip()
            sql_body = sql_body.replace("%s", "?")
            self.cur.execute(sql_body, clean_params)
            if "INSERT INTO applications" in q:
                new_id = self.cur.lastrowid
                self._last_returning = [(new_id,)]
            elif "UPDATE applications" in q and "RETURNING application_no" in q:
                # Get the application_no of updated row
                self._last_returning = [("SSP26" + str(new_id if 'new_id' in locals() else 100000),)]
                # If app_id in params, fetch actual application_no
                for p in clean_params:
                    if isinstance(p, int):
                        c2 = self.conn.cursor()
                        c2.execute("SELECT application_no FROM applications WHERE id = ?", (p,))
                        r = c2.fetchone()
                        c2.close()
                        if r:
                            self._last_returning = [(r[0],)]
                        break
            return self

        q = q.replace("%s", "?")
        self.cur.execute(q, clean_params)
        return self

    def fetchone(self):
        if self._last_returning is not None:
            res = self._last_returning[0]
            self._last_returning = None
            return res
        row = self.cur.fetchone()
        return tuple(row) if row is not None else None

    def fetchall(self):
        rows = self.cur.fetchall()
        return [tuple(r) for r in rows]

    @property
    def rowcount(self):
        return self.cur.rowcount

    def close(self):
        self.cur.close()


class SQLiteConnWrapper:
    def __init__(self, conn):
        self.conn = conn

    def cursor(self):
        return SQLiteCursorWrapper(self.conn.cursor(), self.conn)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def init_sqlite_db(conn):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        application_no  VARCHAR(20),
        applicant_name  VARCHAR(100),
        mobile          VARCHAR(15),
        dob             DATE,
        gender          VARCHAR(10),
        marital_status  VARCHAR(20),
        husband_name    VARCHAR(100),
        husband_employer VARCHAR(100),
        village         VARCHAR(100),
        block           VARCHAR(50),
        bank_account    VARCHAR(30),
        ifsc            VARCHAR(15),
        doc_path        VARCHAR(200),
        status          VARCHAR(20) DEFAULT 'PENDING',
        submitted_at    TIMESTAMP,
        decided_at      TIMESTAMP,
        decided_by      VARCHAR(50)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS portal_users (
        mobile        VARCHAR(15) PRIMARY KEY,
        password_hash VARCHAR(80)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS otps (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        mobile     VARCHAR(15),
        code       VARCHAR(6),
        created_at TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        action         VARCHAR(20),
        actor          VARCHAR(50),
        note           VARCHAR(500),
        at             TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_keys (
        kid         VARCHAR(32) PRIMARY KEY,
        private_pem TEXT,
        created_at  TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_clients (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id     VARCHAR(300) UNIQUE,
        client_name   VARCHAR(100),
        redirect_uris TEXT,
        secret_hash   VARCHAR(64),
        kind          VARCHAR(10),
        created_at    TIMESTAMP,
        fetched_at    TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_requests (
        id             VARCHAR(64) PRIMARY KEY,
        params         TEXT,
        subject        VARCHAR(80),
        role           VARCHAR(10),
        pending_mobile VARCHAR(15),
        created_at     TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash      VARCHAR(64) PRIMARY KEY,
        client_id      VARCHAR(300),
        redirect_uri   VARCHAR(500),
        scope          VARCHAR(20),
        code_challenge VARCHAR(128),
        resource       VARCHAR(300),
        subject        VARCHAR(80),
        role           VARCHAR(10),
        created_at     TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token_hash  VARCHAR(64) PRIMARY KEY,
        client_id   VARCHAR(300),
        client_name VARCHAR(100),
        subject     VARCHAR(80),
        role        VARCHAR(10),
        scope       VARCHAR(20),
        resource    VARCHAR(300),
        created_at  TIMESTAMP,
        expires_at  TIMESTAMP,
        revoked_at  TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oauth_program_tokens (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        jti        VARCHAR(32) UNIQUE,
        label      VARCHAR(100),
        subject    VARCHAR(80),
        audience   VARCHAR(300),
        issued_by  VARCHAR(50),
        created_at TIMESTAMP,
        expires_at TIMESTAMP,
        revoked_at TIMESTAMP
    );
    """)
    conn.commit()


def get_db(config=None):
    """Try PostgreSQL; if unavailable, use local SQLite fallback."""
    if config:
        try:
            return psycopg2.connect(
                host=os.environ.get("DB_HOST") or config.get("database", "host"),
                port=os.environ.get("DB_PORT") or config.get("database", "port"),
                dbname=os.environ.get("DB_NAME") or config.get("database", "name"),
                user=os.environ.get("DB_USER") or config.get("database", "user"),
                password=os.environ.get("DB_PASSWORD") or config.get("database", "password"),
                connect_timeout=2
            )
        except Exception:
            pass

    # SQLite fallback
    conn = sqlite3.connect(DB_FILE)
    init_sqlite_db(conn)
    return SQLiteConnWrapper(conn)
