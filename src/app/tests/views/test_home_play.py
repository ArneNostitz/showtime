from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from events.models import Event
from users.models import HomeSortChoices


class HomePlayNextTests(TestCase):
    """Test the home page's play-next buttons, counts and oldest-unwatched sort."""

    def setUp(self):
        """Create seasons with released and upcoming episodes."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.vsembed_enabled = True
        self.user.save()
        self.client.login(**self.credentials)
        self.now = timezone.now()

        # Next unwatched: E2 aired 20 days ago.
        self.old = self._create_season("100", "Old Show", days_ago=[30, 20, 10])
        # Next unwatched: E3 aired 1 day ago, but E4 airs soonest.
        self.new = self._create_season("200", "New Show", days_ago=[9, 5, 1])
        # Nothing released left to watch.
        self.caught_up = self._create_season("300", "Caught Up", days_ago=[40])

        self._watch(self.old, 1)
        self._watch(self.new, 1)
        self._watch(self.new, 2)
        self._watch(self.caught_up, 1)

    def _create_season(self, media_id, title, days_ago, upcoming_in=7):
        tv_item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="http://example.com/image.jpg",
        )
        season_item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title=title,
            image="http://example.com/image.jpg",
            season_number=1,
        )
        tv = TV.objects.bulk_create(
            [TV(item=tv_item, user=self.user, status=Status.IN_PROGRESS.value)],
        )[0]
        season = Season.objects.bulk_create(
            [
                Season(
                    item=season_item,
                    user=self.user,
                    related_tv=tv,
                    status=Status.IN_PROGRESS.value,
                ),
            ],
        )[0]
        Event.objects.bulk_create(
            [
                Event(
                    item=season_item,
                    content_number=number,
                    datetime=self.now - timedelta(days=days),
                )
                for number, days in enumerate(days_ago, start=1)
            ]
            + [
                Event(
                    item=season_item,
                    content_number=len(days_ago) + 1,
                    datetime=self.now + timedelta(days=upcoming_in),
                ),
            ],
        )
        return season

    def _watch(self, season, episode_number):
        item, _ = Item.objects.get_or_create(
            media_id=season.item.media_id,
            source=season.item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season.item.season_number,
            episode_number=episode_number,
            defaults={"title": season.item.title, "image": season.item.image},
        )
        Episode.objects.bulk_create(
            [Episode(item=item, related_season=season, end_date=self.now)],
        )

    def _home_seasons(self, sort=HomeSortChoices.OLDEST_UNWATCHED):
        response = self.client.get(reverse("home"), {"sort": sort})
        section = response.context["home_sections"][0]
        return response, section["media_types"][MediaTypes.SEASON.value]["items"]

    def test_oldest_unwatched_is_default_sort(self):
        """New users get the oldest-unwatched home sort."""
        user = get_user_model().objects.create_user(username="other", password="x")  # noqa: S106
        self.assertEqual(user.home_sort, HomeSortChoices.OLDEST_UNWATCHED)

    def test_oldest_unwatched_sort(self):
        """Seasons whose next unwatched episode aired longest ago come first."""
        _, items = self._home_seasons()

        self.assertEqual(
            [media.id for media in items],
            [self.old.id, self.new.id, self.caught_up.id],
        )

    def test_play_next_button(self):
        """Each season links to its next unwatched episode, marking it watched."""
        response, _ = self._home_seasons()

        self.assertContains(response, "/embed/tv/100/1/2")
        self.assertContains(response, "/embed/tv/200/1/3")
        self.assertNotContains(response, "/embed/tv/300/")
        self.assertContains(response, '"skip_if_watched": "1"')
        self.assertContains(response, "vidsrcPlayer.js")

    def test_season_cards_drop_modal_buttons(self):
        """Season cards no longer show the tracker, lists and history buttons."""
        response, _ = self._home_seasons()

        self.assertNotContains(response, 'title="Add to custom lists"')
        self.assertNotContains(response, 'title="View your activity history"')

    def test_no_play_button_without_vidsrc(self):
        """Without VidSrc enabled there is no play link."""
        self.user.vsembed_enabled = False
        self.user.save()

        response, _ = self._home_seasons()

        self.assertNotContains(response, "/embed/tv/")

    def test_count_ignores_rewatches(self):
        """The episode count is distinct episodes watched, not the most repeated."""
        self._watch(self.old, 1)
        self._watch(self.old, 2)
        self._watch(self.old, 3)

        _, items = self._home_seasons()
        old = next(media for media in items if media.id == self.old.id)

        self.assertEqual(old.formatted_progress, "3")
        self.assertIsNone(old.next_episode)

    def test_undated_episode_is_not_next(self):
        """Episodes without an air date aren't offered to play."""
        Event.objects.filter(item=self.old.item, content_number=2).update(
            datetime=datetime.min.replace(tzinfo=UTC),
        )

        _, items = self._home_seasons()
        old = next(media for media in items if media.id == self.old.id)

        self.assertEqual(old.next_episode["episode"], 3)

    def test_home_item_refresh(self):
        """A single home card can be re-rendered after playing its episode."""
        self._watch(self.old, 2)

        response = self.client.get(
            reverse("home_item", args=[MediaTypes.SEASON.value, self.old.id]),
            {"home_status": Status.IN_PROGRESS.value},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-home-item")
        self.assertContains(response, "/embed/tv/100/1/3")
        self.assertContains(response, "2\n")

    def test_play_marks_episode_and_returns_card(self):
        """Playing posts the episode and swaps in the refreshed card."""
        next_url = (
            reverse("home_item", args=[MediaTypes.SEASON.value, self.old.id])
            + "?home_status=In+progress"
        )
        with patch("app.models.Season.watch") as mock_watch:
            response = self.client.post(
                reverse("episode_save") + "?next=" + next_url,
                {
                    "media_id": "100",
                    "season_number": 1,
                    "episode_number": 2,
                    "source": Sources.TMDB.value,
                    "end_date": "2026-01-01",
                    "skip_if_watched": "1",
                },
                follow=True,
            )

        mock_watch.assert_called_once()
        self.assertContains(response, "data-home-item")


class WatchlistQuickAddTests(TestCase):
    """Test adding a show to the watchlist from search or Discover."""

    def setUp(self):
        """Create and log in a user."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.models.Item.fetch_releases")
    @patch("app.views.services.get_media_metadata")
    def test_tv_watchlist_plans_first_season(self, mock_metadata, _mock_releases):
        """A watchlisted show's first season is planned so it shows on home."""
        mock_metadata.return_value = {
            "title": "Show",
            "image": "http://example.com/tv.jpg",
            "related": {
                "seasons": [
                    {"season_number": 0, "image": "http://example.com/s0.jpg"},
                    {"season_number": 1, "image": "http://example.com/s1.jpg"},
                    {"season_number": 2, "image": "http://example.com/s2.jpg"},
                ],
            },
        }

        response = self.client.post(
            reverse("media_save_planning"),
            {
                "media_id": "500",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "inline": "1",
            },
            headers={"HX-Request": "true"},
        )

        self.assertContains(response, "On your watchlist")
        tv = TV.objects.get(user=self.user, item__media_id="500")
        self.assertEqual(tv.status, Status.PLANNING.value)
        season = Season.objects.get(user=self.user, item__media_id="500")
        self.assertEqual(season.item.season_number, 1)
        self.assertEqual(season.status, Status.PLANNING.value)
        self.assertEqual(season.related_tv, tv)

    @patch("app.views.services.search")
    def test_search_shows_watchlist_button(self, mock_search):
        """Untracked search results offer a one-click watchlist add."""
        mock_search.side_effect = lambda *_args: {
            "page": 1,
            "total_pages": 1,
            "total_results": 1,
            "results": [
                {
                    "media_id": "500",
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "title": "Show",
                    "image": "http://example.com/tv.jpg",
                },
            ],
        }

        for layout in ("grid", "list"):
            response = self.client.get(
                reverse("search"),
                {"media_type": MediaTypes.TV.value, "q": "show", "layout": layout},
            )
            self.assertContains(response, reverse("media_save_planning"))
