from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app import views
from app.models import (
    MediaTypes,
    Sources,
)


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")

        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }

        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 1,
                "name": "Episode 1",
                "air_date": "2023-01-01",
                "watched": False,
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)

        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_vidsrc_play_button(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        """Episode thumbnails get a VidSrc play button for the fullscreen player."""
        self.user.vsembed_enabled = True
        self.user.save()
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "imdb_id": "tt0108778",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "season_number": 1,
                "episodes": [],
            },
        }
        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 2,
                "title": "Episode 2",
                "image": "http://example.com/episode.jpg",
                "air_date": "2023-01-01",
                "history": [],
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertContains(
            response,
            "https://vidsrc.sh/embed/tv?imdb=tt0108778&amp;autoplay=1",
        )
        self.assertContains(response, "subtitles.vtt&season=1&episode=2")
        self.assertContains(response, "data-vidsrc-play")
        self.assertNotContains(response, "window.open(")
        self.assertContains(response, 'id="vidsrc-domains"')
        self.assertContains(response, "js/vidsrcPlayer.js")


class BuildVsembedUrlTests(TestCase):
    """Test _build_vsembed_url playback options."""

    def test_movie_url_contains_playback_options(self):
        """Movie URLs autoplay on the primary VidSrc domain with no subtitles."""
        url = views._build_vsembed_url("tt0111161", MediaTypes.MOVIE.value)

        self.assertEqual(
            url,
            "https://vidsrc.sh/embed/movie?imdb=tt0111161"
            "&autoplay=1"
            "&sub_url=https://example.invalid/subtitles.vtt",
        )

    def test_tv_url_contains_playback_options(self):
        """TV URLs autoplay with no subtitles."""
        url = views._build_vsembed_url("tt5875444", MediaTypes.TV.value)

        self.assertTrue(
            url.startswith("https://vidsrc.sh/embed/tv?imdb=tt5875444&"),
        )
        self.assertIn("autoplay=1", url)
        self.assertIn("sub_url=https://example.invalid/subtitles.vtt", url)

    @override_settings(VIDSRC_DOMAINS=["player.example.com", "vidsrc.sh"])
    def test_uses_first_configured_domain(self):
        """The first configured VidSrc domain is used for links."""
        url = views._build_vsembed_url("tt0111161", MediaTypes.MOVIE.value)

        self.assertTrue(
            url.startswith("https://player.example.com/embed/movie?imdb=tt0111161&"),
        )

    def test_no_imdb_id_returns_none(self):
        """No IMDb ID means no vsembed URL."""
        self.assertIsNone(views._build_vsembed_url(None, MediaTypes.TV.value))
        self.assertIsNone(
            views._build_vsembed_url("", MediaTypes.MOVIE.value),
        )
