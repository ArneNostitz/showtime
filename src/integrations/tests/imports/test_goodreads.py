from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Book,
    Status,
)
from integrations.imports import (
    goodreads,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


def _make_book(book_id, title):
    """Return a minimal hardcover search result entry."""
    return {
        "media_id": book_id,
        "media_type": "book",
        "source": "hardcover",
        "title": title,
        "image": "",
    }


_MOCK_BOOKS = {
    "Ghosts": _make_book("28825810", "Ghosts of the Tristan Basin"),
    "Wise Man": _make_book("1215032", "The Wise Man's Fear"),
    "White Sand": _make_book("39298848", "White Sand, Volume 3"),
}


def _mock_hardcover_search(query, _page):
    """Return a fake search result matched by keyword, empty otherwise."""
    for key, book in _MOCK_BOOKS.items():
        if key in query:
            return {"results": [book], "page": 1, "total_results": 1, "total_pages": 1}
    return {"results": [], "page": 1, "total_results": 0, "total_pages": 0}


class ImportGoodreads(TestCase):
    """Test importing media from GoodReads CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with (
            patch("app.providers.hardcover.search", side_effect=_mock_hardcover_search),
            Path(mock_path / "import_goodreads.csv").open("rb") as file,
        ):
            self.import_results = goodreads.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported books."""
        self.assertEqual(Book.objects.filter(user=self.user).count(), 3)

    def test_historical_records(self):
        """Test historical records creation during import."""
        book = Book.objects.filter(user=self.user).first()
        self.assertEqual(book.history.count(), 1)

    def test_stored_progress(self):
        """Test progress of imported books."""
        read_book = Book.objects.get(status=Status.COMPLETED.value)
        self.assertEqual(read_book.status, Status.COMPLETED.value)
        self.assertEqual(read_book.progress, 994)

        read_book = Book.objects.get(status=Status.IN_PROGRESS.value)
        self.assertEqual(read_book.status, Status.IN_PROGRESS.value)
        self.assertEqual(read_book.progress, 0)
