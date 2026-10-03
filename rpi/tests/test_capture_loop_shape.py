"""The capture loop must save a frame per keypress, not one frame per run.

`capture_dataset.py` had its save block dedented out of the capture loop. Because
`saved` only advanced after the loop finished, `while saved < args.count` could never
end on its own condition - only on Q/Esc - and then exactly one frame was written on
the way out. Every S press in between looped and discarded, so the tool looked like the
key did nothing at all (D-034).

These tests assert the shape of the loop rather than its behaviour, because the
behaviour needs a camera and a window. An indentation slip is invisible to every other
test in the suite and fatal to the tool.
"""

from __future__ import annotations

import ast
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "capture_dataset.py"


def _main_tree():
    return ast.parse(TOOL.read_text(encoding="utf-8"))


def _capture_loop(node: ast.FunctionDef) -> ast.While:
    """The loop guarded by `while saved < args.count`."""
    loops = []
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.While):
            continue
        test = candidate.test
        if not isinstance(test, ast.Compare):
            continue
        left = test.left
        if isinstance(left, ast.Name) and left.id == "saved":
            loops.append(candidate)
    assert len(loops) == 1, f"expected exactly one saved<count loop, found {len(loops)}"
    return loops[0]


def _main() -> ast.FunctionDef:
    for node in ast.walk(_main_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("no main() in capture_dataset.py")


def _lines(node) -> set[int]:
    """Line numbers of every node that has one - operators and names do not."""
    return {
        n.lineno
        for n in ast.walk(node)
        if getattr(n, "lineno", None) is not None
    }


# --- the regression itself ----------------------------------------------


def test_save_block_is_inside_the_capture_loop():
    """The bug: `stem = ...` and the writes sat after the loop's dedent."""
    loop = _capture_loop(_main())
    stem_lines = [
        n.lineno
        for n in ast.walk(_main())
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "stem" for t in n.targets
        )
    ]
    assert stem_lines, "the stem assignment has vanished entirely"
    assert all(lineno in _lines(loop) for lineno in stem_lines), (
        f"stem assigned at {stem_lines}, outside the capture loop at "
        f"{loop.lineno}-{loop.body[-1].lineno}"
    )


def test_saved_counter_advances_inside_the_loop():
    """`saved += 1` outside the loop means the loop condition never becomes false."""
    loop = _capture_loop(_main())
    increments = [
        n.lineno
        for n in ast.walk(_main())
        if isinstance(n, ast.AugAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "saved"
    ]
    assert increments, "saved is never incremented - the loop cannot terminate on count"
    assert all(lineno in _lines(loop) for lineno in increments), (
        f"saved += 1 at {increments} is outside the capture loop"
    )


def test_frame_write_is_inside_the_loop():
    loop = _capture_loop(_main())
    writes = [
        n.lineno
        for n in ast.walk(_main())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "imwrite"
    ]
    assert writes, "no image is written at all"
    assert all(lineno in _lines(loop) for lineno in writes), (
        f"cv2.imwrite at {writes} is outside the capture loop"
    )


def test_label_and_metadata_writes_are_inside_the_loop():
    loop = _capture_loop(_main())
    for name in ("write_yolo_label", "append_metadata"):
        calls = [
            n.lineno
            for n in ast.walk(_main())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name
        ]
        assert calls, f"{name} is never called"
        assert all(lineno in _lines(loop) for lineno in calls), (
            f"{name} at {calls} is outside the capture loop"
        )


def test_saved_log_line_is_inside_the_loop():
    loop = _capture_loop(_main())
    logs = [
        n.lineno
        for n in ast.walk(_main())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "info"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "saved %s (%d/%d)"
    ]
    assert logs
    assert all(lineno in _lines(loop) for lineno in logs)


def test_loop_actually_ends_with_a_save_increment():
    """The last statement of the loop body must be the save, not a bare continue."""
    loop = _capture_loop(_main())
    last = loop.body[-1]
    assert not isinstance(last, ast.Continue), (
        "the loop still ends on `continue`, so a save can never be the final action"
    )


# --- the saved frame is the assessed frame -------------------------------


def test_saved_image_is_written_from_the_assessment():
    """Not the loop's `frame` variable - the exact object assess_frame judged."""
    source = TOOL.read_text(encoding="utf-8")
    assert "view.frame," in source
    assert "imwrite(str(images_dir" not in source, (
        "imwrite should be a multi-line call taking view.frame explicitly"
    )
