#!/usr/bin/env python3
"""validate_delta.py 的轻量单元测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from validate_delta import validate_elements

ROOT = Path(__file__).resolve().parent.parent / "scripts"
VALIDATOR = ROOT / "validate_delta.py"
MERGER = ROOT / "merge_delta.py"
STRUCT_VALIDATOR = ROOT / "validate_structure.py"


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def base_story():
    return {
        "介绍": {"标题": "测试故事", "描述": ""},
        "角色集": [
            {"名称": "张三", "是否主角": True, "性别": 0, "年龄": 20, "生日": "0001-01-01T00:00:00", "所属阵营": ["青云门"], "关系": [], "分组": "主角团", "别名": [], "标签集": ["主角"], "介绍": "主角", "详情": {"首次章节": "0001", "最近章节": "0001"}}
        ],
        "事件集": [],
        "地点集": [
            {"名称": "青云山", "父级地点": "", "分组": "地理", "别名": [], "标签集": ["山门"], "介绍": "宗门所在", "详情": {"首次章节": "0001", "最近章节": "0001"}}
        ],
        "线索集": [],
        "阵营集": [
            {"名称": "青云门", "父级阵营": "", "座落地点": "青云山", "分组": "宗门", "别名": [], "标签集": ["宗门"], "介绍": "修行宗门", "详情": {"首次章节": "0001", "最近章节": "0001"}}
        ],
        "物品集": [], "其他事项集": []
    }


def good_delta():
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    return {
        "章节": "第001章 测试",
        "新增元素": {
            **empty,
            "角色集": [
                {"名称": "李四", "是否主角": False, "性别": 1, "年龄": 18, "生日": "0001-01-01T00:00:00", "所属阵营": ["青云门"], "关系": ["同门:张三"], "分组": "主角团", "别名": [], "标签集": ["同伴"], "介绍": "李四首次登场。", "详情": {"初登场": "事件:入门试炼", "提取理由": "首次登场并进入主角团关系网", "首次章节": "0001", "最近章节": "0001"}}
            ],
            "事件集": [
                {"名称": "入门试炼", "发生地点": "青云山", "参与成员": ["张三", "李四"], "重量级": 20, "目标事件": [], "分组": "0001-卷一", "时间": "0001-01-01T00:00:00", "别名": [], "标签集": ["试炼", "入门成长+考验推进+紧张+任务挑战"], "介绍": "张三与李四参加入门试炼。", "详情": {"提取理由": "推动张三进入宗门情节", "涉及章节": "0001"}}
            ]
        },
        "修改元素": empty
    }


def bad_delta_missing_ref():
    data = good_delta()
    data["新增元素"]["事件集"][0]["参与成员"].append("不存在的人")
    return data


def bad_delta_missing_evidence():
    data = good_delta()
    data["新增元素"]["角色集"][0]["详情"].pop("提取理由")
    return data


def bad_delta_missing_trace_for_new_element():
    data = good_delta()
    data["新增元素"]["角色集"][0]["详情"].pop("首次章节")
    return data


def bad_delta_missing_trace_for_modified_element():
    data = good_delta()
    data["新增元素"]["角色集"] = []
    data["修改元素"]["角色集"] = [
        {"名称": "张三", "详情": {"提取理由": "本章再次出现"}}
    ]
    return data


def governance_range_delta():
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    return {
        "章节范围": "第001章-第005章",
        "治理类型": "no_change",
        "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
        "证据范围": ["第001章", "第002章", "第003章", "第004章", "第005章"],
        "新增元素": empty,
        "修改元素": empty,
    }


def empty_governance_delta_without_metadata():
    """旧式空补丁：用以确认空补丁不再能跳过审计。"""
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    return {
        "章节范围": "第001章-第005章",
        "新增元素": empty,
        "修改元素": empty,
    }


def run(cmd):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=env)


def test_good_delta_passes():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, good_delta())
        result = run([sys.executable, str(VALIDATOR), str(story), str(delta)])
        assert result.returncode == 0, result.stdout


def test_bad_delta_fails():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, bad_delta_missing_ref())
        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert result.returncode != 0, result.stdout


def test_delta_without_evidence_fails_in_both_process_and_governance():
    # delta_extraction.md 规则5：提取理由 对所有模式都是必填；process 与 governance
    # 均应拒绝缺提取理由的元素。
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, bad_delta_missing_evidence())
        process = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert process.returncode != 0, process.stdout
        assert "提取理由" in process.stdout, process.stdout
        governance = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert governance.returncode != 0, governance.stdout
        assert "提取理由" in governance.stdout, governance.stdout


def test_process_delta_rejects_new_element_missing_trace_fields():
    """新增档案必须有可核对的来源；过程阶段不能靠默认值补造通过。"""
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, bad_delta_missing_trace_for_new_element())
        result = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert result.returncode != 0, result.stdout
        assert "[质量字段:/新增元素/角色集/0/详情/首次章节]" in result.stdout, result.stdout


def test_process_delta_allows_modified_element_as_patch_without_trace_fields():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, bad_delta_missing_trace_for_modified_element())
        result = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert result.returncode == 0, result.stdout


def test_process_delta_modified_event_does_not_require_time_matching_involved_chapters():
    current = base_story()
    current["事件集"] = [{
        "名称": "入门试炼", "发生地点": "青云山", "参与成员": ["张三"], "重量级": 20,
        "目标事件": [], "分组": "0001-卷一", "时间": "0001-01-01T00:00:00",
        "别名": [], "标签集": [], "介绍": "试炼", "详情": {"涉及章节": "0001"},
    }]
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    delta = {
        "章节": "第001章 测试",
        "新增元素": empty,
        "修改元素": {
            **empty,
            "事件集": [{"名称": "入门试炼", "详情": {"提取理由": "补充描述", "涉及章节": "0001"}}],
        },
    }
    errors, warnings = validate_elements(current, delta, "process")
    assert not errors
    assert any("时间" in warning or "涉及章节" in warning for warning in warnings), warnings


def test_process_delta_modified_event_does_not_require_involved_chapters():
    current = base_story()
    current["事件集"] = [{"名称": "既有事件", "详情": {"涉及章节": "0001"}}]
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
    delta = {
        "章节": "第001章 测试",
        "新增元素": empty,
        "修改元素": {
            **empty,
            "事件集": [{
                "名称": "既有事件",
                "时间": "0001-01-01T00:00:00",
                "详情": {"提取理由": "补充描述"},
            }],
        },
    }
    errors, warnings = validate_elements(current, delta, "process")
    assert not errors
    assert any("涉及章节" in warning for warning in warnings), warnings


def test_governance_range_delta_passes_only_in_governance_mode():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, base_story())
        write_json(delta, governance_range_delta())
        governance = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        process = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert governance.returncode == 0, governance.stdout
        assert process.returncode != 0, process.stdout


def test_empty_governance_delta_without_no_change_metadata_fails():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, base_story())
        write_json(delta, empty_governance_delta_without_metadata())
        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert result.returncode != 0, result.stdout
        assert "治理类型=no_change" in result.stdout, result.stdout
        assert "质量说明" in result.stdout, result.stdout
        assert "证据范围" in result.stdout, result.stdout


def test_no_change_metadata_only_required_when_governance_workload_empty():
    """非空治理补丁不应被新规则误伤：仅靠新增元素也能通过 governance 校验。"""
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, base_story())
        patch = good_delta()
        patch.pop("章节")
        patch["章节范围"] = "第001章-第005章"
        write_json(delta, patch)
        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert result.returncode == 0, result.stdout


def test_governance_delta_allows_unregistered_event_location_as_warning():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, base_story())
        patch = good_delta()
        patch.pop("章节")
        patch["章节范围"] = "第001章-第005章"
        patch["新增元素"]["事件集"][0]["发生地点"] = "青云山山门口"
        write_json(delta, patch)

        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])

        assert result.returncode == 0, result.stdout
        assert "青云山山门口" in result.stdout, result.stdout
        assert "警告" in result.stdout, result.stdout


def _story_with_event():
    s = base_story()
    s["事件集"] = [{
        "名称": "入门试炼", "发生地点": "青云山", "参与成员": ["张三"],
        "重量级": 20, "目标事件": [], "分组": "00000001-卷一开篇", "时间": "0001-01-01T00:00:00",
        "别名": [], "标签集": ["试炼"], "介绍": "张三参加入门试炼。",
        "详情": {"涉及章节": "0001"},
    }]
    return s


def _set_field_patch(field, value):
    empty = {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    return {
        "章节范围": "第001章-第005章",
        "治理操作": {"设置字段": [{"类型": "事件", "名称": "入门试炼", "字段": field, "值": value}]},
        "新增元素": empty,
        "修改元素": empty,
    }


def test_governance_set_field_cannot_mutate_event_time_or_detail():
    # §133：对既有事件只允许重分类，不得改 时间/涉及章节/数组位置。
    # 设置字段 覆写 时间 或 整块 详情(含涉及章节) 均应被治理校验拒绝。
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, _story_with_event())

        write_json(delta, _set_field_patch("时间", "0002-01-01T00:00:00"))
        rc_time = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert rc_time.returncode != 0, rc_time.stdout
        assert "时间" in rc_time.stdout and "不可变" in rc_time.stdout, rc_time.stdout

        write_json(delta, _set_field_patch("详情", {"涉及章节": "0002"}))
        rc_detail = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert rc_detail.returncode != 0, rc_detail.stdout
        assert "详情" in rc_detail.stdout and "不可变" in rc_detail.stdout, rc_detail.stdout


def test_governance_set_field_allows_event_reclassification():
    # 设置字段 改事件的非不可变字段（如 分组，即重分类）应放行。
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, _story_with_event())
        write_json(delta, _set_field_patch("分组", "00000002-宗门考核"))

        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])

        assert result.returncode == 0, result.stdout


def test_governance_delta_still_rejects_unregistered_event_participant():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        write_json(story, base_story())
        patch = good_delta()
        patch.pop("章节")
        patch["章节范围"] = "第001章-第005章"
        patch["新增元素"]["事件集"][0]["参与成员"] = ["张三", "未注册角色"]
        write_json(delta, patch)

        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])

        assert result.returncode != 0, result.stdout
        assert "未注册角色" in result.stdout, result.stdout


def test_governance_delta_accepts_registered_child_location():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction_001-005.json"
        current = base_story()
        current["地点集"].append({
            "名称": "青云山山门口",
            "父级地点": "青云山",
            "分组": "地理/入口",
            "别名": [],
            "标签集": [],
            "介绍": "青云山入口。",
            "详情": {"首次章节": "0001", "最近章节": "0001"},
        })
        patch = good_delta()
        patch.pop("章节")
        patch["章节范围"] = "第001章-第005章"
        patch["新增元素"]["事件集"][0]["发生地点"] = "青云山山门口"
        write_json(story, current)
        write_json(delta, patch)

        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])

        assert result.returncode == 0, result.stdout
        assert "引用了不存在" not in result.stdout, result.stdout


def test_chapter_check_after_merge_passes():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, base_story())
        write_json(delta, good_delta())
        merge = run([sys.executable, str(MERGER), str(story), str(delta)])
        assert merge.returncode == 0, merge.stdout
        check = run([sys.executable, str(STRUCT_VALIDATOR), "--chapter-check", str(story), str(delta)])
        assert check.returncode == 0, check.stdout


def test_governance_cannot_change_existing_event_temporal_fields():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction.json"
        current = base_story()
        current["事件集"] = [{
            "名称": "入门试炼", "发生地点": "青云山", "参与成员": ["张三"], "重量级": 20,
            "目标事件": [], "分组": "00000010-入门试炼主线考验成长悬念", "时间": "0001-01-01T00:00:00",
            "别名": [], "标签集": [], "介绍": "试炼", "详情": {"涉及章节": "0001"},
        }]
        patch = {"章节范围": "第001章-第005章", "新增元素": {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}, "修改元素": {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}}
        patch["修改元素"]["事件集"] = [{"名称": "入门试炼", "详情": {"提取理由": "错误的时序改写", "涉及章节": "0002"}}]
        write_json(story, current)
        write_json(delta, patch)
        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert result.returncode != 0, result.stdout
        assert "不得在普通治理中修改" in result.stdout, result.stdout


def test_process_delta_accepts_minimal_named_patch():
    current = base_story()
    delta = {
        "章节": "第002章 测试",
        "新增元素": {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]},
        "修改元素": {
            "角色集": [{"名称": "张三", "详情": {"提取理由": "本章继续出现", "最近章节": "0002"}}],
            "事件集": [],
            "地点集": [],
            "线索集": [],
            "阵营集": [],
            "物品集": [],
        },
    }
    errors, warnings = validate_elements(current, delta, "process")
    assert not errors
    assert isinstance(warnings, list)


def test_process_delta_still_rejects_element_without_name():
    current = base_story()
    delta = good_delta()
    delta["新增元素"]["角色集"][0].pop("名称")
    errors, _ = validate_elements(current, delta, "process")
    assert any("名称 必须是非空字符串" in error for error in errors), errors


def test_process_delta_still_rejects_non_object_detail():
    current = base_story()
    delta = good_delta()
    delta["新增元素"]["角色集"][0]["详情"] = "不是对象"
    errors, _ = validate_elements(current, delta, "process")
    assert any("详情 必须是对象" in error for error in errors), errors


def test_process_delta_still_rejects_invalid_detail_value_type():
    current = base_story()
    delta = good_delta()
    delta["新增元素"]["角色集"][0]["详情"] = {"提取理由": "首次登场", "数值": 123}
    errors, _ = validate_elements(current, delta, "process")
    assert any("值类型必须是 string 或 string[]" in error for error in errors), errors


def test_governance_delta_still_requires_trace_fields_for_traceable_elements():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "correction.json"
        patch = bad_delta_missing_trace_for_new_element()
        patch.pop("章节")
        patch["章节范围"] = "第001章-第005章"
        write_json(story, base_story())
        write_json(delta, patch)
        result = run([sys.executable, str(VALIDATOR), "--mode", "governance", str(story), str(delta)])
        assert result.returncode != 0, result.stdout
        assert "首次章节" in result.stdout, result.stdout


def test_process_delta_rejects_incomplete_new_element_without_modifying_story():
    """不完整新增必须先修复，不能通过合并补默认值伪装成完整档案。"""
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        current = base_story()
        patch = {
            "章节": "第002章 测试",
            "新增元素": {
                "角色集": [{"名称": "王五", "详情": {"提取理由": "本章首次出现", "首次章节": "0002", "最近章节": "0002"}}],
                "事件集": [],
                "地点集": [],
                "线索集": [],
                "阵营集": [],
                "物品集": [],
            },
            "修改元素": {k: [] for k in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]},
        }
        write_json(story, current)
        write_json(delta, patch)
        original_story = story.read_bytes()
        original_delta = delta.read_bytes()

        validate = run([sys.executable, str(VALIDATOR), "--mode", "process", str(story), str(delta)])
        assert validate.returncode != 0, validate.stdout
        assert "[质量字段:/新增元素/角色集/0/介绍]" in validate.stdout, validate.stdout
        assert story.read_bytes() == original_story
        assert delta.read_bytes() == original_delta


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            func()
            print(f"PASS {name}")
