"""只追加（append-only）的批次审计哈希链。

每个**成功提交**的批量转移（``POST /transfers/batch``）在其写事务内追加一条
审计记录，内容包含：

* **批次摘要** ``summary``：转移条数与逐步明细（源/目标/规范化体积、按混合
  比例实际扣取的各祖先来源精确份额）；
* **受影响容器** ``affected_containers``：本批触碰（源∪目标去重）的全部容器；
* **结果版本** ``versions``：提交后每个受影响容器的乐观版本（与批量响应一致）。

每条记录持有链上位置 ``seq``（从 1 开始连续递增）、上一条记录哈希
``prev_hash``（创世记录为全 0 的 64 位十六串 ``GENESIS_PREV_HASH``）以及本条
记录哈希 ``record_hash``。哈希输入为记录内容（含 ``seq``、``prev_hash``、
``summary``、``affected_containers``、``versions``）的规范化 JSON
（``json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)``），
算法 SHA-256，输出小写 hex。因此任何一条记录被删改，都会让它自身的重算哈希与
存储哈希不符，并破坏其后所有记录对前一条哈希的引用——链可整体校验。

存储层用 SQLite 触发器（见 :mod:`app.db` 中 ``_APPEND_ONLY_TRIGGERS``）拒绝
对 ``audit_records`` / ``audit_containers`` 的任何 UPDATE 与 DELETE；记录只在
批次事务中 INSERT，且与容器扣减、版本递增在**同一事务**，失败批次与幂等重放
（不重复执行转移）以及预演（只读）都不会产生记录。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any

# 创世记录（seq=1）引用的“前一条哈希”：全 0 的 SHA-256 长度十六串
GENESIS_PREV_HASH = "0" * 64


class AuditError(Exception):
    """审计查询 / 校验错误，映射为 HTTP 4xx。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 规范化与哈希
