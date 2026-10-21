import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from recommender import GameRecommender, TimeBudgetParser
from steamdial import create_app
from thumbnail_cache import ThumbnailCache
from thumbnail_service import ThumbnailService


APPID = 1495320
DEFAULT_IMAGE = b"\xff\xd8\xffdefault"
OVERRIDE_IMAGE = b"\xff\xd8\xffoverride"


class ThumbnailOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.default = self.root / "default.jpg"
        self.default.write_bytes(DEFAULT_IMAGE)
        self.scanner = Mock()
        self.scanner.local_header_image.return_value = self.default
        self.cache = ThumbnailCache(self.root / "thumbnails")
        self.provider = Mock()
        self.thumbnails = ThumbnailService(self.scanner, self.cache, self.provider)
        self.library = Mock()
        self.library.game_dicts.side_effect = lambda: [{
            "appid": APPID,
            "name": "Carnal Instinct",
            "thumbnail": f"/thumb/{APPID}",
            "completionist_hours": 4,
        }]
        self.library.rescan.return_value = []
        self.enrichment = Mock()
        self.enrichment.status.return_value = {"running": False}
        self.watcher = Mock()
        self.watcher.status.return_value = {"count": 0}
        self.client = self.make_client()

    def make_client(self, allow_fetch=False):
        return create_app(
            self.library, self.scanner, self.enrichment, TimeBudgetParser(),
            GameRecommender(), self.thumbnails, Mock(), Mock(), Mock(),
            self.watcher, allow_fetch=allow_fetch,
        ).test_client()

    def upload(self, content=OVERRIDE_IMAGE):
        return self.client.post(
            f"/api/thumbnail/{APPID}/override",
            data={"image": (BytesIO(content), "replacement.jpg")},
        )

    def game(self, endpoint="/api/games"):
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 200)
        return response.json["games"][0]

    def test_existing_override_bypasses_previously_cached_unversioned_url(self):
        self.cache.store_override(APPID, OVERRIDE_IMAGE)
        game = self.game()
        self.assertNotEqual(game["thumbnail"], f"/thumb/{APPID}")
        self.assertTrue(game["has_thumbnail"])
        with self.client.get(game["thumbnail"]) as image:
            self.assertEqual(image.data, OVERRIDE_IMAGE)

    def test_upload_returns_same_versioned_url_as_library_and_recommendations(self):
        old_url = self.game()["thumbnail"]
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        url = response.json["thumbnail"]
        self.assertNotEqual(url, old_url)
        self.assertEqual(url, self.game()["thumbnail"])
        self.assertEqual(url, self.game("/api/recommend?max_hours=10")["thumbnail"])

    def test_replacing_override_changes_version_and_revalidates_old_etag(self):
        self.upload()
        before = self.game()["thumbnail"]
        with self.client.get(before) as image:
            etag = image.headers["ETag"]
            self.assertEqual(image.data, OVERRIDE_IMAGE)
        path = self.cache.get_override(APPID)
        assert path is not None
        timestamp = path.stat().st_mtime_ns
        self.upload(b"\xff\xd8\xffupdated!")
        os.utime(path, ns=(timestamp + 1_000_000_000, timestamp + 1_000_000_000))
        after = self.game()["thumbnail"]
        self.assertNotEqual(after, before)
        with self.client.get(after, headers={"If-None-Match": etag}) as image:
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.data, b"\xff\xd8\xffupdated!")

    def test_override_survives_rescan_and_service_restart(self):
        self.upload()
        url = self.game()["thumbnail"]
        response = self.client.post("/api/rescan")
        self.assertEqual(response.status_code, 200)
        self.thumbnails = ThumbnailService(
            self.scanner, ThumbnailCache(self.cache.directory), self.provider,
        )
        for allow_fetch in (False, True):
            with self.subTest(allow_fetch=allow_fetch):
                self.client = self.make_client(allow_fetch=allow_fetch)
                self.assertEqual(self.game()["thumbnail"], url)
                with self.client.get(url) as image:
                    self.assertEqual(image.data, OVERRIDE_IMAGE)
        self.provider.fetch.assert_not_called()

    def test_thumbnails_revalidate_instead_of_remaining_fresh_for_a_week(self):
        with self.client.get(f"/thumb/{APPID}") as image:
            self.assertTrue(image.cache_control.no_cache)
            self.assertEqual(image.cache_control.max_age, 0)
            etag = image.headers["ETag"]
        with self.client.get(f"/thumb/{APPID}", headers={"If-None-Match": etag}) as image:
            self.assertEqual(image.status_code, 304)
        self.upload()
        with self.client.get(f"/thumb/{APPID}", headers={"If-None-Match": etag}) as image:
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.data, OVERRIDE_IMAGE)

    def test_missing_thumbnail_can_be_replaced_without_network_access(self):
        self.scanner.local_header_image.return_value = None
        game = self.game()
        self.assertFalse(game["has_thumbnail"])
        with self.client.get(game["thumbnail"]) as image:
            self.assertEqual(image.status_code, 404)
        self.upload()
        self.assertTrue(self.game()["has_thumbnail"])
        self.assertIn("?v=", self.game()["thumbnail"])
        self.provider.fetch.assert_not_called()

    def test_invalid_upload_does_not_replace_saved_override(self):
        self.upload()
        url = self.game()["thumbnail"]
        response = self.upload(b"not a JPEG")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)
        self.assertEqual(self.game()["thumbnail"], url)
        path = self.cache.get_override(APPID)
        assert path is not None
        self.assertEqual(path.read_bytes(), OVERRIDE_IMAGE)

    def test_disk_write_error_is_reported_without_claiming_success(self):
        with patch.object(self.thumbnails, "set_override", side_effect=OSError("Disk full")):
            with self.assertLogs("steamdial", level="ERROR"):
                response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Could not save", response.json["error"])
        self.assertNotIn("ok", response.json)


if __name__ == "__main__":
    unittest.main()
