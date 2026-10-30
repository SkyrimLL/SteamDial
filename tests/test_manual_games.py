import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

from cache_manager import GameCache
from library_service import LibraryService
from manual_game_service import ManualGameService
from manual_games import MANUAL_APPID_BASE, ManualGamesStore
from models import PlaytimeEstimate, SteamGame
from recommender import GameRecommender, TimeBudgetParser
from steamdial import create_app
from thumbnail_cache import ThumbnailCache
from thumbnail_service import ThumbnailService
from user_overrides import UserOverrides

JPEG = b"\xff\xd8\xffimage"


class ManualGamesStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "manual_games.json"
        self.store = ManualGamesStore(self.path)

    def test_added_game_gets_an_appid_outside_steams_range(self):
        game = self.store.add("Disco Elysium")
        self.assertGreaterEqual(game.appid, MANUAL_APPID_BASE)
        self.assertTrue(game.manual)
        self.assertFalse(game.installed)
        self.assertEqual(game.name, "Disco Elysium")

    def test_each_game_gets_a_distinct_appid_that_survives_a_restart(self):
        first = self.store.add("Hades")
        second = self.store.add("Bastion")
        self.assertNotEqual(first.appid, second.appid)
        reloaded = ManualGamesStore(self.path)
        self.assertEqual({(g.appid, g.name) for g in reloaded.games()},
                         {(first.appid, "Hades"), (second.appid, "Bastion")})
        self.assertNotIn(reloaded.add("Pyre").appid, {first.appid, second.appid})

    def test_games_are_sorted_by_name(self):
        for name in ("Zeno Clash", "Amnesia", "Mirror's Edge"):
            self.store.add(name)
        self.assertEqual([g.name for g in self.store.games()], ["Amnesia", "Mirror's Edge", "Zeno Clash"])

    def test_blank_and_oversized_titles_are_rejected(self):
        for title in ("", "   ", "\t\n", "x" * 201):
            with self.subTest(title=title):
                with self.assertRaises(ValueError):
                    self.store.add(title)
        self.assertEqual(self.store.games(), [])

    def test_duplicate_titles_are_rejected_regardless_of_case_and_spacing(self):
        self.store.add("Outer Wilds")
        with self.assertRaises(ValueError):
            self.store.add("  outer   wilds ")
        self.assertEqual(len(self.store.games()), 1)

    def test_surrounding_and_repeated_whitespace_is_normalised(self):
        self.assertEqual(self.store.add("  The   Witness  ").name, "The Witness")

    def test_malformed_entries_are_skipped_instead_of_failing_to_load(self):
        self.path.write_text(json.dumps({
            "version": 1,
            "games": {"9000000001": {"name": "Tunic"}, "not-an-id": {"name": "Broken"},
                      "9000000002": {"name": "  "}},
        }), encoding="utf-8")
        with self.assertLogs("manual_games", level="WARNING"):
            store = ManualGamesStore(self.path)
        self.assertEqual([g.name for g in store.games()], ["Tunic"])

    def test_rename_keeps_the_appid_so_artwork_and_overrides_follow(self):
        game = self.store.add("Tunik")
        renamed = self.store.rename(game.appid, "  Tunic  ")
        self.assertEqual((renamed.appid, renamed.name), (game.appid, "Tunic"))
        self.assertEqual([(g.appid, g.name) for g in ManualGamesStore(self.path).games()],
                         [(game.appid, "Tunic")])

    def test_rename_rejects_unusable_titles_and_other_games_titles(self):
        first = self.store.add("Tunic")
        second = self.store.add("Hades")
        for title in ("", "   ", "x" * 201, "hades"):
            with self.subTest(title=title):
                with self.assertRaises(ValueError):
                    self.store.rename(first.appid, title)
        self.assertEqual(self.store.rename(second.appid, "HADES").name, "HADES")
        self.assertEqual([g.name for g in self.store.games()], ["HADES", "Tunic"])

    def test_remove_deletes_only_the_requested_game(self):
        first = self.store.add("Tunic")
        second = self.store.add("Hades")
        self.assertEqual(self.store.remove(first.appid).name, "Tunic")
        self.assertEqual([g.appid for g in ManualGamesStore(self.path).games()], [second.appid])

    def test_renaming_or_removing_an_unknown_game_raises(self):
        with self.assertRaises(KeyError):
            self.store.rename(440, "Team Fortress 2")
        with self.assertRaises(KeyError):
            self.store.remove(440)

    def test_a_deleted_appid_is_never_handed_to_another_game(self):
        first = self.store.add("Tunic")
        self.store.remove(first.appid)
        self.assertNotEqual(self.store.add("Hades").appid, first.appid)
        # The counter has to survive a restart too, or the next run would recycle it.
        latest = self.store.games()[-1]
        self.store.remove(latest.appid)
        reopened = ManualGamesStore(self.path)
        self.assertNotIn(reopened.add("Celeste").appid, {first.appid, latest.appid})


class ManualGamesLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.scanner = Mock()
        self.scanner.scan.return_value = [SteamGame(appid=400, name="Portal", installed=True)]
        self.store = ManualGamesStore(root / "manual_games.json")
        self.library = LibraryService(self.scanner, GameCache(root / "cache.json"), manual_games=self.store)

    def test_rescan_keeps_manual_games_merged_in_name_order(self):
        self.store.add("Anachronox")
        self.store.add("Zork")
        self.assertEqual([g.name for g in self.library.rescan()], ["Anachronox", "Portal", "Zork"])

    def test_game_dicts_flag_manual_games_only(self):
        self.store.add("Anachronox")
        self.library.rescan()
        self.assertEqual({g["name"]: g["manual"] for g in self.library.game_dicts()},
                         {"Anachronox": True, "Portal": False})

    def test_a_manual_game_is_never_reported_as_a_newly_scanned_game(self):
        game = self.store.add("Anachronox")
        self.library.rescan()
        self.assertEqual(self.library.add_new_games([game]), [])
        self.assertEqual(len(self.library.games), 2)


class ManualGameServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.scanner = Mock()
        self.scanner.scan.return_value = [SteamGame(appid=400, name="Portal", installed=True)]
        self.scanner.local_header_image.return_value = None
        self.store = ManualGamesStore(root / "manual_games.json")
        self.cache = GameCache(root / "cache.json")
        self.overrides = UserOverrides(root / "user_overrides.json")
        self.thumbnails = ThumbnailService(self.scanner, ThumbnailCache(root / "thumbnails"), Mock())
        self.library = LibraryService(self.scanner, self.cache, user_overrides=self.overrides,
                                      manual_games=self.store)
        self.library.rescan()
        self.service = ManualGameService(self.store, self.library,
                                         (self.overrides, self.thumbnails, self.cache))

    def populate(self, game):
        self.thumbnails.set_override(game.appid, JPEG)
        self.overrides.set(game.appid, hidden=False, completed=True, completionist_hours=30.0)
        self.cache.put(game.appid, game.name, PlaytimeEstimate(main_hours=20.0))

    def test_added_game_joins_the_loaded_library_without_a_rescan(self):
        self.service.add("Anachronox")
        self.scanner.scan.assert_called_once()
        self.assertEqual([g.name for g in self.library.games], ["Anachronox", "Portal"])

    def test_rename_updates_the_loaded_library_and_keeps_name_order(self):
        game = self.service.add("Anachronox")
        renamed = self.service.rename(game.appid, "Zork")
        self.assertEqual(renamed.appid, game.appid)
        self.assertEqual([g.name for g in self.library.games], ["Portal", "Zork"])
        self.assertTrue(all(g.manual for g in self.library.games if g.name == "Zork"))

    def test_removal_clears_every_trace_of_the_game(self):
        game = self.service.add("Anachronox")
        self.populate(game)
        self.assertEqual(self.service.remove(game.appid).name, "Anachronox")
        self.assertEqual([g.name for g in self.library.games], ["Portal"])
        self.assertEqual(self.store.games(), [])
        self.assertIsNone(self.thumbnails.local_only(game.appid))
        self.assertFalse(self.overrides.get(game.appid).completed)
        self.assertIsNone(self.cache.get(game.appid))

    def test_removal_leaves_other_games_data_untouched(self):
        kept = self.service.add("Anachronox")
        doomed = self.service.add("Zork")
        self.populate(kept)
        self.populate(doomed)
        self.service.remove(doomed.appid)
        self.assertIsNotNone(self.thumbnails.local_only(kept.appid))
        self.assertTrue(self.overrides.get(kept.appid).completed)
        self.assertIsNotNone(self.cache.get(kept.appid))

    def test_a_store_that_cannot_clean_up_does_not_block_the_deletion(self):
        broken = Mock()
        broken.discard.side_effect = OSError("Permission denied")
        service = ManualGameService(self.store, self.library, (broken, self.cache))
        game = service.add("Anachronox")
        self.populate(game)
        with self.assertLogs("manual_game_service", level="ERROR"):
            service.remove(game.appid)
        self.assertEqual(self.store.games(), [])
        self.assertIsNone(self.cache.get(game.appid))

    def test_unknown_games_cannot_be_renamed_or_removed(self):
        for action in (lambda: self.service.rename(400, "Portal 2"), lambda: self.service.remove(400)):
            with self.subTest(action=action):
                with self.assertRaises(KeyError):
                    action()
        self.assertEqual([g.name for g in self.library.games], ["Portal"])


class ManualGameApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.scanner = Mock()
        self.scanner.scan.return_value = []
        self.scanner.local_header_image.return_value = None
        self.store = ManualGamesStore(root / "manual_games.json")
        self.cache = GameCache(root / "cache.json")
        self.overrides = UserOverrides(root / "user_overrides.json")
        self.thumbnails = ThumbnailService(self.scanner, ThumbnailCache(root / "thumbnails"), Mock())
        self.library = LibraryService(self.scanner, self.cache, user_overrides=self.overrides,
                                      manual_games=self.store)
        self.library.rescan()
        self.service = ManualGameService(self.store, self.library,
                                         (self.overrides, self.thumbnails, self.cache))
        self.enrichment = Mock()
        self.enrichment.status.return_value = {"running": False}
        self.watcher = Mock()
        self.watcher.status.return_value = {"count": 0}
        self.warmup = Mock()
        self.warmup.is_running.return_value = False
        self.client = create_app(
            self.library, self.scanner, self.enrichment, TimeBudgetParser(), GameRecommender(),
            self.thumbnails, Mock(), Mock(), self.overrides, self.service, self.watcher, self.warmup,
            allow_fetch=True,
        ).test_client()

    def add(self, name):
        return self.client.post("/api/manual-game", json={"name": name})

    def listed(self):
        return [g["name"] for g in self.client.get("/api/games").json["games"]]

    def test_added_game_shows_up_in_the_library(self):
        response = self.add("Disco Elysium")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json["appid"], MANUAL_APPID_BASE)
        self.assertEqual(self.listed(), ["Disco Elysium"])

    def test_added_game_is_looked_up_for_playtime_but_not_for_artwork(self):
        self.add("Disco Elysium")
        self.enrichment.start.assert_called_once()
        self.warmup.start.assert_not_called()

    def test_added_game_survives_a_rescan(self):
        self.add("Disco Elysium")
        self.assertEqual(self.client.post("/api/rescan").status_code, 200)
        self.assertEqual(self.listed(), ["Disco Elysium"])

    def test_blank_and_duplicate_titles_are_reported_without_adding_anything(self):
        self.add("Disco Elysium")
        for payload in ({"name": ""}, {}, {"name": "disco elysium"}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/manual-game", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
        self.assertEqual(self.listed(), ["Disco Elysium"])

    def test_manual_games_can_be_edited_through_the_existing_override_endpoint(self):
        appid = self.add("Disco Elysium").json["appid"]
        response = self.client.post(f"/api/game/{appid}/override",
                                    json={"completed": True, "actual_hours": 7.5})
        self.assertEqual(response.status_code, 200)

    def test_renaming_keeps_the_appid_and_refreshes_the_playtime_lookup(self):
        appid = self.add("Disco Elyseum").json["appid"]
        self.enrichment.start.reset_mock()
        response = self.client.patch(f"/api/manual-game/{appid}", json={"name": "Disco Elysium"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["appid"], appid)
        self.assertEqual(self.listed(), ["Disco Elysium"])
        self.enrichment.start.assert_called_once()

    def test_renaming_keeps_the_games_thumbnail_and_overrides(self):
        appid = self.add("Disco Elyseum").json["appid"]
        self.client.post(f"/api/thumbnail/{appid}/override", data={"image": (BytesIO(JPEG), "art.jpg")})
        self.client.post(f"/api/game/{appid}/override", json={"actual_hours": 7.5})
        self.client.patch(f"/api/manual-game/{appid}", json={"name": "Disco Elysium"})
        game = self.client.get("/api/games").json["games"][0]
        self.assertTrue(game["has_thumbnail"])
        self.assertEqual(game["actual_override_hours"], 7.5)

    def test_invalid_renames_are_reported_and_change_nothing(self):
        appid = self.add("Disco Elysium").json["appid"]
        self.add("Hades")
        for payload in ({"name": ""}, {}, {"name": "hades"}, {"name": "x" * 201}):
            with self.subTest(payload=payload):
                response = self.client.patch(f"/api/manual-game/{appid}", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
        self.assertEqual(self.listed(), ["Disco Elysium", "Hades"])

    def test_scanned_games_can_be_neither_renamed_nor_deleted(self):
        self.scanner.scan.return_value = [SteamGame(appid=400, name="Portal", installed=True)]
        self.library.rescan()
        self.assertEqual(self.client.patch("/api/manual-game/400", json={"name": "Portal 2"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/manual-game/400").status_code, 404)
        self.assertEqual(self.listed(), ["Portal"])

    def test_deleting_removes_the_game_and_the_data_stored_under_its_appid(self):
        appid = self.add("Disco Elysium").json["appid"]
        self.client.post(f"/api/thumbnail/{appid}/override", data={"image": (BytesIO(JPEG), "art.jpg")})
        self.client.post(f"/api/game/{appid}/override", json={"actual_hours": 7.5})
        response = self.client.delete(f"/api/manual-game/{appid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.listed(), [])
        self.assertIsNone(self.thumbnails.local_only(appid))
        self.assertIsNone(self.overrides.get(appid).actual_hours)

    def test_a_deleted_game_stays_gone_after_a_rescan_and_restart(self):
        appid = self.add("Disco Elysium").json["appid"]
        self.client.delete(f"/api/manual-game/{appid}")
        self.assertEqual(self.client.post("/api/rescan").status_code, 200)
        self.assertEqual(self.listed(), [])
        self.assertEqual(ManualGamesStore(self.store.path).games(), [])

    def test_a_deleted_games_appid_is_not_reused_by_the_next_addition(self):
        appid = self.add("Disco Elysium").json["appid"]
        self.client.delete(f"/api/manual-game/{appid}")
        self.assertNotEqual(self.add("Hades").json["appid"], appid)

    def test_deleting_the_same_game_twice_reports_not_found(self):
        appid = self.add("Disco Elysium").json["appid"]
        self.assertEqual(self.client.delete(f"/api/manual-game/{appid}").status_code, 200)
        repeat = self.client.delete(f"/api/manual-game/{appid}")
        self.assertEqual(repeat.status_code, 404)
        self.assertIn("error", repeat.json)


if __name__ == "__main__":
    unittest.main()
