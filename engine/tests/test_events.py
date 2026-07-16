from events import Event, EventBus


def test_emit_calls_listeners():
    bus = EventBus()
    got = []
    bus.subscribe(lambda e: got.append(e))
    bus.emit(Event("stage_start", stage="chapter_analysis"))
    assert len(got) == 1
    assert got[0].type == "stage_start"
    assert got[0].stage == "chapter_analysis"
    assert got[0].ts > 0


def test_listener_exception_does_not_break_emit():
    def _raise(_e):
        raise RuntimeError("boom")

    bus = EventBus()
    ok = []
    bus.subscribe(_raise)
    bus.subscribe(lambda e: ok.append(e.type))
    bus.emit(Event("stage_done"))
    assert ok == ["stage_done"]


def test_to_dict_roundtrip():
    e = Event("progress", stage="chapter_delta", detail="3/10", data={"done": 3})
    d = EventBus.to_dict(e)
    assert d["type"] == "progress"
    assert d["data"]["done"] == 3
