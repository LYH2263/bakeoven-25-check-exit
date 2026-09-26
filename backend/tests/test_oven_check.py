"""排炉核对的端到端测试：入口 → 采集 → 判定 → 退出码/点名输出。

当前库场景用 SQLite 内存库跑生产的 seed_if_empty 复现种子三批；
其余场景用 tests/fixtures 下的 JSON 夹具。
"""

import io
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.checks.runner import (
    EXIT_MISMATCH,
    EXIT_MISSING,
    EXIT_OK,
    EXIT_OVERLAP,
    run_check,
)
from app.database import Base
from app.services.seed import seed_if_empty

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_if_empty(session)
    yield session
    session.close()


def _run(argv, db=None) -> tuple[int, str]:
    out = io.StringIO()
    code = run_check(argv, db=db, out=out)
    return code, out.getvalue()


def test_current_seed_three_batches_pass(db):
    code, text = _run([], db=db)
    assert code == EXIT_OK
    # 输出点名种子三批
    for batch_code in ("BO-0900", "BO-1030", "BO-1000"):
        assert batch_code in text


def test_current_seed_is_repeatable(db):
    # 可重复执行：连续两次结论与退出码一致
    first = _run([], db=db)
    second = _run([], db=db)
    assert first == second
    assert first[0] == EXIT_OK


def test_fixture_overlap_nonzero_and_names_batches():
    code, text = _run(["--fixture", str(FIXTURES / "overlap_two_batches.json")])
    assert code == EXIT_OVERLAP
    assert code != 0
    assert "BO-3000" in text and "BO-3001" in text


def test_fixture_touching_endpoints_pass():
    code, text = _run(["--fixture", str(FIXTURES / "touching_endpoints.json")])
    assert code == EXIT_OK
    assert "端点相接" in text
    assert "发现重叠" not in text


def test_fixture_missing_endpoint_exit_distinct_from_overlap():
    code, text = _run(["--fixture", str(FIXTURES / "missing_endpoint.json")])
    assert code == EXIT_MISSING
    assert code != EXIT_OVERLAP
    assert "缺少端点" in text


def test_fixture_missing_swatch_exit_distinct_from_overlap():
    code, text = _run(["--fixture", str(FIXTURES / "missing_swatch.json")])
    assert code == EXIT_MISSING
    assert code != EXIT_OVERLAP
    assert "缺少色块" in text
