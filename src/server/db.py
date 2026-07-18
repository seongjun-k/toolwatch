"""toolwatch SQLite 접근 계층 (E1).

내장 sqlite3만 사용, 의존성 추가 없음. 커넥션은 요청마다 열고 닫는 방식으로만
쓴다 (풀링·WAL 튜닝 금지 — 확장계획.md §E1, 구현계획.md §8). 스키마 DDL은
확장계획.md §E1이 SSoT이며 여기서는 그대로 옮겨 적용만 한다.
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  uid          TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  student_id   TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS loans (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  tool          TEXT NOT NULL,
  uid           TEXT REFERENCES users(uid),
  out_at        TEXT NOT NULL,
  due_at        TEXT,
  returned_at   TEXT,
  overdue_logged INTEGER NOT NULL DEFAULT 0,
  unauth        INTEGER NOT NULL DEFAULT 0,
  cleared       INTEGER NOT NULL DEFAULT 0,
  snapshot_path TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  type      TEXT NOT NULL,
  tool      TEXT NOT NULL,
  uid       TEXT,
  loan_id   INTEGER REFERENCES loans(id),
  snapshot_path TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
  uid       TEXT NOT NULL REFERENCES users(uid),
  endpoint  TEXT NOT NULL,
  keys_json TEXT NOT NULL,
  PRIMARY KEY (uid, endpoint)
);

CREATE TABLE IF NOT EXISTS sent_notices (
  loan_id INTEGER NOT NULL REFERENCES loans(id),
  kind    TEXT NOT NULL,
  PRIMARY KEY (loan_id, kind)
);
"""


def get_conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        # 기존 DB(구 스키마)에 student_id 컬럼이 없으면 추가 — 이미 있으면 예외 무시
        try:
            conn.execute("ALTER TABLE users ADD COLUMN student_id TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        # 학번은 학생 로그인 키 — 중복 등록 차단 (NULL은 SQLite 유니크 인덱스에서 중복 허용)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_student_id ON users(student_id)"
        )
        conn.commit()
    finally:
        conn.close()


# --- users (구 CONFIG["uid_names"] 이관) ---

def list_users(conn):
    """uid -> name dict (attribute_out에 그대로 넘길 수 있는 형태)."""
    rows = conn.execute("SELECT uid, name FROM users").fetchall()
    return {row["uid"]: row["name"] for row in rows}


def add_user(conn, uid, name, student_id=None):
    conn.execute(
        "INSERT INTO users (uid, name, student_id) VALUES (?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, student_id=excluded.student_id",
        (uid, name, student_id),
    )
    conn.commit()


def list_users_full(conn):
    """uid, name, student_id를 모두 포함한 사용자 목록 (관리자 화면 표시용)."""
    return conn.execute("SELECT uid, name, student_id FROM users").fetchall()


def find_uid_by_student(conn, student_id, name):
    """학번+이름이 일치하는 사용자의 uid를 반환 (학생 로그인용). 없으면 None."""
    row = conn.execute(
        "SELECT uid FROM users WHERE student_id = ? AND name = ?",
        (student_id, name),
    ).fetchone()
    return row["uid"] if row else None


def delete_user(conn, uid):
    conn.execute("DELETE FROM users WHERE uid = ?", (uid,))
    conn.commit()


# --- loans ---

def insert_loan(conn, tool, uid, out_at, unauth, snapshot_path=None, due_at=None):
    cur = conn.execute(
        "INSERT INTO loans (tool, uid, out_at, unauth, snapshot_path, due_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tool, uid or None, out_at, int(unauth), snapshot_path, due_at),
    )
    conn.commit()
    return cur.lastrowid


def close_loan(conn, loan_id, returned_at):
    """IN 확정 시 호출부가 큐(FIFO)에서 꺼낸 가장 오래된 미반납 loan을 반납 처리한다
    (out_at 오름차순으로 큐를 쌓으므로 loan_id로 직접 지정해도 의미는 동일)."""
    conn.execute("UPDATE loans SET returned_at = ? WHERE id = ?", (returned_at, loan_id))
    conn.commit()