# --------------------------------------------------------------------------- #
def canonical_json(body: Any) -> str:
    """把记录内容序列化为确定性 JSON：键排序、紧凑分隔、不转义非 ASCII。

    哈希输入与 ``payload`` 列落库均使用本函数，保证“存什么就验什么”。
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_body(body: Any) -> str:
    """计算规范化记录内容的 SHA-256（小写 hex）。"""
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _step_summary(
    index: int,
    src: str,
    dst: str,
    volume: Decimal,
    taken: dict[str, Decimal],
) -> dict:
    """构造批次摘要中的一步（taken 按来源 ID 排序，体积全部字符串化）。"""
    return {
        "index": index,
        "source": src,
        "destination": dst,
        "volume": str(volume),
        "taken": [
            {"source_id": sid, "amount": str(amount)}
            for sid, amount in sorted(taken.items())
        ],
    }


def build_record_body(
    seq: int,
    prev_hash: str,
    prepared: list[tuple[str, str, Decimal]],
    taken_steps: list[dict[str, Decimal]],
    next_versions: dict[str, int],
) -> dict:
    """组装待哈希 / 落库的审计记录内容（不含 created，时间戳不参与哈希）。

    ``affected_containers`` 取本批全部源/目标去重并排序；``versions`` 保留
    提交后的容器版本（规范化 JSON 会对其键排序，使哈希与字典插入顺序无关）。
    """
    affected = sorted({cid for src, dst, _v in prepared for cid in (src, dst)})
    return {
        "seq": seq,
        "prev_hash": prev_hash,
        "summary": {
            "transfer_count": len(prepared),
            "steps": [
                _step_summary(i, src, dst, volume, taken)
                for i, ((src, dst, volume), taken) in enumerate(
                    zip(prepared, taken_steps), start=1
                )
            ],
        },
        "affected_containers": affected,
        "versions": dict(next_versions),
    }


# --------------------------------------------------------------------------- #
# 写入（在批次写事务内调用）
# --------------------------------------------------------------------------- #
def append_batch_audit(
    conn: sqlite3.Connection,
    prepared: list[tuple[str, str, Decimal]],
    taken_steps: list[dict[str, Decimal]],
    next_versions: dict[str, int],
) -> dict:
    """在当前批次写事务内追加一条审计记录及其受影响容器索引。

    必须在已取得写锁的事务中（调用方为 ``BEGIN IMMEDIATE`` 之后）调用，
    与容器扣减/版本递增同事务：任一后续写入失败则整体回滚，审计记录也不留痕。
    新 ``seq`` 通过 ``max(seq)+1`` 在同一事务内取得（``BEGIN IMMEDIATE`` 的
    写锁保证并发批次串行化，不会重号）。

    返回本条记录的对外表示（含 ``seq`` 与两个哈希）。
    """
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM audit_records").fetchone()
    seq = int(row["max_seq"]) + 1
    prev_row = conn.execute(
        "SELECT record_hash FROM audit_records WHERE seq = ?", (seq - 1,)
    ).fetchone()
    # 正常链上 prev_row 必然存在（seq>1 时）；GENESIS 常量仅用于 seq==1
    prev_hash = prev_row["record_hash"] if prev_row is not None else GENESIS_PREV_HASH

    body = build_record_body(seq, prev_hash, prepared, taken_steps, next_versions)
    record_hash = hash_body(body)
    payload = canonical_json(body)

    conn.execute(
        "INSERT INTO audit_records"
        " (seq, prev_hash, record_hash, transfer_count, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        (seq, prev_hash, record_hash, len(prepared), payload),
    )
    conn.executemany(
        "INSERT INTO audit_containers (seq, container_id) VALUES (?, ?)",
        [(seq, cid) for cid in body["affected_containers"]],
    )
    return _represent(body, record_hash, created=None)


# --------------------------------------------------------------------------- #
# 读取 / 查询
# --------------------------------------------------------------------------- #
def _body_from_row(row: sqlite3.Row) -> dict:
    """从存储行解析规范化 JSON 负载（即当初哈希的输入）。"""
    return json.loads(row["payload"])


def _represent(body: dict, record_hash: str, created: str | None) -> dict:
    """构造审计记录的对外 JSON 表示。"""
    return {
        "seq": body["seq"],
        "prev_hash": body["prev_hash"],
        "record_hash": record_hash,
        "transfer_count": body["summary"]["transfer_count"],
        "summary": body["summary"],
        "affected_containers": body["affected_containers"],
        "versions": body["versions"],
        "created": created,
    }


def get_audit_record(conn: sqlite3.Connection, seq: int) -> dict:
    """按序号取单条审计记录；不存在抛 404 audit_record_not_found。"""
    row = conn.execute(
        "SELECT seq, record_hash, payload, created FROM audit_records WHERE seq = ?",
        (seq,),
    ).fetchone()
    if row is None:
        raise AuditError(404, "audit_record_not_found", f"审计记录 #{seq} 不存在")
    return _represent(_body_from_row(row), row["record_hash"], row["created"])


def list_audit_records(
    conn: sqlite3.Connection,
    container_id: str | None = None,
    seq_from: int | None = None,
    seq_to: int | None = None,
) -> dict:
    """按容器与序号范围查询审计记录（只追加，按 seq 升序返回）。

    * ``container_id`` 给定时，仅返回该容器出现在受影响容器集合中的批次；
      容器本身不存在返回 404 unknown_container；
    * ``seq_from`` / ``seq_to`` 为闭区间，任一越界非法（from>to）返回
      422 invalid_seq_range；不给定时分别取链首/链尾；
    * 空结果返回 ``records: []``（不视为错误）。
    """
    if seq_from is not None and seq_to is not None and seq_from > seq_to:
        raise AuditError(
            422,
            "invalid_seq_range",
            f"序号范围非法：seq_from={seq_from} 大于 seq_to={seq_to}",
        )
    if container_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM containers WHERE id = ?", (container_id,)
        ).fetchone()
        if exists is None:
            raise AuditError(
                404, "unknown_container", f"容器 '{container_id}' 不存在"
            )

    sql = (
        "SELECT r.seq AS seq, r.record_hash AS record_hash, r.payload AS payload,"
        " r.created AS created FROM audit_records r"
    )
    params: list[Any] = []
    if container_id is not None:
        # audit_containers 每批次对每个受影响容器恰一行，JOIN 不会产生重复
        sql += " JOIN audit_containers ac ON ac.seq = r.seq AND ac.container_id = ?"
        params.append(container_id)
    where: list[str] = []
    if seq_from is not None:
        where.append("r.seq >= ?")
        params.append(seq_from)
    if seq_to is not None:
        where.append("r.seq <= ?")
        params.append(seq_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.seq ASC"

    records = [
        _represent(_body_from_row(r), r["record_hash"], r["created"])
        for r in conn.execute(sql, params)
    ]
    return {
        "container_id": container_id,
        "seq_from": seq_from,
        "seq_to": seq_to,
        "count": len(records),
        "records": records,
    }


# --------------------------------------------------------------------------- #
# 整链校验
# --------------------------------------------------------------------------- #
def _verification_ok(
    count: int, head_seq: int | None, tail_seq: int | None, tail_hash: str | None
) -> dict:
    return {
        "valid": True,
        "count": count,
        "head_seq": head_seq,
        "tail_seq": tail_seq,
        "tail_hash": tail_hash,
        "break_at": None,
        "reason": None,
    }


def _verification_bad(
    count: int, head_seq: int | None, tail_seq: int | None, break_seq: int, reason: str
) -> dict:
    return {
        "valid": False,
        "count": count,
        "head_seq": head_seq,
        "tail_seq": tail_seq,
        "tail_hash": None,
        "break_at": break_seq,
        "reason": reason,
    }


def verify_chain(conn: sqlite3.Connection) -> dict:
    """在一致性只读快照内顺序重放整条审计哈希链。

    对每条记录依次检查：

    1. ``seq`` 从 1 开始连续无缺号（可检出删除中间记录——触发器防正常连接的
       DELETE，此检查兜底绕过触发器或底层文件损坏的情况）；
    2. 存储的 ``record_hash`` 等于按 ``payload`` 重算的 SHA-256
       （可检出内容篡改）；
    3. ``prev_hash`` 等于上一条记录的 ``record_hash``，首条必须等于
       :data:`GENESIS_PREV_HASH`（可检出链断裂 / 记录重排）；
    4. ``payload`` 中声明的 ``affected_containers`` 与 ``versions`` 键集合
       一致（摘要自洽）；
    5. ``audit_containers`` 索引行与 ``affected_containers`` 完全一致
       （按容器查询所用索引未被篡改——该表同样受只追加触发器保护）。

    任一不符：``valid=false``，并给出首个断裂位置 ``break_at`` 与原因。
    """
    conn.execute("BEGIN")
    try:
        rows = conn.execute(
            "SELECT seq, prev_hash, record_hash, payload, transfer_count FROM audit_records"
            " ORDER BY seq ASC"
        ).fetchall()
        if not rows:
            conn.execute("ROLLBACK")
            return _verification_ok(0, None, None, None)

        head_seq = int(rows[0]["seq"])
        tail_seq = int(rows[-1]["seq"])
        expected_seq = 1
        prev_hash = GENESIS_PREV_HASH

        for row in rows:
            seq = int(row["seq"])
            stored_hash = row["record_hash"]
            stored_prev = row["prev_hash"]
            body = json.loads(row["payload"])

            def fail(reason: str) -> dict:
                conn.execute("ROLLBACK")
                return _verification_bad(len(rows), head_seq, tail_seq, seq, reason)

            if seq != expected_seq:
                return fail(
                    f"序号不连续：期望 #{expected_seq}，实际 #{seq}（疑似记录被删除）"
                )
            recomputed = hash_body(body)
            if stored_hash != recomputed:
                return fail("记录内容与 record_hash 不符（记录被篡改或损坏）")
            if body.get("seq") != seq:
                return fail("payload 中的 seq 与行序号不一致")
            if stored_prev != prev_hash:
                return fail(
                    f"prev_hash 与上一条记录哈希不符（链断裂；期望 {prev_hash}，"
                    f"实际 {stored_prev}）"
                )
            affected = body.get("affected_containers")
            versions = body.get("versions")
            if not isinstance(affected, list) or not isinstance(versions, dict):
                return fail("payload 缺少 affected_containers / versions 字段")
            if sorted(affected) != sorted(versions):
                return fail("affected_containers 与 versions 的容器集合不一致")
            if body.get("summary", {}).get("transfer_count") != int(
                row["transfer_count"]
            ):
                return fail("摘要 transfer_count 与冗余列不符")

            index_rows = conn.execute(
                "SELECT container_id FROM audit_containers WHERE seq = ? ORDER BY container_id ASC",
                (seq,),
            ).fetchall()
            indexed = [r["container_id"] for r in index_rows]
            if indexed != sorted(affected):
                return fail("受影响容器索引（audit_containers）与记录内容不一致")

            prev_hash = stored_hash
            expected_seq += 1

        conn.execute("ROLLBACK")
        return _verification_ok(len(rows), head_seq, tail_seq, prev_hash)
    except Exception:
        conn.execute("ROLLBACK")
        raise
