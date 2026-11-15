from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    BasicMedia,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from events.models import Event
from users.models import MediaSortChoices, MediaStatusChoices


class ListPlayNextTests(TestCase):
    """Test next-episode play links, progress and newest-unwatched sorting."""

    def setUp(self):
        """Create two tracked shows with released episodes."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.vsembed_enabled = True
        self.user.save()
        self.client.login(**self.credentials)
        self.now = timezone.now()

        self.old_tv = self._create_show("100", "Old Show", days_ago=[30, 20, 10])
        self.new_tv = self._create_show("200", "New Show", days_ago=[9, 5, 1])
        self.caught_up_tv = self._create_show("300", "Caught Up", days_ago=[40])

        self._watch(self.old_tv, 1, 1)
        self._watch(self.new_tv, 1, 1)
        self._watch(self.new_tv, 1, 2)
        self._watch(self.caught_up_tv, 1, 1)

    def _create_show(self, media_id, title, days_ago):
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
        Season.objects.bulk_create(
            [
                Season(
                    item=season_item,
                    user=self.user,
                    related_tv=tv,
                    status=Status.IN_PROGRESS.value,
                ),
            ],
        )
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
                    datetime=self.now + timedelta(days=7),
                ),
            ],
        )
        return tv

    def _watch(self, tv, season_number, episode_number):
        season = Season.objects.get(
            related_tv=tv,
            item__season_number=season_number,
        )
        item, _ = Item.objects.get_or_create(
            media_id=tv.item.media_id,
            source=tv.item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number=episode_number,
            defaults={"title": tv.item.title, "image": tv.item.image},
        )
        Episode.objects.bulk_create(
            [Episode(item=item, related_season=season, end_date=self.now)],
        )

    def _tv_list(self, sort):
        return list(
            BasicMedia.objects.get_media_list(
                self.user,
                MediaTypes.TV.value,
                MediaStatusChoices.ALL,
                sort,
            ),
        )

    def test_progress_ignores_rewatches(self):
        """Repeat watches of an episode don't inflate the progress."""
        self._watch(self.old_tv, 1, 1)
        self._watch(self.old_tv, 1, 1)
        self._watch(self.old_tv, 1, 2)

        tv = next(
            tv
            for tv in self._tv_list(MediaSortChoices.TITLE)
            if tv.id == self.old_tv.id
        )
        self.assertEqual(tv.progress, 2)

    def test_annotate_next_episode(self):
        """The next episode is the first released one after the furthest watched."""
        tv_list = self._tv_list(MediaSortChoices.TITLE)
        BasicMedia.objects.annotate_next_episode(tv_list)
        by_id = {tv.id: tv for tv in tv_list}

        self.assertEqual(by_id[self.old_tv.id].next_episode["episode"], 2)
        self.assertEqual(by_id[self.new_tv.id].next_episode["episode"], 3)
        self.assertIsNone(by_id[self.caught_up_tv.id].next_episode)
        self.assertIsNone(by_id[self.caught_up_tv.id].newest_unwatched_at)

    @patch("app.vidsrc.tv_episode_ranges")
    def test_newest_unwatched_sort(self, mock_ranges):
        """Shows with the newest playable unwatched episode come first."""
        mock_ranges.return_value = {}

        tv_list = self._tv_list(MediaSortChoices.NEWEST_UNWATCHED)

        self.assertEqual(
            [tv.id for tv in tv_list],
            [self.new_tv.id, self.old_tv.id, self.caught_up_tv.id],
        )

    @patch("app.vidsrc.tv_episode_ranges")
    def test_newest_unwatched_sort_requires_vidsrc_link(self, mock_ranges):
        """A new episode VidSrc doesn't have yet doesn't move the show up."""
        mock_ranges.return_value = {"200": {1: (1, 2)}, "100": {1: (1, 3)}}

        tv_list = self._tv_list(MediaSortChoices.NEWEST_UNWATCHED)

        self.assertEqual(tv_list[0].id, self.old_tv.id)
        self.assertIsNone(tv_list[1].newest_unwatched_at)

    def test_tv_list_play_button(self):
        """The TV list links to the next episode on VidSrc."""
        response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))

        self.assertContains(response, "/embed/tv/200/1/3")
        self.assertContains(response, "/embed/tv/100/1/2")
        self.assertNotContains(response, "/embed/tv/300/")
        self.assertContains(response, '"skip_if_watched": "1"')

    def test_tv_list_play_button_table_layout(self):
        """The table layout also shows the next-episode play link."""
        self.user.tv_layout = "table"
        self.user.save()

        response = self.client.get(reverse("medialist", args=[MediaTypes.TV.value]))

        self.assertContains(response, "/embed/tv/200/1/3")

    def test_newest_unwatched_sort_only_offered_for_tv(self):
        """The newest-unwatched sort is only offered on the TV list."""
        tv_response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value]),
        )
        movie_response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]),
        )

        self.assertIn(
            MediaSortChoices.NEWEST_UNWATCHED,
            [value for value, _ in tv_response.context["sort_choices"]],
        )
        self.assertNotIn(
            MediaSortChoices.NEWEST_UNWATCHED,
            [value for value, _ in movie_response.context["sort_choices"]],
        )

    def test_movie_list_play_button(self):
        """The movie list links to the movie on VidSrc."""
        item = Item.objects.create(
            media_id="278",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.bulk_create(
            [Movie(item=item, user=self.user, status=Status.PLANNING.value)],
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]),
        )

        self.assertContains(response, "/embed/movie/278")

    def test_list_item_refresh(self):
        """A single list entry can be re-rendered after playing it."""
        self._watch(self.new_tv, 1, 3)

        for layout in ("grid", "table"):
            self.user.tv_layout = layout
            self.user.save()
            response = self.client.get(
                reverse(
                    "medialist_item",
                    args=[MediaTypes.TV.value, self.new_tv.id],
                ),
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "/embed/tv/200/1/3")
            self.assertContains(response, "3")

    @patch("app.models.Season.watch")
    def test_episode_save_skip_if_watched(self, mock_watch):
        """Play links don't log a rewatch for an already watched episode."""
        data = {
            "media_id": "200",
            "season_number": 1,
            "episode_number": 2,
            "source": Sources.TMDB.value,
            "end_date": "2026-01-01",
        }

        self.client.post(reverse("episode_save"), {**data, "skip_if_watched": "1"})
        mock_watch.assert_not_called()

        self.client.post(
            reverse("episode_save"),
            {**data, "episode_number": 3, "skip_if_watched": "1"},
        )
        mock_watch.assert_called_once()
