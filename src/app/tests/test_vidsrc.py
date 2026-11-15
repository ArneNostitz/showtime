from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from app import vidsrc


@override_settings(VIDSRC_DOMAINS=["vidsrc.test"])
class VidsrcAvailabilityTests(TestCase):
    """Test the server-side VidSrc availability lookups."""

    def setUp(self):
        """Start every test with an empty cache."""
        cache.clear()

    @patch("app.vidsrc.requests.get")
    def test_tv_episode_ranges(self, mock_get):
        """Season ranges are parsed, cached and missing shows are empty."""
        found = Mock(status_code=200, ok=True)
        found.json.return_value = {
            "seasons": [{"season": 1, "first_episode": 1, "last_episode": 8}],
        }
        missing = Mock(status_code=404, ok=False)
        mock_get.side_effect = lambda url, **_: found if "/1399." in url else missing

        ranges = vidsrc.tv_episode_ranges(["1399", "999"])

        self.assertEqual(ranges, {"1399": {1: (1, 8)}, "999": {}})
        self.assertTrue(vidsrc.episode_available(ranges["1399"], 1, 8))
        self.assertFalse(vidsrc.episode_available(ranges["1399"], 1, 9))
        self.assertFalse(vidsrc.episode_available(ranges["1399"], 2, 1))

        vidsrc.tv_episode_ranges(["1399"])
        self.assertEqual(mock_get.call_count, 2)

    @patch("app.vidsrc.requests.get")
    def test_unknown_availability_is_left_out(self, mock_get):
        """Network errors don't mark a show as unavailable."""
        mock_get.side_effect = vidsrc.requests.ConnectionError

        self.assertEqual(vidsrc.tv_episode_ranges(["1399"]), {})
