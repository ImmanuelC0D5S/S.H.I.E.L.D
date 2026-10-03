"""Tests for the WIDER FACE annotation parser.

The parser is pure text handling, so it is fully testable without the 3.4 GB
dataset. The formats it must survive are documented quirks rather than
hypotheticals: a byte-order mark, annotations for images that are not on disk,
faces flagged ``invalid``, and faces too small to be useful.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from add_general_faces import collect_wider, parse_wider  # noqa: E402

SIMPLE = """0--Parade/0_Parade_1.jpg
2
0 0 0 0 100 100 260 300
0 0 0 0 400 400 500 620
1--Party/1_Party_1.jpg
1
0 0 0 0 10 10 60 60
"""


def write(tmp_path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path = tmp_path / "bbx.txt"
    path.write_text(text, encoding=encoding)
    return path


# -- parsing ---------------------------------------------------------------


def test_parses_two_images_with_correct_box_counts(tmp_path):
    parsed = parse_wider(write(tmp_path, SIMPLE))
    assert len(parsed) == 2
    assert parsed[0][0] == "0--Parade/0_Parade_1.jpg"
    assert len(parsed[0][1]) == 2
    assert parsed[1][0] == "1--Party/1_Party_1.jpg"


def test_box_coordinates_are_preserved(tmp_path):
    parsed = parse_wider(write(tmp_path, SIMPLE))
    assert parsed[0][1][0] == (100.0, 100.0, 260.0, 300.0)


def test_byte_order_mark_does_not_lose_the_first_block(tmp_path):
    """A BOM makes the first line fail its digit test; that would silently drop a
    whole image block, so utf-8-sig decoding is required."""
    parsed = parse_wider(write(tmp_path, SIMPLE, encoding="utf-8-sig"))
    assert len(parsed) == 2
    assert len(parsed[0][1]) == 2


def test_declared_count_matching_boxes(tmp_path):
    """Each face row has 8 fields; extra fields must not confuse the parser."""
    text = "0--A/a.jpg\n1\n0 0 0 0 10 20 30 40\n"
    parsed = parse_wider(write(tmp_path, text))
    assert len(parsed) == 1
    assert parsed[0][1] == [(10.0, 20.0, 30.0, 40.0)]


def test_empty_file_yields_nothing(tmp_path):
    assert parse_wider(write(tmp_path, "")) == []


def test_missing_annotation_file_exits(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        parse_wider(tmp_path / "nope.txt")


# -- invalid faces ---------------------------------------------------------


def test_invalid_flagged_faces_are_dropped(tmp_path):
    """An unrecognisable face teaches the detector to find faces that are not faces."""
    text = "0--A/a.jpg\n2\n1 0 0 0 10 10 60 60\n0 0 0 0 100 100 200 200\n"
    parsed = parse_wider(write(tmp_path, text))
    assert len(parsed[0][1]) == 1
    assert parsed[0][2] == 1  # one dropped as invalid


# -- collection ------------------------------------------------------------


def test_collect_skips_images_that_are_not_on_disk(tmp_path):
    parsed_path = write(tmp_path, SIMPLE)
    images = tmp_path / "images"
    (images / "0--Parade").mkdir(parents=True)
    from PIL import Image

    Image.new("RGB", (600, 400)).save(images / "0--Parade" / "0_Parade_1.jpg")

    usable = collect_wider(parsed_path, images, min_face=40, include_invalid=False)
    assert len(usable) == 1


def test_collect_applies_min_face(tmp_path):
    """Faces below the pixel floor are dropped; a 20px face is not useful."""
    parsed_path = write(tmp_path, SIMPLE)
    images = tmp_path / "images"
    for sub in ("0--Parade", "1--Party"):
        (images / sub).mkdir(parents=True)
        from PIL import Image

        Image.new("RGB", (600, 400)).save(images / sub / f"{sub[3:]}.jpg")
    # rename to match annotations
    (images / "0--Parade" / "Parade.jpg").rename(images / "0--Parade" / "0_Parade_1.jpg")
    (images / "1--Party" / "Party.jpg").rename(images / "1--Party" / "1_Party_1.jpg")

    usable = collect_wider(parsed_path, images, min_face=80, include_invalid=False)
    # the 50x50 face in the second image is dropped, leaving only the first image
    assert len(usable) == 1
    assert len(usable[0][1]) == 2


def test_collect_exits_when_nothing_usable(tmp_path):
    parsed_path = write(tmp_path, SIMPLE)
    images = tmp_path / "images"
    images.mkdir()
    with pytest.raises(SystemExit, match="no usable images"):
        collect_wider(parsed_path, images, min_face=40, include_invalid=False)