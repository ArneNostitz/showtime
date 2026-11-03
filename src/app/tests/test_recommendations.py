import datetime
import random
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import recommendations as recs
from app.models import TV, Item, MediaTypes, Movie, Sources, Status


def rec(media_id, genre_ids=(18,), score=7.5, count=1000, media_type="movie"):
    """Build a formatted TMDB result."""
    return {
        "media_id": str(media_id),
        "source": Sources.TMDB.value,
        "media_type": media_type,
        "title": f"Title {media_id}",
        "image": "http://example.com/img.jpg",
        "genre_ids": list(genre_ids),
        "score": score,
        "score_count": count,
        "popularity": 10,
        "year": "2020",
        "synopsis": "Synopsis",
    }


SEEDS = {
    ("movie", "1"): {
        "genre_ids": [878, 28],
        "recommendations": [rec(100, (878,)), rec(101, (35,)), rec(2)],
    },
    ("movie", "2"): {
        "genre_ids": [35],
        "recommendations": [rec(101, (35,)), rec(102, (35,))],
    },
    ("movie", "3"): {
        "genre_ids": [27],
        "recommendations": [rec(103, (27,))],
    },
}


def fake_seed(media_type, media_id):
    """Return canned seed data."""
    return SEEDS.get((media_type, media_id), {"genre_ids": [], "recommendations": []})


class RecommendationsTestBase(TestCase):
    """Create a user with some tracked movies."""

    def setUp(self):
        """Create user and history."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def track(self, media_id, status, score=None, model=Movie, media_type="movie"):
        """Track a TMDB title."""
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=media_type,
            title=f"Seed {media_id}",
            image="http://example.com/img.jpg",
        )
        # bulk_create skips save(), which would fetch metadata from TMDB
        return model.objects.bulk_create(
            [model(item=item, user=self.user, status=status, score=score)],
        )[0]


@patch("app.providers.tmdb.taste_seed", side_effect=fake_seed)
class ProfileTests(RecommendationsTestBase):
    """Tests for the taste profile."""

    def test_weights(self, _mock):
        """Higher scores weigh more, dropped titles count against."""
        loved = self.track("1", Status.COMPLETED.value, 10)
        meh = self.track("2", Status.COMPLETED.value, 6)
        dropped = self.track("3", Status.DROPPED.value)
        self.assertGreater(recs.media_weight(loved), recs.media_weight(meh))
        self.assertGreater(recs.media_weight(meh), 0)
        self.assertLess(recs.media_weight(dropped), 0)

    def test_old_activity_weighs_less(self, _mock):
        """Recent activity counts more than old activity."""
        recent = self.track("1", Status.COMPLETED.value, 8)
        old = self.track("2", Status.COMPLETED.value, 8)
        old.end_date = timezone.now() - datetime.timedelta(days=1000)
        self.assertGreater(recs.media_weight(recent), recs.media_weight(old))

    def test_genre_affinity(self, _mock):
        """Liked genres are positive, genres of dropped titles negative."""
        self.track("1", Status.COMPLETED.value, 9)
        self.track("3", Status.DROPPED.value)
        profile = recs.build_profile(self.user)
        self.assertEqual(profile.genre_affinity["scifi"], 1.0)
        self.assertLess(profile.genre_affinity["horror"], 0)
        self.assertTrue(profile.is_tracked("movie", "1"))

    def test_genre_choices_flag_new_genres(self, _mock):
        """Genres the user rarely watches are flagged."""
        self.track("1", Status.COMPLETED.value, 9)
        choices = {
            g["key"]: g for g in recs.genre_choices(recs.build_profile(self.user))
        }
        self.assertFalse(choices["scifi"]["new_to_you"])
        self.assertTrue(choices["western"]["new_to_you"])
        self.assertFalse(choices["horror"]["tv"])


@patch("app.providers.tmdb.taste_seed", side_effect=fake_seed)
class ForYouTests(RecommendationsTestBase):
    """Tests for personal recommendations."""

    def test_aggregates_and_excludes_tracked(self, _mock):
        """Titles recommended by several seeds rank first, tracked are skipped."""
        self.track("1", Status.COMPLETED.value, 9)
        self.track("2", Status.COMPLETED.value, 9)
        results = recs.for_you(recs.build_profile(self.user), MediaTypes.MOVIE.value)
        ids = [item["media_id"] for item in results]
        self.assertEqual(ids[0], "101")
        self.assertNotIn("2", ids)
        self.assertIn("Seed 1", results[0]["reason"])
        self.assertIn("Seed 2", results[0]["reason"])

    def test_dropped_titles_are_not_seeds(self, _mock):
        """Recommendations of dropped titles are not used."""
        self.track("3", Status.DROPPED.value)
        self.track("1", Status.COMPLETED.value, 8)
        ids = [
            item["media_id"]
            for item in recs.for_you(recs.build_profile(self.user), "movie")
        ]
        self.assertNotIn("103", ids)

    @patch("app.providers.tmdb.trending", return_value=[rec(500), rec(501)])
    def test_trending_fallback(self, _trending, _mock):
        """Without history, trending titles are shown."""
        results = recs.for_you(recs.build_profile(self.user), "movie")
        self.assertEqual(results[0]["reason"], "Trending this week")


@patch("app.providers.tmdb.taste_seed", side_effect=fake_seed)
class SurpriseTests(RecommendationsTestBase):
    """Tests for the surprise picker."""

    @patch("app.providers.tmdb.discover")
    def test_pick_untracked_in_genre(self, discover, _mock):
        """Picks an untracked title using the genre's TMDB id."""
        self.track("1", Status.COMPLETED.value, 9)
        discover.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [rec(1), rec(200), rec(201)],
        }
        result = recs.surprise(
            recs.build_profile(self.user),
            "horror",
            "movie",
            exclude=["201"],
            rng=random.Random(1),  # noqa: S311
        )
        self.assertEqual(discover.call_args[0][1]["with_genres"], 27)
        self.assertEqual(result["pick"]["media_id"], "200")
        self.assertEqual(result["alternatives"], [])

    @patch("app.providers.tmdb.discover")
    def test_hidden_gems_filters(self, discover, _mock):
        """Hidden gems cap the vote count."""
        discover.return_value = {"page": 1, "total_pages": 1, "results": []}
        result = recs.surprise(
            recs.build_profile(self.user), "comedy", "tv", hidden_gems=True
        )
        self.assertIn("vote_count.lte", discover.call_args[0][1])
        self.assertIsNone(result["pick"])

    @patch("app.providers.tmdb.discover")
    def test_random_genre_explores(self, discover, _mock):
        """A random genre avoids the user's favourite genres."""
        self.track("1", Status.COMPLETED.value, 9)
        discover.return_value = {"page": 1, "total_pages": 1, "results": []}
        for seed in range(10):
            result = recs.surprise(
                recs.build_profile(self.user),
                "random",
                "movie",
                rng=random.Random(seed),  # noqa: S311
            )
            self.assertNotIn(result["genre"], ("scifi", "action"))

    def test_invalid_genre(self, _mock):
        """Genres without a TV equivalent are rejected for TV."""
        with self.assertRaises(ValueError):
            recs.surprise(recs.build_profile(self.user), "horror", "tv")


