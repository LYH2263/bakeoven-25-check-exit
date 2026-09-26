"""排炉核对（采集 → 判定 → 入口）的测试。

覆盖：当前库种子三批、故意重叠夹具、端点相接夹具、采集缺项、判定不一致。
"""

import json
import os

os.environ["DATABASE_URL"] = "sqlite://"  # 须在导入 app 前设置，测试用内存库

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import schedule_check  # noqa: E402
from app.services.seed import seed_if_empty  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_if_empty(db)
    finally:
        db.close()


def _write_fixture(tmp_path, fx):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fx), encoding="utf-8")
    return str(path)


def _two_batch_fixture(a_end, b_start, b_end, b_ferment_end=None):
    """两段同炉批次：A [540, a_end)，B [b_start, b_end)，各占发酵+烘烤两段。"""
    a_ferment_end = 580
    if b_ferment_end is None:
        b_ferment_end = b_start + 20
    return {
        "endpoints": [
            {"code": "FX-A", "oven_id": 1, "start": 540, "end": a_end},
            {"code": "FX-B", "oven_id": 1, "start": b_start, "end": b_end},
        ],
        "gantt": [
            {"code": "FX-A", "oven_id": 1, "phase": "ferment", "start": 540, "end": a_ferment_end},
            {"code": "FX-A", "oven_id": 1, "phase": "bake", "start": a_ferment_end, "end": a_end},
            {"code": "FX-B", "oven_id": 1, "phase": "ferment", "start": b_start, "end": b_ferment_end},
            {"code": "FX-B", "oven_id": 1, "phase": "bake", "start": b_ferment_end, "end": b_end},
        ],
        "occupancies": [
            {"code": "FX-A", "oven_id": 1, "phase": "ferment", "start": 540, "end": a_ferment_end},
            {"code": "FX-A", "oven_id": 1, "phase": "bake", "start": a_ferment_end, "end": a_end},
            {"code": "FX-B", "oven_id": 1, "phase": "ferment", "start": b_start, "end": b_ferment_end},
            {"code": "FX-B", "oven_id": 1, "phase": "bake", "start": b_ferment_end, "end": b_end},
        ],
    }


def test_current_db_seed_passes_and_names_batches(capsys):
    code = schedule_check.main([])
    out = capsys.readouterr().out
    assert code == schedule_check.EXIT_OK
    for batch_code in ("BO-0900", "BO-1030", "BO-1000"):
        assert batch_code in out


def test_overlap_fixture_exits_nonzero_and_prints_pair(tmp_path, capsys):
    # 两段故意重叠：A [540,615) 与 B [600,640) 相交
    fx = _two_batch_fixture(a_end=615, b_start=600, b_end=640)
    code = schedule_check.main(["--source", "fixture", "--fixture", _write_fixture(tmp_path, fx)])
    out = capsys.readouterr().out
    assert code == schedule_check.EXIT_OVERLAP != 0
    assert "FX-A" in out and "FX-B" in out


def test_touching_fixture_passes(tmp_path, capsys):
    # 两段端点相接：A [540,615) 与 B [615,655) 仅端点相接
    fx = _two_batch_fixture(a_end=615, b_start=615, b_end=655)
    code = schedule_check.main(["--source", "fixture", "--fixture", _write_fixture(tmp_path, fx)])
    assert code == schedule_check.EXIT_OK


def test_missing_endpoints_exit_code_and_message(tmp_path, capsys):
    fx = _two_batch_fixture(a_end=615, b_start=615, b_end=655)
    del fx["endpoints"]
    code = schedule_check.main(["--source", "fixture", "--fixture", _write_fixture(tmp_path, fx)])
    out = capsys.readouterr().out
    assert code == schedule_check.EXIT_INCOMPLETE
    assert code != schedule_check.EXIT_OVERLAP
    assert "批次端点" in out


def test_missing_blocks_exit_code_and_message(tmp_path, capsys):
    fx = _two_batch_fixture(a_end=615, b_start=615, b_end=655)
    del fx["gantt"]
    code = schedule_check.main(["--source", "fixture", "--fixture", _write_fixture(tmp_path, fx)])
    out = capsys.readouterr().out
    assert code == schedule_check.EXIT_INCOMPLETE
    assert code != schedule_check.EXIT_OVERLAP
    assert "甘特色块" in out


def test_touching_pair_reported_as_overlap_is_mismatch(tmp_path, capsys):
    # 端点相接，但重叠判断的占用区间被改成相交 → 判定不一致
    fx = _two_batch_fixture(a_end=615, b_start=615, b_end=655)
    fx["occupancies"][2]["start"] = 610  # B 发酵段提前到 610，与 A 烘烤段相交
    code = schedule_check.main(["--source", "fixture", "--fixture", _write_fixture(tmp_path, fx)])
    out = capsys.readouterr().out
    assert code == schedule_check.EXIT_MISMATCH
    assert "端点相接" in out


def test_true_overlap_not_found_is_mismatch(tmp_path, capsys):
    # 端点真重叠，但重叠判断的占用区间被改成不相交 → 判定不一致
    fx = _two_batch_fixture(a_end=615, b_start=600, b_end=640)
    fx["occupancies"][2]["start"] = 615  # B 发酵段推迟到 615，不再相交
    fx["occupancies"][3]["start"] = 635
    fx["occupancies"][3]["end"] = 655
    code = schedule_check.main(["--source", "fixture", "--fixture", _write_fixture(tmp_path, fx)])
    out = capsys.readouterr().out
    assert code == schedule_check.EXIT_MISMATCH
    assert "未被找到" in out
