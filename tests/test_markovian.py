"""Markovian engine conformance: a long task that does not grow its context."""

import json

from app.engines.markovian.engine import MAX_CARRYOVER_CHARS, MarkovianRunner, compress


def test_context_stays_bounded_across_24_steps():
    """Each step dumps far more than the cap; context must stay flat anyway."""
    runner = MarkovianRunner()

    def step(index, label, context):
        return {
            "summary": f"hour {label}",
            "notes": "x" * 20_000,
            "open_issues": [f"issue-{index}-{k}" for k in range(200)],
            "digest": {"beliefs": index * 17, "decisions": index},
        }

    runner.run([f"h{i}" for i in range(24)], step)

    assert len(runner.steps) == 24
    sizes = runner.context_sizes
    # Compare steady-state steps; step 1 has no carryover yet.
    assert max(sizes[1:]) <= min(sizes[1:]) * 1.2
    assert runner.bounded()
    assert runner.digest()["steps"] == 24


def test_step_n_runs_with_bounded_context():
    runner = MarkovianRunner()
    seen = {}

    def step(index, label, context):
        seen[index] = len(json.dumps(context, default=str))
        return {"notes": "y" * 9_000, "open_issues": list(range(100))}

    runner.run([f"h{i}" for i in range(40)], step)
    assert seen[40] <= MAX_CARRYOVER_CHARS + 500
    assert seen[40] <= seen[2] * 1.2


def test_compress_caps_lists_strings_and_total_size():
    squeezed = compress({"notes": "z" * 5_000, "open_issues": list(range(500)), "step": 7})
    assert len(squeezed["notes"]) <= 160
    assert len(squeezed["open_issues"]) <= 6
    assert squeezed["step"] == 7
    assert len(json.dumps(squeezed)) <= MAX_CARRYOVER_CHARS


def test_hmem_context_injected_once_at_step_one():
    """The COMBINE contract: memory enters at step 1, not on every step."""
    runner = MarkovianRunner()
    saw_memory = []

    def step(index, label, context):
        saw_memory.append("memory" in context)
        return {"summary": "ok"}

    runner.run(["a", "b", "c", "d"], step, initial_memory={"lessons": ["S-14 fills early"]})
    assert saw_memory == [True, False, False, False]
