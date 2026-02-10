import pytest

from beets.dbcore import types
from beets.library.migrations import MultiGenreFieldMigration
from beets.library.models import Album, Item
from beets.test.helper import TestHelper


class TestMultiGenreFieldMigration:
    @pytest.fixture
    def helper(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("beets.library.library.Library._migrations", ())
        monkeypatch.setattr(
            "beets.library.models.Item._fields",
            {**Item._fields, "genre": types.STRING},
        )
        monkeypatch.setattr(
            "beets.library.models.Album._fields",
            {**Album._fields, "genre": types.STRING},
        )
        monkeypatch.setattr(
            "beets.library.models.Album.item_keys",
            {*Album.item_keys, "genre"},
        )
        helper = TestHelper()
        helper.setup_beets()

        monkeypatch.setattr(
            "beets.library.library.Library._migrations",
            ((MultiGenreFieldMigration, (Item, Album)),),
        )
        yield helper

        helper.teardown_beets()

    def test_migrates_only_rows_with_missing_genres(self, helper: TestHelper):
        helper.config["lastgenre"]["separator"] = " - "

        expected_item_genres = []
        for genre, initial_genres, expected_genres in [
            # already existing value is not overwritten
            ("Item Rock", ("Ignored",), ("Ignored",)),
            ("", (), ()),
            ("Rock", (), ("Rock",)),
            # multiple genres are split on one of default separators
            ("Item Rock; Alternative", (), ("Item Rock", "Alternative")),
            # multiple genres are split the first (lastgenre) separator ONLY
            ("Item - Rock, Alternative", (), ("Item", "Rock, Alternative")),
        ]:
            helper.add_item(genre=genre, genres=initial_genres)
            expected_item_genres.append(expected_genres)

        unmigrated_album = helper.add_album(
            genre="Album Rock / Alternative", genres=[]
        )
        expected_item_genres.append(("Album Rock", "Alternative"))

        helper.lib._migrate()

        actual_item_genres = [tuple(i.genres) for i in helper.lib.items()]
        assert actual_item_genres == expected_item_genres

        unmigrated_album.load()
        assert unmigrated_album.genres == ["Album Rock", "Alternative"]

        assert helper.lib.get_migration_state("multi_genre_field_items")
        assert helper.lib.get_migration_state("multi_genre_field_albums")
