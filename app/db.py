"""SQLite 连接管理与建库。

容器启动时调用 init_db() 建库建表；应用通过 FastAPI 依赖拿到连接。
为支持事务回滚，连接使用显式 BEGIN/COMMIT/ROLLBACK（autocommit 隔离级别）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    id      TEXT PRIMARY KEY,
    -- 乐观并发版本：创建时为 0，每次成功的批量转移触碰后加 1（同批多次触碰只加 1）
    version INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 容器中来自每个祖先初始样品的“绝对体积份额”。
-- 初始样品登记后占 1 行（source_id = 自身）；空容器为 0 行。
-- Decimal 以字符串存储，杜绝浮点误差。
CREATE TABLE IF NOT EXISTS components (
    container_id TEXT NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL,
    amount       TEXT NOT NULL,
    PRIMARY KEY (container_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_components_source ON components(source_id);

-- POST /transfers/batch 的幂等记录：仅在批次成功提交时，与容器扣减/版本递增
-- 写入同一个事务。重放（同键同请求体）直接返回 response_body，不再扣减体积。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    -- 首次成功请求的原始请求体指纹（SHA-256 hex）；同键但指纹不同 -> 409
    request_sha256  TEXT NOT NULL,
    -- 首次响应的 HTTP 状态码与完整响应体（compact JSON 原文，逐字节重放）
    status_code     INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created INTEGER NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- --------------------------------------------------------------------------- --
-- 批次审计哈希链（只追加）
--
-- 每个成功提交的批量转移在其写事务内追加一行：seq 从 1 连续递增，prev_hash
-- 指向前一条记录的 record_hash（首条为 64 个 0），record_hash =
-- SHA-256(规范化 JSON(payload 含 seq/prev_hash/summary/affected_containers/
-- versions))。payload 原样落库，校验时重算哈希并重放链即可发现任何删改。
CREATE TABLE IF NOT EXISTS audit_records (
    seq            INTEGER PRIMARY KEY,
    prev_hash      TEXT NOT NULL,
    record_hash    TEXT NOT NULL UNIQUE,
    -- 冗余列，便于列表页展示与一致性校验
    transfer_count INTEGER NOT NULL,
    -- 当初参与哈希的规范化 JSON 原文（created 不参与哈希）
    payload        TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 批次 -> 受影响容器的索引（用于“按容器查询审计记录”）。每批次对每个
-- 受影响容器恰一行，内容与 audit_records.payload 中 affected_containers 一致，
-- 整链校验时会交叉核对。
CREATE TABLE IF NOT EXISTS audit_containers (
    seq          INTEGER NOT NULL REFERENCES audit_records(seq),
    container_id TEXT NOT NULL,
    PRIMARY KEY (seq, container_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_containers_container
    ON audit_containers(container_id, seq);
"""

# 只追加保护：拒绝任何通过 SQL 层对审计表的 UPDATE / DELETE。
# 触发器无法被普通 DML 绕过（INSERT 是唯一允许的写操作）；审计写入随批次事务，
# 整批回滚时 INSERT 随之撤销，因此不需要、也不允许事后删除。
# 测试需要清库时使用 reset_audit_tables()（先 DROP TRIGGER 再清表再重建）。
_APPEND_ONLY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_audit_records_no_update
BEFORE UPDATE ON audit_records
BEGIN
    SELECT RAISE(ABORT, 'audit_records 是只追加表，禁止 UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_records_no_delete
BEFORE DELETE ON audit_records
BEGIN
    SELECT RAISE(ABORT, 'audit_records 是只追加表，禁止 DELETE');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_containers_no_update
BEFORE UPDATE ON audit_containers
BEGIN
    SELECT RAISE(ABORT, 'audit_containers 是只追加表，禁止 UPDATE');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_containers_no_delete
BEFORE DELETE ON audit_containers
BEGIN
    SELECT RAISE(ABORT, 'audit_containers 是只追加表，禁止 DELETE');
END;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对已有数据库做幂等升级（旧库没有 containers.version 列）。

    ``idempotency_keys`` 表由建表脚本中的 ``CREATE TABLE IF NOT EXISTS`` 负责补齐，
    旧库升级时自动创建，无需手工迁移。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(containers)")}
    if columns and "version" not in columns:
        # 旧库中的容器版本统一视为 0（DEFAULT 0 会回填到每一行）
        conn.execute(
            "ALTER TABLE containers ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
        )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.database_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # 显式事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """创建数据目录与全部表（幂等，容器启动时执行），并兼容升级旧库。"""
    directory = os.path.dirname(os.path.abspath(settings.database_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        # executescript 自带事务语义；各语句均为 IF NOT EXISTS，幂等
        conn.executescript(_SCHEMA)
        # 审计表的只追加触发器（CREATE TRIGGER IF NOT EXISTS，同样幂等）
        conn.executescript(_APPEND_ONLY_TRIGGERS)
        # 旧版本数据库缺少 version 列时补齐（ALTER TABLE 幂等由本函数判断）
        _migrate(conn)
    finally:
        conn.close()


def reset_audit_tables(conn: sqlite3.Connection) -> None:
    """清空审计哈希链（仅供测试隔离使用，生产路径不应调用）。

    只追加触发器会拒绝 DELETE，因此先摘除触发器、清空两表、重建触发器。
    其余业务表之间无外键引用审计表，删除顺序无约束。audit_records 使用普通
    INTEGER PRIMARY KEY（无 AUTOINCREMENT），清空后 MAX(seq) 归零即从 1 重新开始。
    """
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_audit_records_no_update;
        DROP TRIGGER IF EXISTS trg_audit_records_no_delete;
        DROP TRIGGER IF EXISTS trg_audit_containers_no_update;
        DROP TRIGGER IF EXISTS trg_audit_containers_no_delete;
        DELETE FROM audit_containers;
        DELETE FROM audit_records;
        """
    )
    conn.executescript(_APPEND_ONLY_TRIGGERS)


@contextmanager
def get_conn_context() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 请求依赖。"""
    with get_conn_context() as conn:
        yield conn
