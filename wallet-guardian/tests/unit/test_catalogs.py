import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_acceptance_documents_match_offline_tests():
    v1 = json.loads((ROOT / "tests" / "v1_acceptance_cases.json").read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / "tests" / "v2_acceptance_cases.json").read_text(encoding="utf-8"))
    workbench = json.loads((ROOT / "tests" / "workbench_acceptance_cases.json").read_text(encoding="utf-8"))
    assert len(v1) == 56
    assert len(v2) == 72
    assert len(workbench) == 31
    v1_src = (ROOT / "tests" / "unit" / "test_v1_acceptance.py").read_text(encoding="utf-8").lower()
    v2_src = (ROOT / "tests" / "unit" / "test_v2_acceptance.py").read_text(encoding="utf-8").lower()
    workbench_src = (ROOT / "tests" / "unit" / "test_workbench.py").read_text(encoding="utf-8").lower()
    workbench_md = (ROOT / "tests" / "workbench_acceptance_cases.md").read_text(encoding="utf-8")
    for case in v1:
        assert f"test_{case['id'].lower()}_" in v1_src
    for case in v2:
        assert f"test_{case['id'].lower()}_" in v2_src
    for case in workbench:
        assert case["group"] == "workbench"
        assert case["layer"] == "offline"
        assert f"test_{case['id'].lower()}_" in workbench_src
        assert case["id"] in workbench_md