class MoodPlanTests(TestCase):
    """Tests for translating mood answers into filters."""

    def test_short_movie(self):
        """A quick movie limits the runtime."""
        plan = recs.mood_plan({"feeling": "cheerful", "time": "short"})
        self.assertEqual(plan["media_type"], "movie")
        self.assertEqual(plan["filters"]["with_runtime.lte"], 100)
        self.assertIn("35", plan["filters"]["with_genres"])

    def test_family_avoids_horror(self):
        """Family viewing requires family genres and avoids horror."""
        plan = recs.mood_plan({"feeling": "edgy", "company": "family"})
        self.assertEqual(plan["filters"]["with_genres"], "10751|16")
        self.assertIn("27", plan["filters"]["without_genres"].split(","))

    def test_binge_is_tv(self):
        """Binge time recommends TV with TV genre ids only."""
        plan = recs.mood_plan({"feeling": "edgy", "time": "binge"})
        self.assertEqual(plan["media_type"], "tv")
        self.assertNotIn("27", plan["filters"].get("with_genres", ""))

    def test_binge_fresh_keeps_binge_threshold(self):
        """Fresh picks don't lower the binge vote-count minimum."""
        plan = recs.mood_plan({"time": "binge", "novelty": "fresh"})
        self.assertEqual(plan["filters"]["vote_count.gte"], 500)

    def test_fresh_lowers_vote_minimum(self):
        """Fresh picks outside binge mode include lesser-known titles."""
        plan = recs.mood_plan({"time": "evening", "novelty": "fresh"})
        self.assertEqual(plan["filters"]["vote_count.gte"], 80)

    def test_classic(self):
        """Classics are limited to releases before 2000."""
        plan = recs.mood_plan({"novelty": "classic"})
        self.assertEqual(
            plan["filters"]["primary_release_date.lte"],
            "1999-12-31",
        )


