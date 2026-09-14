from pipeline_state import DONE, STAGES, PipelineState


def test_load_fresh(tmp_path):
    st = PipelineState.load(tmp_path)
    assert st.current_stage == STAGES[0]
    assert st.completed == []
    assert st.started_at > 0


def test_save_load_roundtrip(tmp_path):
    st = PipelineState.load(tmp_path)
    st.bump_attempt("chapter_analysis")
    st.fullbook_completed_tasks.append("summary")
    st.mark_completed("chapter_analysis")
    st.save()
    st2 = PipelineState.load(tmp_path)
    assert st2.current_stage == STAGES[1]
    assert "chapter_analysis" in st2.completed
    assert st2.fullbook_completed_tasks == ["summary"]


def test_mark_completed_advances(tmp_path):
    st = PipelineState.load(tmp_path)
    st.mark_completed("chapter_analysis")
    assert st.current_stage == "chapter_delta"


def test_done_after_all(tmp_path):
    st = PipelineState.load(tmp_path)
    for s in STAGES:
        st.mark_completed(s)
    assert st.is_done()
    assert st.current_stage == DONE


def test_bump_attempt_and_reset(tmp_path):
    st = PipelineState.load(tmp_path)
    assert st.bump_attempt("final_pack") == 1
    assert st.bump_attempt("final_pack") == 2
    st.reset_stage("final_pack")
    assert st.attempts("final_pack") == 0


def test_record_signature_stagnant(tmp_path):
    st = PipelineState.load(tmp_path)
    assert st.record_signature("chapter_delta", "sig1") == 0
    assert st.record_signature("chapter_delta", "sig1") == 1
    assert st.record_signature("chapter_delta", "sig1") == 2
    assert st.record_signature("chapter_delta", "sig2") == 0


def test_corrupt_state_falls_back_to_fresh(tmp_path):
    (tmp_path / "质量治理" / "编排").mkdir(parents=True)
    (tmp_path / "质量治理" / "编排" / "状态.json").write_text("{not json", encoding="utf-8")
    st = PipelineState.load(tmp_path)
    assert st.current_stage == STAGES[0]
