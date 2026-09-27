import shutil
import tempfile
import unittest
from pathlib import Path

from explorer.charades import load_dataset, load_taxonomy, parse_actions, split_scene

FIXTURES = Path(__file__).parent / "fixtures" / "charades_mini"


class TaxonomyTest(unittest.TestCase):
    def test_classes_resolve_verb_and_object(self):
        taxonomy = load_taxonomy(FIXTURES)
        self.assertEqual(len(taxonomy.classes), 7)
        door = taxonomy.classes["c001"]
        self.assertEqual((door.label, door.verb, door.object), ("Opening a door", "open", "door"))

    def test_none_object_becomes_empty(self):
        self.assertEqual(load_taxonomy(FIXTURES).classes["c006"].object, "")


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(FIXTURES)
        cls.clips = {c.id: c for c in cls.dataset.clips}

    def test_loads_both_splits(self):
        splits = [c.split for c in self.dataset.clips]
        self.assertEqual((splits.count("train"), splits.count("test")), (5, 2))
        self.assertEqual(self.dataset.warnings, [])

    def test_row_fields(self):
        clip = self.clips["AAA01"]
        self.assertEqual(clip.script, "A person is cooking at the stove, then drinks from a cup.")
        self.assertEqual(clip.objects, ["food", "stove", "cup"])
        self.assertEqual(len(clip.descriptions), 2)
        self.assertEqual((clip.quality, clip.relevance, clip.verified), (6, 7, True))
        self.assertEqual(clip.length, 18.0)
        self.assertEqual(clip.issues, [])

    def test_segments_sorted_and_labeled(self):
        segments = self.clips["AAA02"].segments
        self.assertEqual([s.class_id for s in segments], ["c001", "c006"])
        self.assertEqual((segments[0].label, segments[0].verb), ("Opening a door", "open"))
        self.assertAlmostEqual(segments[0].duration, 3.5)

    def test_scene_description_split_off(self):
        clip = self.clips["AAA02"]
        self.assertEqual(clip.scene, "Home Office / Study")
        self.assertEqual(clip.scene_detail, "A room in a house used for work")
        self.assertEqual(split_scene("Kitchen"), ("Kitchen", ""))

    def test_empty_actions_and_unverified_are_flagged(self):
        clip = self.clips["AAA03"]
        self.assertEqual(clip.segments, [])
        self.assertEqual(clip.issues, ["no_actions", "unverified"])

    def test_bad_entries_are_flagged_not_dropped(self):
        clip = self.clips["AAA04"]
        self.assertEqual(
            clip.issues,
            ["inverted_segment", "unknown_class", "malformed_segment", "missing_rating"],
        )
        # malformed "garbage" is dropped; unknown c099 and inverted c003 are kept
        self.assertEqual(sorted(s.class_id for s in clip.segments), ["c003", "c005", "c099"])
        unknown = next(s for s in clip.segments if s.class_id == "c099")
        self.assertEqual(unknown.label, "c099")

    def test_overrun_tolerance(self):
        self.assertNotIn("segment_overrun", self.clips["AAA02"].issues)  # 0.4 s over
        self.assertIn("segment_overrun", self.clips["AAA05"].issues)  # 4.5 s over

    def test_coverage_merges_overlaps(self):
        # [0, 12.5] + [11, 15.2] -> 15.2 s of 18 s
        self.assertAlmostEqual(self.clips["AAA01"].coverage, round(15.2 / 18, 4))
        self.assertEqual(self.clips["AAA03"].coverage, 0.0)
        self.assertEqual(self.clips["AAA05"].coverage, 1.0)  # clamped to length

    def test_parse_actions_directly(self):
        taxonomy = load_taxonomy(FIXTURES)
        segments, issues = parse_actions(" c001 1 2 ; ;c002 x 3", taxonomy)
        self.assertEqual([s.class_id for s in segments], ["c001"])
        self.assertEqual(issues, {"malformed_segment"})


class RobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in FIXTURES.iterdir():
            shutil.copy(f, self.tmp / f.name)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_duplicate_ids_are_skipped_with_warning(self):
        test_csv = self.tmp / "Charades_v1_test.csv"
        dup = "AAA01,S009,Garage,5,5,Yes,dup,,dup,,5.00\n"
        test_csv.write_text(test_csv.read_text() + dup)
        dataset = load_dataset(self.tmp)
        self.assertEqual(len(dataset.clips), 7)
        self.assertEqual(dataset.warnings, ["Charades_v1_test.csv: duplicate id AAA01 skipped"])

    def test_missing_column_is_a_clear_error(self):
        (self.tmp / "Charades_v1_test.csv").write_text("id,scene\nX,Kitchen\n")
        with self.assertRaisesRegex(ValueError, "missing columns"):
            load_dataset(self.tmp)


if __name__ == "__main__":
    unittest.main()
