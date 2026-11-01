import tempfile
import unittest
from pathlib import Path

from steam_root import (
    SteamRootDetector,
    SteamRootSettings,
    is_steam_root,
    resolve_steam_root,
)


class StaticCandidateSource:
    def __init__(self, *paths):
        self._paths = paths

    def candidates(self):
        return list(self._paths)


class ExplodingCandidateSource:
    def candidates(self):
        raise OSError("registry unavailable")


class SteamRootTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_library(self, name: str) -> Path:
        """A secondary library folder: games, but no Steam client."""
        path = self.root / name
        (path / "steamapps").mkdir(parents=True)
        return path

    def make_installation(self, name: str) -> Path:
        path = self.make_library(name)
        (path / "appcache").mkdir()
        return path


class IsSteamRootTests(SteamRootTestCase):
    def test_a_folder_with_steamapps_is_a_steam_root(self):
        self.assertTrue(is_steam_root(self.make_library("Steam")))

    def test_a_folder_without_steamapps_is_not(self):
        self.assertFalse(is_steam_root(self.root / "Documents"))

    def test_blank_and_missing_values_are_not(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertFalse(is_steam_root(value))


class SteamRootDetectorTests(SteamRootTestCase):
    def test_the_first_valid_candidate_wins(self):
        missing = self.root / "nope"
        installation = self.make_installation("Steam")
        detector = SteamRootDetector([StaticCandidateSource(missing, installation)])
        self.assertEqual(detector.detect(), installation)

    def test_a_real_installation_is_preferred_over_a_bare_library_folder(self):
        library = self.make_library("SteamLibrary")
        installation = self.make_installation("Steam")
        detector = SteamRootDetector([StaticCandidateSource(library, installation)])
        self.assertEqual(detector.detect(), installation)

    def test_a_bare_library_folder_is_used_when_no_installation_is_found(self):
        library = self.make_library("SteamLibrary")
        detector = SteamRootDetector([StaticCandidateSource(library)])
        self.assertEqual(detector.detect(), library)

    def test_nothing_is_returned_when_no_candidate_holds_a_library(self):
        detector = SteamRootDetector([StaticCandidateSource(self.root / "nope")])
        self.assertIsNone(detector.detect())

    def test_a_failing_source_does_not_stop_the_remaining_ones(self):
        installation = self.make_installation("Steam")
        detector = SteamRootDetector([ExplodingCandidateSource(), StaticCandidateSource(installation)])
        with self.assertLogs("steam_root", level="ERROR"):
            self.assertEqual(detector.detect(), installation)


class ResolveSteamRootTests(SteamRootTestCase):
    def test_a_usable_configured_value_wins_over_detection(self):
        configured = self.make_installation("Configured")
        detector = SteamRootDetector([StaticCandidateSource(self.make_installation("Detected"))])
        self.assertEqual(resolve_steam_root(configured, detector), configured)

    def test_detection_is_used_when_nothing_is_configured(self):
        detected = self.make_installation("Detected")
        detector = SteamRootDetector([StaticCandidateSource(detected)])
        self.assertEqual(resolve_steam_root(None, detector), detected)
        self.assertEqual(resolve_steam_root("  ", detector), detected)

    def test_a_stale_configured_value_falls_back_to_detection(self):
        detected = self.make_installation("Detected")
        detector = SteamRootDetector([StaticCandidateSource(detected)])
        with self.assertLogs("steam_root", level="WARNING"):
            self.assertEqual(resolve_steam_root(self.root / "gone", detector), detected)

    def test_an_unusable_configured_value_is_kept_when_detection_finds_nothing(self):
        stale = self.root / "gone"
        detector = SteamRootDetector([StaticCandidateSource()])
        with self.assertLogs("steam_root", level="ERROR"):
            self.assertEqual(resolve_steam_root(stale, detector), stale)

    def test_the_fallback_is_used_when_there_is_nothing_to_go_on(self):
        fallback = self.root / "fallback"
        detector = SteamRootDetector([StaticCandidateSource()])
        with self.assertLogs("steam_root", level="ERROR"):
            self.assertEqual(resolve_steam_root(None, detector, fallback=fallback), fallback)


class SteamRootSettingsTests(SteamRootTestCase):
    def test_validity_reflects_the_active_folder(self):
        self.assertTrue(SteamRootSettings(self.make_installation("Steam")).is_valid())
        self.assertFalse(SteamRootSettings(self.root / "gone").is_valid())

    def test_a_blank_value_means_auto_detect(self):
        settings = SteamRootSettings(self.root)
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(settings.validate(value))

    def test_a_valid_folder_is_normalised(self):
        installation = self.make_installation("Steam")
        settings = SteamRootSettings(self.root)
        self.assertEqual(settings.validate(f'  "{installation}"  '), installation)

    def test_a_missing_folder_is_rejected(self):
        settings = SteamRootSettings(self.root)
        with self.assertRaisesRegex(ValueError, "Folder not found"):
            settings.validate(self.root / "gone")

    def test_a_folder_without_steamapps_is_rejected(self):
        settings = SteamRootSettings(self.root)
        with self.assertRaisesRegex(ValueError, "steamapps"):
            settings.validate(self.root)


if __name__ == "__main__":
    unittest.main()
