"""批次审计哈希链端到端测试。

覆盖：
* 每个成功批次在同一事务追加一条只追加记录（批次摘要/受影响容器/结果版本）；
* seq 从 1 连续、prev_hash 串成 SHA-256 链，首条引用全 0 创世哈希；
* 失败批次与预演不落记录；幂等重放不追加第二条；
* 按容器与序号范围查询、单条查询、参数与 404 语义；
* GET /audit/verify 对未篡改链返回 valid，并检出内容篡改与删记录；
* 存储层只追加触发器拒绝 UPDATE/DELETE。
"""
import hashlib
import json
import sqlite3

from helpers import container, sample, volume_of

from app import audit
from app.db import _APPEND_ONLY_TRIGGERS, connect, reset_audit_tables


def _setup(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")


def _batch(client, transfers, key=None, expected_versions=None):
    body = {"transfers": transfers}
    if key is not None:
        body["idempotency_key"] = key
    if expected_versions is not None:
        body["expected_versions"] = expected_versions
    return client.post("/transfers/batch", json=body)


def _verify(client):
    return client.get("/audit/verify").json()


def _drop_audit_triggers(conn):
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_audit_records_no_update;
        DROP TRIGGER IF EXISTS trg_audit_records_no_delete;
        DROP TRIGGER IF EXISTS trg_audit_containers_no_update;
        DROP TRIGGER IF EXISTS trg_audit_containers_no_delete;
        """
    )


def _restore_audit_triggers(conn):
    conn.executescript(_APPEND_ONLY_TRIGGERS)


# --------------------------------------------------------------------------- #
# 成功批次 -> 追加记录；记录内容
# --------------------------------------------------------------------------- #
def test_successful_batches_append_sequential_records(client):
    _setup(client)

    assert _verify(client) == {
        "valid": True,
        "count": 0,
        "head_seq": None,
        "tail_seq": None,
        "tail_hash": None,
        "break_at": None,
        "reason": None,
    }

    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "40"},
            {"source": "S2", "destination": "V1", "volume": "60"},
        ],
    )
    assert r.status_code == 200, r.text

    r1 = client.get("/audit/records/1").json()
    assert r1["seq"] == 1
    assert r1["prev_hash"] == audit.GENESIS_PREV_HASH
    assert r1["transfer_count"] == 2
    assert r1["affected_containers"] == ["S1", "S2", "V1"]
    assert r1["versions"] == {"S1": 1, "S2": 1, "V1": 1}
    assert r1["summary"] == {
        "transfer_count": 2,
        "steps": [
            {
                "index": 1,
                "source": "S1",
                "destination": "V1",
                "volume": "40.000000000",
                "taken": [{"source_id": "S1", "amount": "40.000000000"}],
            },
            {
                "index": 2,
                "source": "S2",
                "destination": "V1",
                "volume": "60.000000000",
                "taken": [{"source_id": "S2", "amount": "60.000000000"}],
            },
        ],
    }
    assert r1["created"] is not None

    # 第二批：混合来源按 40/60 分摊，摘要中记录实际扣取的祖先份额
    r = _batch(client, [{"source": "V1", "destination": "V2", "volume": "25"}])
    assert r.status_code == 200, r.text
    r2 = client.get("/audit/records/2").json()
    assert r2["seq"] == 2
    assert r2["prev_hash"] == r1["record_hash"]
    assert r2["affected_containers"] == ["V1", "V2"]
    assert r2["versions"] == {"V1": 2, "V2": 1}
    taken = {(t["source_id"], t["amount"]) for t in r2["summary"]["steps"][0]["taken"]}
    assert taken == {("S1", "10.000000000"), ("S2", "15.000000000")}

    v = _verify(client)
    assert v["valid"] is True
    assert v["count"] == 2
    assert v["head_seq"] == 1
    assert v["tail_seq"] == 2
    assert v["tail_hash"] == r2["record_hash"]


def test_record_hash_is_sha256_of_canonical_payload(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])

    conn = connect()
    try:
        row = conn.execute(
            "SELECT record_hash, payload FROM audit_records WHERE seq = 1"
        ).fetchone()
    finally:
        conn.close()

    expected = hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
    assert row["record_hash"] == expected
    assert len(expected) == 64
    # payload 是紧凑、键排序的规范化 JSON
    payload = json.loads(row["payload"])
    assert set(payload) == {
        "seq",
        "prev_hash",
        "summary",
        "affected_containers",
        "versions",
    }
    assert payload["seq"] == 1
    assert payload["prev_hash"] == audit.GENESIS_PREV_HASH


# --------------------------------------------------------------------------- #
# 失败批次与预演不产生记录；审计与批次同事务
# --------------------------------------------------------------------------- #
def test_failed_batch_writes_no_audit_and_leaves_state_untouched(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])

    # 第二条源液不足 -> 整批失败
    r = _batch(
        client,
        [
            {"source": "S2", "destination": "V1", "volume": "5"},
            {"source": "S1", "destination": "V2", "volume": "9999"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "insufficient_volume"

    # 审计链仍只有第一条记录；状态也完全回滚
    assert _verify(client)["count"] == 1
    assert volume_of(client, "S2") == "100.000000000"
    assert volume_of(client, "V1") == "40.000000000"
    assert volume_of(client, "V2") == "0"


def test_unknown_container_and_stale_version_write_no_audit(client):
    _setup(client)
    _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "40"}],
        expected_versions={"S1": 0, "V1": 0},
    )

    r = _batch(
        client,
        [{"source": "S1", "destination": "GHOST", "volume": "1"}],
    )
    assert r.status_code == 404

    # 版本过期：S1 已为 1，按 0 提交 -> 409 stale_container
    r = _batch(
        client,
        [{"source": "S1", "destination": "V1", "volume": "1"}],
        expected_versions={"S1": 0, "V1": 1},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"

    assert _verify(client)["count"] == 1


def test_preview_never_writes_audit(client):
    _setup(client)
    # 成功预演
    r = client.post(
        "/transfers/preview",
        json={
            "transfers": [
                {"source": "S1", "destination": "V1", "volume": "40"},
            ],
            "idempotency_key": "ignored-anyway",
        },
    )
    assert r.status_code == 200
    # 失败预演（源液不足）
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "9999"}]},
    )
    assert r.status_code == 422

    assert _verify(client)["count"] == 0
    assert volume_of(client, "S1") == "100.000000000"


def test_idempotent_replay_does_not_append_second_record(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}], key="k1")
    assert _verify(client)["count"] == 1

    # 同键同请求体重放：不重复转移，也不追加审计
    for _ in range(2):
        r = _batch(
            client, [{"source": "S1", "destination": "V1", "volume": "40"}], key="k1"
        )
        assert r.status_code == 200
        assert r.headers.get("Idempotency-Replayed") == "true"

    v = _verify(client)
    assert v["valid"] is True
    assert v["count"] == 1
    assert volume_of(client, "S1") == "60.000000000"
    assert client.get("/containers/S1").json()["version"] == 1

    # 同键异请求体 -> 409，不追加
    r = _batch(
        client, [{"source": "S1", "destination": "V1", "volume": "41"}], key="k1"
    )
    assert r.status_code == 409
    assert _verify(client)["count"] == 1


# --------------------------------------------------------------------------- #
# 查询：按容器、按序号范围
# --------------------------------------------------------------------------- #
def test_list_all_records_ordered_by_seq(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "2"}])
    _batch(client, [{"source": "V1", "destination": "V2", "volume": "1"}])

    r = client.get("/audit/records")
    assert r.status_code == 200
    body = r.json()
    assert body["container_id"] is None
    assert body["seq_from"] is None
    assert body["seq_to"] is None
    assert body["count"] == 3
    assert [rec["seq"] for rec in body["records"]] == [1, 2, 3]


def test_list_records_filtered_by_container(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])      # 1
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "60"}])      # 2
    _batch(client, [{"source": "V1", "destination": "V2", "volume": "25"}])      # 3

    r = client.get("/audit/records", params={"container_id": "V1"})
    seqs = [rec["seq"] for rec in r.json()["records"]]
    assert seqs == [1, 2, 3]  # V1 三批都受影响

    r = client.get("/audit/records", params={"container_id": "S1"})
    seqs = [rec["seq"] for rec in r.json()["records"]]
    assert seqs == [1]

    r = client.get("/audit/records", params={"container_id": "V2"})
    seqs = [rec["seq"] for rec in r.json()["records"]]
    assert seqs == [3]

    r = client.get("/audit/records", params={"container_id": "S2"})
    body = r.json()
    assert body["container_id"] == "S2"
    assert body["count"] == 1
    assert body["records"][0]["seq"] == 2

    # 未知容器 -> 404
    r = client.get("/audit/records", params={"container_id": "GHOST"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


def test_list_records_filtered_by_seq_range(client):
    _setup(client)
    for i in range(1, 5):
        _batch(client, [{"source": "S1", "destination": "V1", "volume": str(i)}])

    r = client.get("/audit/records", params={"seq_from": 2, "seq_to": 4})
    body = r.json()
    assert body["seq_from"] == 2
    assert body["seq_to"] == 4
    assert [rec["seq"] for rec in body["records"]] == [2, 3, 4]

    # 单侧边界
    r = client.get("/audit/records", params={"seq_from": 4})
    assert [rec["seq"] for rec in r.json()["records"]] == [4]
    r = client.get("/audit/records", params={"seq_to": 2})
    assert [rec["seq"] for rec in r.json()["records"]] == [1, 2]

    # 容器 + 范围组合
    r = client.get(
        "/audit/records", params={"container_id": "V1", "seq_from": 2, "seq_to": 3}
    )
    assert [rec["seq"] for rec in r.json()["records"]] == [2, 3]

    # 范围无命中 -> 空列表（不是错误）
    r = client.get("/audit/records", params={"seq_from": 99, "seq_to": 120})
    body = r.json()
    assert body["count"] == 0
    assert body["records"] == []

    # from > to -> 422
    r = client.get("/audit/records", params={"seq_from": 5, "seq_to": 2})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_seq_range"

    # 小于 1 的查询参数被 FastAPI 拒绝
    r = client.get("/audit/records", params={"seq_from": 0})
    assert r.status_code == 422


def test_get_single_record_404(client):
    _setup(client)
    r = client.get("/audit/records/1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "audit_record_not_found"

    _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    assert client.get("/audit/records/2").status_code == 404


# --------------------------------------------------------------------------- #
# 链校验：检出篡改 / 删除
# --------------------------------------------------------------------------- #
def test_verify_detects_payload_tampering(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "60"}])
    assert _verify(client)["valid"] is True

    conn = connect()
    try:
        _drop_audit_triggers(conn)
        row = conn.execute(
            "SELECT payload FROM audit_records WHERE seq = 1"
        ).fetchone()
        body = json.loads(row["payload"])
        body["versions"]["S1"] = 42  # 篡改结果版本
        conn.execute(
            "UPDATE audit_records SET payload = ? WHERE seq = 1",
            (audit.canonical_json(body),),
        )
        _restore_audit_triggers(conn)
    finally:
        conn.close()

    v = _verify(client)
    assert v["valid"] is False
    assert v["break_at"] == 1
    assert v["count"] == 2
    assert "record_hash" in v["reason"]


def test_verify_detects_deleted_middle_record(client):
    _setup(client)
    for i in range(3):
        _batch(
            client,
            [{"source": "S1", "destination": "V1", "volume": str(i + 1)}],
        )
    assert _verify(client)["valid"] is True

    conn = connect()
    try:
        _drop_audit_triggers(conn)
        conn.execute("DELETE FROM audit_containers WHERE seq = 2")
        conn.execute("DELETE FROM audit_records WHERE seq = 2")
        _restore_audit_triggers(conn)
    finally:
        conn.close()

    v = _verify(client)
    assert v["valid"] is False
    assert v["break_at"] == 3
    assert "序号不连续" in v["reason"]


def test_verify_detects_broken_prev_hash_link(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    _batch(client, [{"source": "S2", "destination": "V1", "volume": "2"}])

    conn = connect()
    try:
        _drop_audit_triggers(conn)
        # 把第 2 条的 prev_hash 改成别的值（重排/断裂）
        conn.execute(
            "UPDATE audit_records SET prev_hash = ? WHERE seq = 2",
            ("f" * 64,),
        )
        _restore_audit_triggers(conn)
    finally:
        conn.close()

    v = _verify(client)
    assert v["valid"] is False
    assert v["break_at"] == 2
    assert "prev_hash" in v["reason"]


def test_verify_detects_container_index_tampering(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])

    conn = connect()
    try:
        _drop_audit_triggers(conn)
        conn.execute("DELETE FROM audit_containers WHERE seq = 1 AND container_id = 'S1'")
        _restore_audit_triggers(conn)
    finally:
        conn.close()

    v = _verify(client)
    assert v["valid"] is False
    assert v["break_at"] == 1
    assert "audit_containers" in v["reason"]

    # 按容器查询也会反映篡改：S1 不再命中
    r = client.get("/audit/records", params={"container_id": "S1"})
    assert r.json()["records"] == []


# --------------------------------------------------------------------------- #
# 只追加存储保护
# --------------------------------------------------------------------------- #
def test_append_only_triggers_reject_update_and_delete(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])

    conn = connect()
    try:
        for stmt in (
            "UPDATE audit_records SET transfer_count = 9 WHERE seq = 1",
            "DELETE FROM audit_records WHERE seq = 1",
            "UPDATE audit_containers SET container_id = 'X' WHERE seq = 1",
            "DELETE FROM audit_containers WHERE seq = 1",
        ):
            try:
                conn.execute(stmt)
                raise AssertionError(f"应当被触发器拒绝：{stmt}")
            except sqlite3.DatabaseError as exc:
                assert "只追加" in str(exc)
    finally:
        conn.close()

    # 记录原封不动，链仍然有效
    v = _verify(client)
    assert v["valid"] is True
    assert v["count"] == 1


def test_reset_audit_tables_helper_clears_chain(client):
    _setup(client)
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "40"}])
    assert _verify(client)["count"] == 1

    conn = connect()
    try:
        reset_audit_tables(conn)
        max_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM audit_records").fetchone()["m"]
        assert max_seq == 0
    finally:
        conn.close()

    # 清空后重新写入仍从 1 开始，且触发器仍然生效
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "3"}])
    rec = client.get("/audit/records/1").json()
    assert rec["prev_hash"] == audit.GENESIS_PREV_HASH
    assert _verify(client)["valid"] is True