def mark_overdue(conn, loan_id):
    conn.execute("UPDATE loans SET overdue_logged = 1 WHERE id = ?", (loan_id,))
    conn.commit()


def clear_loan_warning(conn, loan_id):
    conn.execute("UPDATE loans SET cleared = 1 WHERE id = ?", (loan_id,))
    conn.commit()


def get_open_loans(conn):
    """returned_at IS NULL인 loan을 out_at 오름차순으로 반환 (서버 기동 시 rented 큐 복원용)."""
    return conn.execute(
        "SELECT * FROM loans WHERE returned_at IS NULL ORDER BY out_at ASC"
    ).fetchall()


def get_loans_by_uid(conn, uid, limit=30):
    """학생용(E2) 본인 대여 목록: 진행중+최근 반납 이력을 out_at 내림차순으로 반환.
    진행중/반납완료 분리와 개수 제한은 호출부(app.py)에서 처리한다."""
    return conn.execute(
        "SELECT * FROM loans WHERE uid = ? ORDER BY out_at DESC LIMIT ?",
        (uid, limit),
    ).fetchall()


def set_loan_due(conn, loan_id, uid, due_at):
    """uid까지 WHERE 조건에 넣어 본인 소유 loan만 갱신 (신뢰 경계 방어 — E2).
    반환값(갱신된 행 수)이 0이면 loan이 존재하지 않거나 본인 소유가 아님."""
    cur = conn.execute(
        "UPDATE loans SET due_at = ? WHERE id = ? AND uid = ?",
        (due_at, loan_id, uid),
    )
    conn.commit()
    return cur.rowcount


# --- events ---

def add_event(conn, event_type, tool, uid=None, loan_id=None, snapshot_path=None):
    conn.execute(
        "INSERT INTO events (type, tool, uid, loan_id, snapshot_path) VALUES (?, ?, ?, ?, ?)",
        (event_type, tool, uid or None, loan_id, snapshot_path),
    )
    conn.commit()


# --- push_subscriptions / sent_notices (E3) ---

def add_subscription(conn, uid, endpoint, keys_json):
    conn.execute(
        "INSERT INTO push_subscriptions (uid, endpoint, keys_json) VALUES (?, ?, ?) "
        "ON CONFLICT(uid, endpoint) DO UPDATE SET keys_json=excluded.keys_json",
        (uid, endpoint, keys_json),
    )
    conn.commit()


def remove_subscription(conn, endpoint):
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()


def get_subscriptions(conn, uid):
    return conn.execute(
        "SELECT uid, endpoint, keys_json FROM push_subscriptions WHERE uid = ?", (uid,)
    ).fetchall()


def has_notice(conn, loan_id, kind):
    row = conn.execute(
        "SELECT 1 FROM sent_notices WHERE loan_id = ? AND kind = ?", (loan_id, kind)
    ).fetchone()
    return row is not None


def mark_notice(conn, loan_id, kind):
    conn.execute(
        "INSERT OR IGNORE INTO sent_notices (loan_id, kind) VALUES (?, ?)", (loan_id, kind)
    )
    conn.commit()


def get_recent_events(conn, n=20):
    """대시보드 이력 표시용. 기존 CSV 컬럼명과 동일한 키로 반환해 템플릿 변경을 최소화한다."""
    rows = conn.execute(
        """
        SELECT events.ts AS ts, events.type AS type, events.tool AS tool,
               events.uid AS uid, users.name AS name, events.snapshot_path AS snapshot_path
        FROM events
        LEFT JOIN users ON users.uid = events.uid
        ORDER BY events.id DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    result = []
    for row in rows:
        path = row["snapshot_path"] or ""
        result.append({
            "시각": row["ts"],
            "이벤트": row["type"],
            "공구": row["tool"],
            "UID": row["uid"] or "",
            "이름": row["name"] or "",
            "snapshot_filename": Path(path).name if path else "",
        })
    return result