@patch("app.providers.tmdb.taste_seed", side_effect=fake_seed)
class DiscoverViewTests(RecommendationsTestBase):
    """Tests for the discover views."""

    def test_page(self, _mock):
        """The discover page renders the quiz and surprise picker."""
        response = self.client.get(reverse("discover"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "What mood are you in?")
        self.assertContains(response, "Surprise me")
        self.assertContains(response, "media_type=tv")

    def test_page_hides_disabled_types(self, _mock):
        """Disabled media types get no recommendation row."""
        self.user.tv_enabled = False
        self.user.save()
        response = self.client.get(reverse("discover"))
        self.assertNotContains(response, "media_type=tv")

    def test_for_you(self, _mock):
        """The for-you partial lists recommendations."""
        self.track("1", Status.COMPLETED.value, 9)
        response = self.client.get(
            reverse("discover_for_you"), {"media_type": "movie"}
        )
        self.assertContains(response, "Title 100")
        self.assertContains(response, "Because you liked Seed 1")

    def test_for_you_tv(self, _mock):
        """TV history seeds TV recommendations."""
        SEEDS[("tv", "9")] = {
            "genre_ids": [18],
            "recommendations": [rec(900, media_type="tv")],
        }
        self.track("9", Status.IN_PROGRESS.value, model=TV, media_type="tv")
        response = self.client.get(reverse("discover_for_you"), {"media_type": "tv"})
        self.assertContains(response, "Title 900")

    def test_for_you_bad_type(self, _mock):
        """Only movies and TV are supported."""
        response = self.client.get(
            reverse("discover_for_you"), {"media_type": "anime"}
        )
        self.assertEqual(response.status_code, 400)

    @patch("app.providers.tmdb.discover")
    def test_surprise_remembers_picks(self, discover, _mock):
        """Recent picks are not suggested again."""
        discover.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [rec(300), rec(301)],
        }
        first = self.client.get(
            reverse("discover_surprise"), {"genre": "drama", "media_type": "movie"}
        )
        self.assertEqual(first.status_code, 200)
        picked = self.client.session["discover_recent_picks"][0]
        self.client.get(
            reverse("discover_surprise"), {"genre": "drama", "media_type": "movie"}
        )
        second = self.client.session["discover_recent_picks"][0]
        self.assertNotEqual(picked, second)

    @patch("app.providers.tmdb.discover")
    def test_surprise_recent_picks_per_type(self, discover, _mock):
        """A movie pick doesn't hide a TV show with the same TMDB id."""
        discover.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [rec(300, media_type="tv")],
        }
        session = self.client.session
        session["discover_recent_picks"] = ["movie:300", "300"]
        session.save()
        response = self.client.get(
            reverse("discover_surprise"), {"genre": "drama", "media_type": "tv"}
        )
        self.assertContains(response, "Title 300")
        self.assertEqual(
            self.client.session["discover_recent_picks"], ["tv:300", "movie:300"]
        )

    def test_surprise_invalid_genre(self, _mock):
        """Unknown genres return a bad request."""
        response = self.client.get(
            reverse("discover_surprise"), {"genre": "nope", "media_type": "movie"}
        )
        self.assertEqual(response.status_code, 400)

    @patch("app.providers.tmdb.discover")
    def test_mood(self, discover, _mock):
        """Mood answers return a pick, ignoring invalid answers."""
        discover.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [rec(400, (35,)), rec(401, (18,))],
        }
        response = self.client.get(
            reverse("discover_mood"),
            {"feeling": "cheerful", "time": "short", "company": "bogus"},
        )
        self.assertContains(response, "Your mood pick")
        self.assertContains(response, "Title 400")

    @patch("app.providers.tmdb.discover")
    def test_mood_shuffle_skips_previous_pick(self, discover, _mock):
        """Shuffling a mood result gives a different top pick."""
        discover.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [rec(400, (35,), score=9), rec(401, (18,), score=5)],
        }
        answers = {"feeling": "cheerful", "time": "short"}
        self.client.get(reverse("discover_mood"), answers)
        self.client.get(reverse("discover_mood"), answers)
        self.assertEqual(
            self.client.session["discover_mood_picks"][:2], ["movie:401", "movie:400"]
        )

    @patch("app.providers.services.get_media_metadata")
    def test_inline_watchlist_twice(self, metadata, _mock):
        """Adding an already tracked title again doesn't duplicate or crash."""
        metadata.return_value = {"title": "Title 500", "image": "http://x/img.jpg"}
        for media_type, model in (("movie", Movie), ("tv", TV)):
            for _ in range(2):
                response = self.client.post(
                    reverse("media_save_planning"),
                    {
                        "media_id": "500",
                        "source": Sources.TMDB.value,
                        "media_type": media_type,
                        "inline": "1",
                    },
                    HTTP_HX_REQUEST="true",
                )
                self.assertContains(response, "On your watchlist")
            self.assertEqual(
                model.objects.filter(user=self.user, item__media_id="500").count(), 1
            )

    @patch("app.providers.services.get_media_metadata")
    def test_inline_watchlist(self, metadata, _mock):
        """Inline watchlist adds return a badge instead of redirecting."""
        metadata.return_value = {"title": "Title 400", "image": "http://x/img.jpg"}
        response = self.client.post(
            reverse("media_save_planning"),
            {
                "media_id": "400",
                "source": Sources.TMDB.value,
                "media_type": "movie",
                "inline": "1",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "On your watchlist")
        self.assertNotIn("HX-Redirect", response)
        self.assertTrue(
            Movie.objects.filter(
                user=self.user, item__media_id="400", status=Status.PLANNING.value
            ).exists()
        )


KIDS = (16, 10751, 35)
SEEDS[("movie", "10")] = {
    "genre_ids": [878, 18],
    "keywords": [1, 2, 3],
    "creators": {"77": "Christopher Nolan"},
    "cast": {"88": "Actor"},
    "language": "en",
    "recommendations": [rec(600, KIDS), rec(601, (878, 18)), rec(602, KIDS)],
    "similar": [rec(603, (18,))],
}
SEEDS[("movie", "601")] = {
    "genre_ids": [878, 18],
    "keywords": [1, 2],
    "creators": {"77": "Christopher Nolan"},
}


@patch("app.providers.tmdb.taste_seed", side_effect=fake_seed)
class AlgorithmTests(RecommendationsTestBase):
    """Tests for the hybrid ranking, kids gating and diversity."""

    def test_cosine(self, _mock):
        """Identical vectors are 1, disjoint vectors are 0."""
        self.assertAlmostEqual(recs.cosine({"a": 1, "b": 2}, {"a": 1, "b": 2}), 1)
        self.assertEqual(recs.cosine({"a": 1}, {"b": 1}), 0)

    def test_taste_vector_has_content_features(self, _mock):
        """Keywords and creators of liked titles end up in the taste vector."""
        self.track("10", Status.COMPLETED.value, 10)
        profile = recs.build_profile(self.user)
        self.assertGreater(profile.features["k:1"], 0)
        self.assertGreater(profile.features["p:77"], 0)
        self.assertFalse(profile.kids_ok)

    def test_kids_titles_ranked_below_taste_matches(self, _mock):
        """Animated family films don't win for users who don't watch them."""
        self.track("10", Status.COMPLETED.value, 10)
        results = recs.for_you(recs.build_profile(self.user), "movie")
        self.assertEqual(results[0]["media_id"], "601")
        self.assertEqual(results[0]["reason"], "From Christopher Nolan (Seed 10)")
        kids = [r["media_id"] for r in results if r["media_id"] in ("600", "602")]
        positions = [r["media_id"] for r in results]
        for media_id in kids:
            self.assertGreater(positions.index(media_id), positions.index("603"))

    def test_kids_ok_when_user_watches_them(self, _mock):
        """Family viewers keep their family recommendations."""
        SEEDS[("movie", "11")] = {"genre_ids": [16, 10751], "recommendations": []}
        self.track("11", Status.COMPLETED.value, 10)
        profile = recs.build_profile(self.user)
        self.assertTrue(profile.kids_ok)
        self.assertEqual(recs.kids_factor(profile, "movie", rec(1, KIDS)), 1.0)

    def test_diversify_breaks_up_lookalikes(self, _mock):
        """MMR prefers a different title over a near duplicate."""
        items = [
            {"media_id": "a", "match": 1.0},
            {"media_id": "b", "match": 0.95},
            {"media_id": "c", "match": 0.8},
        ]
        vectors = {
            "a": {"g:x": 1, "k:1": 1},
            "b": {"g:x": 1, "k:1": 1},
            "c": {"g:y": 1},
        }
        ranked = [item["media_id"] for item in recs.diversify(items, vectors, 3)]
        self.assertEqual(ranked, ["a", "c", "b"])

    def test_mood_excludes_kids_genres(self, _mock):
        """Feel-good moods no longer force animation or family titles."""
        for feeling in ("cheerful", "low", "stressed"):
            plan = recs.mood_plan({"feeling": feeling, "energy": "light"})
            self.assertNotIn("animation", plan["preferred"])
            self.assertNotIn("family", plan["preferred"])
            excluded = plan["filters"]["without_genres"].split(",")
            self.assertIn("16", excluded)
            self.assertIn("10751", excluded)

    def test_mood_allows_kids_for_family_or_fans(self, _mock):
        """Family viewing and animation fans still get them."""
        family = recs.mood_plan({"feeling": "low", "company": "family"})
        self.assertIn("10751", family["filters"]["with_genres"])
        fan = recs.mood_plan({"feeling": "low"}, allow_kids=True)
        self.assertNotIn("16", fan["filters"].get("without_genres", "").split(","))

    @patch("app.providers.tmdb.discover")
    def test_mood_ranks_main_genre_over_kids(self, discover, _mock):
        """A mood pick matching the main genre beats an animated family comedy."""
        discover.return_value = {
            "page": 1,
            "total_pages": 1,
            "results": [rec(700, KIDS, score=8.5), rec(701, (53, 80))],
        }
        self.track("10", Status.COMPLETED.value, 10)
        result = recs.mood_recommendations(
            recs.build_profile(self.user),
            {"feeling": "edgy"},
            rng=random.Random(1),  # noqa: S311
        )
        self.assertEqual(result["pick"]["media_id"], "701")

    @patch("app.providers.tmdb.discover")
    def test_surprise_excludes_kids(self, discover, _mock):
        """Surprise picks leave out kids titles unless that genre is asked for."""
        discover.return_value = {"page": 1, "total_pages": 1, "results": [rec(800)]}
        profile = recs.build_profile(self.user)
        recs.surprise(profile, "comedy", "movie", rng=random.Random(1))  # noqa: S311
        self.assertIn("10751", discover.call_args[0][1]["without_genres"])
        recs.surprise(profile, "animation", "movie", rng=random.Random(1))  # noqa: S311
        self.assertNotIn("without_genres", discover.call_args[0][1])


PERSON = {
    "id": 77,
    "name": "Christopher Nolan",
    "image": "http://example.com/p.jpg",
    "biography": "Director.",
    "known_for": "Directing",
    "birthday": "1970-07-30",
    "deathday": None,
    "place_of_birth": "London",
    "credits": [
        {**rec(157336), "roles": ["Director", "Writer"],
         "departments": ["Directing", "Writing"]},
        {**rec(1396, media_type="tv"), "roles": ["Himself"],
         "departments": ["Acting"]},
    ],
}


class PersonTests(RecommendationsTestBase):
    """Tests for the in-app person page."""

    @patch("app.providers.tmdb.person", return_value=PERSON)
    def test_person_page(self, _mock):
        """The person page lists their movies and shows with roles."""
        response = self.client.get(reverse("person_details", args=[77]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Christopher Nolan")
        self.assertContains(response, "Title 157336")
        self.assertContains(response, "Director, Writer")
        self.assertContains(response, "Title 1396")
        self.assertContains(response, "Directing")

    def test_person_credits_are_merged(self):
        """A title with several roles appears once with all roles."""
        from app.providers import tmdb  # noqa: PLC0415

        raw = {"id": 1, "title": "X", "media_type": "movie", "release_date": "2010"}
        merged = tmdb._person_credits(
            [{**raw, "character": "Cobb"}, {"id": 2, "media_type": "person"}],
            [{**raw, "job": "Director", "department": "Directing"}],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["roles"], ["Cobb", "Director"])

    def test_key_crew(self):
        """Directors come before writers and jobs are merged."""
        from app.providers import tmdb  # noqa: PLC0415

        crew = tmdb.get_key_crew(
            [
                {"id": 2, "name": "W", "job": "Screenplay"},
                {"id": 1, "name": "D", "job": "Director"},
                {"id": 1, "name": "D", "job": "Writer"},
                {"id": 3, "name": "G", "job": "Gaffer"},
            ],
        )
        self.assertEqual([c["id"] for c in crew], [1, 2])
        self.assertEqual(crew[0]["character"], "Director, Writer")
