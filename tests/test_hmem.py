"""H-MEM engine conformance: memory that outlives the process that wrote it."""

from app.engines.hmem.engine import LEVEL_EPISODE, LEVEL_LESSON, LEVEL_STANDING, HMem


def test_cross_session_recall(tmp_path):
    """A fresh instance reading the same store is a new session."""
    store = tmp_path / "store.json"

    session_one = HMem(store)
    session_one.remember(
        "shelter:S-14",
        "S-14 filled 4h earlier than forecast",
        tags=["ian_2022", "lee"],
        run_id="run-1",
        confidence=0.8,
    )
    session_one.flush()

    session_two = HMem(store)
    recalled = session_two.recall(["ian_2022", "lee"])
    assert any("S-14 filled" in item.statement for item in recalled)
    assert recalled[0].run_id == "run-1"


def test_recall_requires_tag_overlap_except_for_standing_memory(tmp_path):
    memory = HMem(tmp_path / "store.json")
    memory.remember("a", "about ian", tags=["ian_2022"])
    memory.remember("b", "about a different storm", tags=["nicole_2022"])
    memory.remember("operator", "EOC prefers staging west of I-75", level=LEVEL_STANDING, tags=["any"])

    statements = [i.statement for i in memory.recall(["ian_2022"])]
    assert "about ian" in statements
    assert "about a different storm" not in statements
    # Standing memory is operator policy and surfaces regardless of tags.
    assert "EOC prefers staging west of I-75" in statements


def test_repeated_episodes_promote_to_a_lesson(tmp_path):
    memory = HMem(tmp_path / "store.json")
    memory.remember("shelter:S-14", "filled early", tags=["ian_2022"], run_id="run-1")
    assert memory.consolidate() == []  # one sighting is not a pattern

    memory.remember("shelter:S-14", "filled early again", tags=["ian_2022"], run_id="run-2")
    promoted = memory.consolidate()

    assert len(promoted) == 1
    assert promoted[0].level == LEVEL_LESSON
    assert promoted[0].confidence > 0.6
    assert len(promoted[0].evidence) == 2
    assert memory.consolidate() == []  # idempotent


def test_recall_ranks_lessons_above_raw_episodes(tmp_path):
    memory = HMem(tmp_path / "store.json")
    memory.remember("x", "raw episode", level=LEVEL_EPISODE, tags=["ian_2022"], confidence=0.9)
    memory.remember("y", "consolidated lesson", level=LEVEL_LESSON, tags=["ian_2022"], confidence=0.7)

    assert memory.recall(["ian_2022"], limit=1)[0].statement == "consolidated lesson"


def test_store_survives_a_corrupt_file(tmp_path):
    store = tmp_path / "store.json"
    store.write_text("{ not json", encoding="utf-8")
    memory = HMem(store)  # must not raise
    assert memory.all_items() == []
    memory.remember("a", "b", tags=["t"])
    memory.flush()
    assert HMem(store).digest()["total"] == 1
