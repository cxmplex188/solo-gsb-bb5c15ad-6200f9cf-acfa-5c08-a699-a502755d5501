"""API 端到端测试（FastAPI TestClient + 临时 SQLite）。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.seed import SEED_REFERENCES


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo_data=True)
    with TestClient(app) as c:  # 触发 lifespan：建表 + 种子数据
        yield c


def submit(client, char, strokes, submission_id=None, config=None):
    body = {
        "submission_id": submission_id or uuid.uuid4().hex,
        "char": char,
        "strokes": strokes,
    }
    if config is not None:
        body["config"] = config
    return client.post("/validate", json=body)


# ---------------- 基础 ----------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_seed_references_loaded(client):
    r = client.get("/references")
    chars = {item["char"] for item in r.json()["items"]}
    assert {"永", "二", "人"} <= chars


def test_reference_crud(client):
    strokes = [[[0, 0], [100, 0]], [[50, 0], [50, 100]]]
    r = client.put("/references/十", json={"strokes": strokes})
    assert r.status_code == 200 and r.json()["stroke_count"] == 2

    r = client.get("/references/十")
    assert r.status_code == 200 and r.json()["strokes"] == strokes

    r = client.delete("/references/十")
    assert r.status_code == 200
    assert client.get("/references/十").status_code == 404


def test_validate_unknown_char_404(client):
    r = submit(client, "龘", [[[0, 0], [1, 1]]])
    assert r.status_code == 404


def test_validate_rejects_empty_strokes(client):
    r = submit(client, "二", [])
    assert r.status_code == 422


# ---------------- 匹配行为 ----------------


def test_perfect_writing_passes(client):
    r = submit(client, "永", SEED_REFERENCES["永"])
    assert r.status_code == 200
    data = r.json()
    assert data["passed"] is True
    assert data["score"] == 100.0
    assert data["first_failed_stroke"] is None
    assert data["deviation_reasons"] == []
    assert data["replayed"] is False
    assert len(data["stroke_results"]) == 5


def test_scale_and_translation_invariant(client):
    scaled = [[[x * 3 + 500, y * 3 - 100] for x, y in s] for s in SEED_REFERENCES["永"]]
    data = submit(client, "永", scaled).json()
    assert data["passed"] is True and data["score"] == 100.0


def test_wrong_stroke_order_fails_at_first_stroke(client):
    ref = SEED_REFERENCES["二"]
    data = submit(client, "二", [ref[1], ref[0]]).json()
    assert data["passed"] is False
    assert data["first_failed_stroke"] == 1
    assert "START_POINT_OFFSET" in data["deviation_reasons"]


def test_reversed_stroke_detected(client):
    ref = SEED_REFERENCES["二"]
    data = submit(client, "二", [list(reversed(ref[0])), ref[1]]).json()
    assert data["passed"] is False
    assert data["first_failed_stroke"] == 1
    assert "REVERSED_STROKE" in data["deviation_reasons"]


def test_missing_stroke(client):
    ref = SEED_REFERENCES["二"]
    data = submit(client, "二", [ref[0]]).json()
    assert data["passed"] is False
    assert data["first_failed_stroke"] == 2
    assert data["deviation_reasons"] == ["MISSING_STROKE"]
    assert data["expected_strokes"] == 2 and data["received_strokes"] == 1
    assert data["score"] < 100.0


def test_extra_stroke(client):
    ref = SEED_REFERENCES["二"]
    data = submit(client, "二", ref + [[[10, 10], [90, 90]]]).json()
    assert data["passed"] is False
    assert data["first_failed_stroke"] == 3
    assert data["deviation_reasons"] == ["EXTRA_STROKE"]


def test_displaced_middle_stroke_blamed_correctly(client):
    ref = SEED_REFERENCES["永"]
    bad = [s[:] for s in ref]
    bad[2] = [[x + 30, y + 25] for x, y in ref[2]]
    data = submit(client, "永", bad).json()
    assert data["passed"] is False
    assert data["first_failed_stroke"] == 3
    assert "START_POINT_OFFSET" in data["deviation_reasons"]


def test_fork_tolerance_configurable(client):
    ref = [[[10, 30], [80, 30], [80, 90]]]  # 横折
    client.put("/references/厂", json={"strokes": ref})
    rounded = [[[10, 30], [60, 30], [80, 50], [80, 90]]]  # 拐角处切角

    strict = submit(
        client, "厂", rounded,
        config={"shape_tolerance": 0.05, "fork_extra_tolerance": 0.0},
    ).json()
    assert strict["passed"] is False
    assert "SHAPE_DEVIATION" in strict["deviation_reasons"]

    loose = submit(
        client, "厂", rounded,
        config={"shape_tolerance": 0.05, "fork_extra_tolerance": 0.15},
    ).json()
    assert loose["passed"] is True


# ---------------- 幂等性 ----------------


def test_same_submission_is_idempotent(client):
    sid = uuid.uuid4().hex
    body = {"submission_id": sid, "char": "二", "strokes": SEED_REFERENCES["二"]}
    r1 = client.post("/validate", json=body).json()
    r2 = client.post("/validate", json=body).json()
    assert r1["replayed"] is False
    assert r2["replayed"] is True
    assert r1["score"] == r2["score"]
    assert r1["created_at"] == r2["created_at"]
    assert {k: v for k, v in r1.items() if k != "replayed"} == {
        k: v for k, v in r2.items() if k != "replayed"
    }


def test_same_id_different_payload_conflicts(client):
    sid = uuid.uuid4().hex
    ref = SEED_REFERENCES["二"]
    client.post("/validate", json={"submission_id": sid, "char": "二", "strokes": ref})
    r = client.post("/validate", json={"submission_id": sid, "char": "二", "strokes": [ref[0]]})
    assert r.status_code == 409


def test_get_submission_replays_result(client):
    sid = uuid.uuid4().hex
    created = submit(client, "二", SEED_REFERENCES["二"], submission_id=sid).json()
    fetched = client.get(f"/submissions/{sid}")
    assert fetched.status_code == 200
    assert fetched.json()["replayed"] is True
    assert fetched.json()["score"] == created["score"]
    assert client.get("/submissions/nope").status_code == 404
