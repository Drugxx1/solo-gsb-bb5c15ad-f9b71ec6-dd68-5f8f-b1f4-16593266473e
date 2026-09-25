"""已确认校准预约有效性巡检：UTC 半开窗筛选、开始时刻重建唯一有效链、
与确认时证书快照比较（valid / changed / invalid）、稳定排序与状态汇总。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRACE_DB_PATH"] = _tmp.name
os.environ["SEED_DEMO"] = "0"

from app import booking as booking_svc  # noqa: E402
from app.database import get_conn_ctx, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

START = "2026-06-20T09:00:00Z"
END = "2026-06-20T10:00:00Z"
WIN_LO = "2026-06-20T00:00:00Z"
WIN_HI = "2026-06-21T00:00:00Z"


def _chain(client, *, code="W", leaf_until="2027-01-01T00:00:00"):
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链。"""
    cg, cr, cw = f"CG-{code}", f"CR-{code}", f"CW-{code}"
    instruments = [
        {"code": f"G-{code}", "name": f"国家基准-{code}",
         "is_national_standard": True},
        {"code": f"R-{code}", "name": f"参考标准器-{code}"},
        {"code": code, "name": "工作仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": f"G-{code}", "cert_number": f"{code}-G1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": cr, "instrument_code": f"R-{code}", "cert_number": f"{code}-R1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cw, "instrument_code": code, "cert_number": f"{code}-W1",
         "issued_at": "2026-01-02T00:00:00",
         "valid_from": "2026-01-01T00:00:00", "valid_until": leaf_until,
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": cw, "parent_cert_id": cr},
        {"child_cert_id": cr, "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions",
                    json={"instruments": instruments, "certificates": certs,
                          "edges": edges})
    assert r.status_code == 201, r.text


def _book(client, code="W", start=START, duration=3600):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": code, "start": start, "duration_seconds": duration,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _update_cert(client, *, cert_id, code, valid_from, valid_until,
                 issued_at="2026-01-02T00:00:00"):
    """按 ID upsert 证书（未给摘要时服务端重算，链校验仍可通过）。"""
    r = client.post("/api/certificates", json={
        "id": cert_id, "instrument_code": code, "cert_number": f"{code}-W1",
        "issued_at": issued_at, "valid_from": valid_from,
        "valid_until": valid_until, "uncertainty": 0.01, "unit": "mm",
    })
    assert r.status_code in (200, 201), r.text


def _inspect(client, start=WIN_LO, end=WIN_HI):
    return client.get("/api/calibration-bookings/inspection",
                      params={"start": start, "end": end})


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- 入参校验

def test_window_start_equal_end_422(client):
    r = _inspect(client, start=WIN_LO, end=WIN_LO)
    assert r.status_code == 422
    assert "严格早于" in r.text


def test_window_start_after_end_422(client):
    r = _inspect(client, start=WIN_HI, end=WIN_LO)
    assert r.status_code == 422
    assert "严格早于" in r.text


def test_missing_window_params_422(client):
    assert client.get("/api/calibration-bookings/inspection").status_code == 422
    r = client.get("/api/calibration-bookings/inspection",
                   params={"start": WIN_LO})
    assert r.status_code == 422


def test_empty_window(client):
    r = _inspect(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["window_start"] == WIN_LO
    assert d["window_end"] == WIN_HI
    assert d["booking_count"] == 0
    assert d["status_counts"] == {"valid": 0, "changed": 0, "invalid": 0}
    assert d["results"] == []


# ---------------------------------------------------------------- 半开窗筛选

def test_half_open_window_filter(client):
    _chain(client)
    bk = _book(client)  # 开始时刻 2026-06-20T09:00:00Z
    # 窗起点恰等于预约开始时刻：包含（半开左闭）
    d = _inspect(client, start=START, end=WIN_HI).json()
    assert [x["booking_id"] for x in d["results"]] == [bk["id"]]
    # 窗终点恰等于预约开始时刻：排除（半开右开）
    d = _inspect(client, start=WIN_LO, end=START).json()
    assert d["results"] == []
    assert d["booking_count"] == 0
    # 窗完全在预约开始之前：排除
    assert _inspect(client, start="2026-06-19T00:00:00Z",
                    end="2026-06-20T00:00:00Z").json()["results"] == []


# ---------------------------------------------------------------- valid

def test_valid_booking(client):
    _chain(client)
    bk = _book(client)
    r = _inspect(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["booking_count"] == 1
    assert d["status_counts"] == {"valid": 1, "changed": 0, "invalid": 0}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["instrument_code"] == "W"
    assert item["start_at"] == START
    assert item["end_at"] == END
    assert item["status"] == "valid"
    assert item["reason_code"] is None
    assert item["reason"] is None
    # 当前证书与确认时快照一致
    assert item["current_certificate_id"] == "CW-W"
    assert item["current_certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert item["snapshot_certificate_id"] == "CW-W"
    assert item["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"


# ---------------------------------------------------------------- changed

def test_changed_certificate_valid_until(client):
    """链仍有效，但起点证书有效期截止被更新（仍覆盖预约结束）-> changed。"""
    _chain(client)
    bk = _book(client)
    # 同一张证书，截止由 2027-01-01 缩短为 2026-12-01（仍晚于预约结束）
    _update_cert(client, cert_id="CW-W", code="W",
                 valid_from="2026-01-01T00:00:00",
                 valid_until="2026-12-01T00:00:00")
    d = _inspect(client).json()
    assert d["status_counts"] == {"valid": 0, "changed": 1, "invalid": 0}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["status"] == "changed"
    assert item["reason_code"] == "CERTIFICATE_SNAPSHOT_CHANGED"
    assert "快照" in item["reason"]
    assert item["current_certificate_id"] == "CW-W"
    assert item["current_certificate_valid_until"] == "2026-12-01T00:00:00Z"
    assert item["snapshot_certificate_id"] == "CW-W"
    assert item["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"


def test_changed_certificate_id(client):
    """旧证提前到期、新证接续：开始时刻起点证书换人 -> changed。"""
    _chain(client)
    bk = _book(client)
    # 旧证 2026-06-01 到期（预约开始 06-20 已不在有效期），新证接续
    _update_cert(client, cert_id="CW-W", code="W",
                 valid_from="2026-01-01T00:00:00",
                 valid_until="2026-06-01T00:00:00")
    r = client.post("/api/submissions", json={
        "instruments": [],
        "certificates": [
            {"id": "CW-W2", "instrument_code": "W", "cert_number": "W-W2",
             "issued_at": "2026-06-02T00:00:00",
             "valid_from": "2026-06-01T00:00:00",
             "valid_until": "2027-06-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [{"child_cert_id": "CW-W2", "parent_cert_id": "CR-W"}],
    })
    assert r.status_code == 201, r.text
    d = _inspect(client).json()
    assert d["status_counts"] == {"valid": 0, "changed": 1, "invalid": 0}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["status"] == "changed"
    assert item["reason_code"] == "CERTIFICATE_SNAPSHOT_CHANGED"
    assert item["current_certificate_id"] == "CW-W2"
    assert item["current_certificate_valid_until"] == "2027-06-01T00:00:00Z"
    assert item["snapshot_certificate_id"] == "CW-W"
    assert item["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"


# ---------------------------------------------------------------- invalid

def test_invalid_chain_broken_multiple_active(client):
    """开始时刻同时两张证书有效，起点不唯一 -> invalid。"""
    _chain(client)
    bk = _book(client)
    # 再登记一张与 CW-W 有效期重叠的证书（无向上边，仅制造多起点）
    r = client.post("/api/submissions", json={
        "instruments": [],
        "certificates": [
            {"id": "CW-WX", "instrument_code": "W", "cert_number": "W-WX",
             "issued_at": "2026-02-02T00:00:00",
             "valid_from": "2026-02-01T00:00:00",
             "valid_until": "2026-12-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [],
    })
    assert r.status_code == 201, r.text
    d = _inspect(client).json()
    assert d["status_counts"] == {"valid": 0, "changed": 0, "invalid": 1}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["status"] == "invalid"
    assert item["reason_code"] == "MULTIPLE_ACTIVE_CERTIFICATES"
    assert item["current_certificate_id"] is None
    assert item["current_certificate_valid_until"] is None
    # 原快照仍随结果返回
    assert item["snapshot_certificate_id"] == "CW-W"
    assert item["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"


def test_invalid_chain_broken_parent_expired(client):
    """父证书在开始时刻前失效，链断裂 -> invalid（CHAIN_BROKEN）。"""
    _chain(client)
    bk = _book(client)
    # 参考标准证书 2026-03-01 到期，预约开始 06-20 时父证书已失效
    _update_cert(client, cert_id="CR-W", code="R-W",
                 valid_from="2025-06-01T00:00:00",
                 valid_until="2026-03-01T00:00:00",
                 issued_at="2025-06-01T00:00:00")
    d = _inspect(client).json()
    assert d["status_counts"] == {"valid": 0, "changed": 0, "invalid": 1}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["status"] == "invalid"
    assert item["reason_code"] == "CHAIN_BROKEN"
    assert item["current_certificate_id"] is None


def test_invalid_deadline_insufficient(client):
    """链仍有效，但当前起点证书到期早于预约结束 -> invalid。"""
    _chain(client)
    bk = _book(client)  # 占用 [09:00, 10:00)
    # 起点证书截止缩短到 09:30：开始时刻链仍有效，但预约结束 10:00 超过截止
    _update_cert(client, cert_id="CW-W", code="W",
                 valid_from="2026-01-01T00:00:00",
                 valid_until="2026-06-20T09:30:00Z")
    d = _inspect(client).json()
    assert d["status_counts"] == {"valid": 0, "changed": 0, "invalid": 1}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["status"] == "invalid"
    assert item["reason_code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    # 链仍有效：当前证书信息照常返回，便于与快照比较
    assert item["current_certificate_id"] == "CW-W"
    assert item["current_certificate_valid_until"] == "2026-06-20T09:30:00Z"
    assert item["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"


def test_invalid_instrument_not_found(client):
    """预约所属仪器被删除 -> invalid（INSTRUMENT_NOT_FOUND）。"""
    _chain(client)
    bk = _book(client)
    with get_conn_ctx() as conn:
        # 绕过外键直接删除仪器行，模拟“仪器档案缺失但预约仍在”的脏数据
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM instruments WHERE code=?", ("W",))
    d = _inspect(client).json()
    assert d["status_counts"] == {"valid": 0, "changed": 0, "invalid": 1}
    (item,) = d["results"]
    assert item["booking_id"] == bk["id"]
    assert item["status"] == "invalid"
    assert item["reason_code"] == "INSTRUMENT_NOT_FOUND"
    assert item["current_certificate_id"] is None


# ---------------------------------------------------------------- 排序与汇总

def test_stable_order_and_status_counts(client):
    """多条预约按（开始时刻, 预约 ID）稳定排序，各状态数量正确汇总。"""
    _chain(client, code="W1")
    _chain(client, code="W2")
    _chain(client, code="W3")
    # 直接播种以控制预约 ID（同一起点开始，按 ID 字典序排列）
    with get_conn_ctx() as conn:
        for bid, code, start in (
            ("BK-T-003", "W1", "2026-06-20T09:00:00Z"),
            ("BK-T-001", "W2", "2026-06-20T09:00:00Z"),
            ("BK-T-002", "W3", "2026-06-20T11:00:00Z"),
        ):
            booking_svc.insert_booking(
                conn, booking_id=bid, instrument_code=code,
                start_at=start, end_at="2026-06-20T12:00:00Z",
                duration_seconds=3600, certificate_id=f"CW-{code}",
                certificate_valid_until="2027-01-01T00:00:00Z",
            )
    # W2 证书截止变化（仍覆盖结束）-> changed；W3 起点证书提前到期 -> invalid
    _update_cert(client, cert_id="CW-W2", code="W2",
                 valid_from="2026-01-01T00:00:00",
                 valid_until="2026-12-01T00:00:00")
    _update_cert(client, cert_id="CW-W3", code="W3",
                 valid_from="2026-01-01T00:00:00",
                 valid_until="2026-06-20T11:30:00Z")
    d = _inspect(client).json()
    assert d["booking_count"] == 3
    assert d["status_counts"] == {"valid": 1, "changed": 1, "invalid": 1}
    # 排序：先按开始时刻，同刻按预约 ID
    assert [x["booking_id"] for x in d["results"]] == [
        "BK-T-001", "BK-T-003", "BK-T-002",
    ]
    statuses = {x["booking_id"]: x["status"] for x in d["results"]}
    assert statuses == {
        "BK-T-001": "changed",   # W2 截止变化
        "BK-T-003": "valid",     # W1 未动
        "BK-T-002": "invalid",   # W3 结束 12:00 晚于新截止 11:30
    }
    reasons = {x["booking_id"]: x["reason_code"] for x in d["results"]}
    assert reasons["BK-T-001"] == "CERTIFICATE_SNAPSHOT_CHANGED"
    assert reasons["BK-T-002"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    assert reasons["BK-T-003"] is None


# ---------------------------------------------------------------- 只读语义

def test_inspection_is_read_only(client):
    _chain(client)
    bk = _book(client)
    before = client.get("/api/calibration-bookings").json()
    first = _inspect(client).json()
    second = _inspect(client).json()
    after = client.get("/api/calibration-bookings").json()
    assert first == second                      # 可安全重复调用
    assert before == after                      # 预约数据未被修改
    assert after[0]["id"] == bk["id"]
    assert after[0]["certificate_id"] == "CW-W"
