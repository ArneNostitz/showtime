"""Personal "what to watch" recommendations built on top of TMDB.

The engine derives a taste profile from the user's tracked movies and TV
shows (status, score and recency), then uses it to:

- rank TMDB recommendations of the user's favourite titles ("For you"),
- pick a random, well rated title in a genre ("Surprise me"),
- turn a short mood quiz into discover filters re-ranked by taste.
"""

import logging
import math
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests
from django.utils import timezone

from app.models import TV, MediaTypes, Movie, Sources, Status
from app.providers import services, tmdb

logger = logging.getLogger(__name__)

MOVIE = MediaTypes.MOVIE.value
TV_TYPE = MediaTypes.TV.value
SUPPORTED_MEDIA_TYPES = (MOVIE, TV_TYPE)

# TMDB uses different genre ids for movies and TV, and TV merges some genres
# (e.g. "Sci-Fi & Fantasy"), so genres are unified under one key.
GENRES = {
    "action": {"label": "Action", "movie": 28, "tv": 10759},
    "adventure": {"label": "Adventure", "movie": 12, "tv": 10759},
    "animation": {"label": "Animation", "movie": 16, "tv": 16},
    "comedy": {"label": "Comedy", "movie": 35, "tv": 35},
    "crime": {"label": "Crime", "movie": 80, "tv": 80},
    "documentary": {"label": "Documentary", "movie": 99, "tv": 99},
    "drama": {"label": "Drama", "movie": 18, "tv": 18},
    "family": {"label": "Family", "movie": 10751, "tv": 10751},
    "fantasy": {"label": "Fantasy", "movie": 14, "tv": 10765},
    "history": {"label": "History", "movie": 36, "tv": None},
    "horror": {"label": "Horror", "movie": 27, "tv": None},
    "music": {"label": "Music", "movie": 10402, "tv": None},
    "mystery": {"label": "Mystery", "movie": 9648, "tv": 9648},
    "romance": {"label": "Romance", "movie": 10749, "tv": None},
    "scifi": {"label": "Sci-Fi", "movie": 878, "tv": 10765},
    "thriller": {"label": "Thriller", "movie": 53, "tv": None},
    "war": {"label": "War & Politics", "movie": 10752, "tv": 10768},
    "western": {"label": "Western", "movie": 37, "tv": 37},
}

GENRE_ID_TO_KEYS = {MOVIE: defaultdict(list), TV_TYPE: defaultdict(list)}
for _key, _genre in GENRES.items():
    for _media_type in SUPPORTED_MEDIA_TYPES:
        if _genre[_media_type] is not None:
            GENRE_ID_TO_KEYS[_media_type][_genre[_media_type]].append(_key)

STATUS_WEIGHTS = {
    Status.COMPLETED.value: 1.0,
    Status.IN_PROGRESS.value: 1.0,
    Status.PAUSED.value: 0.5,
    Status.PLANNING.value: 0.25,
    Status.DROPPED.value: -1.0,
}
SEEDS_PER_TYPE = 12
DISLIKED_SEEDS_PER_TYPE = 5
RECS_PER_SEED = 20
MAX_WORKERS = 6
NEW_TO_YOU_THRESHOLD = 0.15



def _option(value, label, hint=""):
    return {"value": value, "label": label, "hint": hint}


MOOD_QUESTIONS = [
    {
        "key": "feeling",
        "question": "How are you feeling today?",
        "options": [
            _option("cheerful", "Cheerful", "keep the good vibes going"),
            _option("low", "A bit down", "cheer me up"),
            _option("stressed", "Stressed", "help me unwind"),
            _option("adventurous", "Adventurous", "take me somewhere"),
            _option("curious", "Curious", "make me think"),
            _option("romantic", "Romantic", "all the feels"),
            _option("edgy", "Edgy", "thrill or scare me"),
        ],
    },
    {
        "key": "energy",
        "question": "How much brainpower do you have left?",
        "options": [
            _option("light", "Switch-off mode", "easy to follow"),
            _option("engaged", "Bring it on", "something to chew on"),
        ],
    },
    {
        "key": "time",
        "question": "How much time have you got?",
        "options": [
            _option("short", "Quick movie", "under 100 minutes"),
            _option("evening", "A movie night", "length doesn't matter"),
            _option("episode", "An episode or two", "a show to dip into"),
            _option("binge", "Binge time", "a show to get hooked on"),
        ],
    },
    {
        "key": "novelty",
        "question": "Familiar or fresh?",
        "options": [
            _option("comfort", "Safe bet", "popular & in my taste"),
            _option("fresh", "Something new", "outside my usual"),
            _option("classic", "A classic", "before 2000"),
            _option("recent", "Brand new", "last few years"),
        ],
    },
    {
        "key": "company",
        "question": "Who's watching?",
        "options": [
            _option("solo", "Just me"),
            _option("date", "Date night"),
            _option("friends", "With friends"),
            _option("family", "The whole family"),
        ],
    },
]

FEELING_GENRES = {
    "cheerful": (["comedy", "adventure", "animation", "music"], ["horror"]),
    "low": (["comedy", "family", "animation", "romance"], ["horror", "war"]),
    "stressed": (["comedy", "animation", "family"], ["horror", "thriller", "war"]),
    "adventurous": (["adventure", "action", "scifi", "fantasy"], []),
    "curious": (["documentary", "mystery", "scifi", "history"], []),
    "romantic": (["romance", "comedy", "drama"], ["horror"]),
    "edgy": (["thriller", "horror", "crime", "mystery"], ["family"]),
}
ENERGY_GENRES = {
    "light": (["comedy", "action", "animation"], ["documentary", "history"]),
    "engaged": (["drama", "mystery", "scifi"], []),
}
COMPANY_GENRES = {
    "solo": ([], []),
    "date": (["romance", "comedy"], []),
    "friends": (["comedy", "action", "horror"], []),
    "family": (["family", "animation"], ["horror", "thriller", "crime", "war"]),
}


@dataclass
class Seed:
    """A tracked title that drives the recommendations."""

    media_type: str
    media_id: str
    title: str
    weight: float
    data: dict = field(default_factory=dict)


@dataclass
class TasteProfile:
    """What the user likes, derived from their tracked titles."""

    seeds: list
    genre_affinity: dict
    tracked: set

    def seeds_for(self, media_type):
        """Return liked seeds of a media type that have TMDB data."""
        return [
            seed
            for seed in self.seeds
            if seed.media_type == media_type and seed.weight > 0 and seed.data
        ]

    def is_tracked(self, media_type, media_id):
        """Return whether the user already tracks the title."""
        return (media_type, str(media_id)) in self.tracked

    def genre_fit(self, media_type, genre_ids):
        """Return the average affinity (-1..1) for a title's genres."""
        keys = genre_keys(media_type, genre_ids)
        if not keys:
            return 0.0
        return sum(self.genre_affinity.get(key, 0.0) for key in keys) / len(keys)

    @property
    def has_data(self):
        """Return whether there is enough history to personalise."""
        return bool(self.genre_affinity)


def genre_keys(media_type, genre_ids):
    """Map TMDB genre ids to unified genre keys."""
    keys = []
    for genre_id in genre_ids or []:
        for key in GENRE_ID_TO_KEYS[media_type].get(genre_id, []):
            if key not in keys:
                keys.append(key)
    return keys


def genre_ids(media_type, keys):
    """Map unified genre keys to the TMDB ids of a media type."""
    ids = []
    for key in keys:
        genre_id = GENRES.get(key, {}).get(media_type)
        if genre_id is not None and genre_id not in ids:
            ids.append(genre_id)
    return ids


def media_weight(media, now=None):
    """Return how strongly a tracked title reflects the user's taste."""
    now = now or timezone.now()
    if media.status == Status.DROPPED.value:
        weight = STATUS_WEIGHTS[Status.DROPPED.value]
    else:
        weight = STATUS_WEIGHTS.get(media.status, 0.5)
        if media.score is not None:
            # 10 -> 2.0, 7 -> 1.0, 4 -> 0.0, 1 -> -1.0
            weight *= (float(media.score) - 4) / 3

    activity = media.end_date or media.start_date or media.created_at
    if activity:
        days = max((now - activity).days, 0)
        weight *= 0.5 + 0.5 * math.exp(-days / 365)
    return weight


def _fetch_seed(seed):
    try:
        return tmdb.taste_seed(seed.media_type, seed.media_id)
    except (services.ProviderAPIError, requests.exceptions.RequestException):
        logger.warning("Could not load TMDB data for %s", seed.title)
        return {}


def build_profile(user, *, fetch=True):
    """Build the taste profile of a user.

    With ``fetch=False`` only TMDB data already in the cache is used, which
    keeps page loads fast.
    """
    now = timezone.now()
    tracked = set()
    seeds = []

    for model, media_type in ((Movie, MOVIE), (TV, TV_TYPE)):
        weighted = []
        for media in model.objects.filter(user=user).select_related("item"):
            tracked.add((media_type, str(media.item.media_id)))
            if media.item.source != Sources.TMDB.value:
                continue
            weighted.append(
                Seed(
                    media_type=media_type,
                    media_id=str(media.item.media_id),
                    title=media.item.title,
                    weight=media_weight(media, now),
                ),
            )

        liked = sorted(
            (seed for seed in weighted if seed.weight > 0),
            key=lambda seed: seed.weight,
            reverse=True,
        )
        disliked = sorted(
            (seed for seed in weighted if seed.weight < 0),
            key=lambda seed: seed.weight,
        )
        seeds.extend(liked[:SEEDS_PER_TYPE])
        seeds.extend(disliked[:DISLIKED_SEEDS_PER_TYPE])

    if fetch and seeds:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for seed, data in zip(seeds, executor.map(_fetch_seed, seeds), strict=True):
                seed.data = data
    else:
        for seed in seeds:
            seed.data = tmdb.cache.get(
                f"{Sources.TMDB.value}_taste_seed_{seed.media_type}_{seed.media_id}",
            ) or {}

    affinity = defaultdict(float)
    for seed in seeds:
        for key in genre_keys(seed.media_type, seed.data.get("genre_ids")):
            affinity[key] += seed.weight

    if affinity:
        top = max(abs(value) for value in affinity.values()) or 1
        affinity = {key: value / top for key, value in affinity.items()}

    return TasteProfile(seeds=seeds, genre_affinity=dict(affinity), tracked=tracked)


def _quality(item):
    """Return a 0..1 quality score from the TMDB rating."""
    if item.get("score_count", 0) < 50:  # noqa: PLR2004
        return 0.3
    return min(max((item.get("score", 0) - 5) / 4, 0.0), 1.0)


def _because(reasons):
    titles = [title for title, _ in sorted(reasons.items(), key=lambda r: -r[1])[:2]]
    return "Because you liked " + " & ".join(titles)


def for_you(profile, media_type, limit=18):
    """Return personalised recommendations of a media type."""
    candidates = {}
    for seed in profile.seeds_for(media_type):
        recommendations = seed.data.get("recommendations", [])[:RECS_PER_SEED]
        for rank, item in enumerate(recommendations):
            if profile.is_tracked(media_type, item["media_id"]):
                continue
            contribution = seed.weight / math.sqrt(rank + 1)
            candidate = candidates.setdefault(
                item["media_id"],
                {"item": item, "support": 0.0, "reasons": defaultdict(float)},
            )
            candidate["support"] += contribution
            candidate["reasons"][seed.title] += contribution

    if not candidates:
        return _trending_fallback(profile, media_type, limit)

    top_support = max(candidate["support"] for candidate in candidates.values())
    results = []
    for candidate in candidates.values():
        item = candidate["item"]
        fit = profile.genre_fit(media_type, item.get("genre_ids"))
        score = (
            0.6 * candidate["support"] / top_support
            + 0.25 * (fit + 1) / 2
            + 0.15 * _quality(item)
        )
        results.append(
            {**item, "match": score, "reason": _because(candidate["reasons"])},
        )

    results.sort(key=lambda item: item["match"], reverse=True)
    return results[:limit]


def _trending_fallback(profile, media_type, limit):
    try:
        trending = tmdb.trending(media_type)
    except (services.ProviderAPIError, requests.exceptions.RequestException):
        return []
    return [
        {**item, "reason": "Trending this week"}
        for item in trending
        if not profile.is_tracked(media_type, item["media_id"])
    ][:limit]


def genre_choices(profile):
    """Return genres for the surprise picker, flagging unexplored ones."""
    return [
        {
            "key": key,
            "label": genre["label"],
            "movie": genre[MOVIE] is not None,
            "tv": genre[TV_TYPE] is not None,
            "new_to_you": profile.has_data
            and profile.genre_affinity.get(key, 0.0) < NEW_TO_YOU_THRESHOLD,
        }
        for key, genre in GENRES.items()
    ]


def _pick_unexplored_genre(profile, media_type, rng):
    keys = [key for key, genre in GENRES.items() if genre[media_type] is not None]
    unexplored = [
        key
        for key in keys
        if profile.genre_affinity.get(key, 0.0) < NEW_TO_YOU_THRESHOLD
    ]
    return rng.choice(unexplored or keys)


def surprise(
    profile,
    genre,
    media_type,
    *,
    hidden_gems=False,
    exclude=(),
    rng=None,
):
    """Return a random untracked, well rated pick plus alternatives.

    ``genre`` can be ``"random"`` to explore a genre the user rarely watches.
    """
    rng = rng or random.Random()  # noqa: S311
    if media_type not in SUPPORTED_MEDIA_TYPES:
        msg = f"Unsupported media type: {media_type}"
        raise ValueError(msg)
    if genre == "random":
        genre = _pick_unexplored_genre(profile, media_type, rng)
    if genre not in GENRES or GENRES[genre][media_type] is None:
        msg = f"Genre {genre} is not available for {media_type}"
        raise ValueError(msg)

    filters = {"with_genres": GENRES[genre][media_type]}
    if hidden_gems:
        filters.update(
            {
                "sort_by": "vote_average.desc",
                "vote_average.gte": 7,
                "vote_count.gte": 50 if media_type == MOVIE else 30,
                "vote_count.lte": 1000 if media_type == MOVIE else 400,
            },
        )
    else:
        filters.update(
            {
                "sort_by": rng.choice(["popularity.desc", "vote_average.desc"]),
                "vote_average.gte": 6.5,
                "vote_count.gte": 300 if media_type == MOVIE else 150,
            },
        )

    first_page = tmdb.discover(media_type, filters)
    max_page = min(first_page["total_pages"], 20)
    exclude = {str(media_id) for media_id in exclude}

    def fresh(results):
        return [
            item
            for item in results
            if not profile.is_tracked(media_type, item["media_id"])
            and item["media_id"] not in exclude
        ]

    candidates = []
    pages = rng.sample(range(1, max_page + 1), min(3, max_page))
    for page in pages:
        results = first_page if page == 1 else tmdb.discover(media_type, filters, page)
        candidates = fresh(results["results"])
        if candidates:
            break
    if not candidates:
        candidates = fresh(first_page["results"])

    rng.shuffle(candidates)
    return {
        "genre": genre,
        "genre_label": GENRES[genre]["label"],
        "media_type": media_type,
        "hidden_gems": hidden_gems,
        "new_to_you": profile.has_data
        and profile.genre_affinity.get(genre, 0.0) < NEW_TO_YOU_THRESHOLD,
        "pick": candidates[0] if candidates else None,
        "alternatives": candidates[1:7],
    }


def _mood_genres(answers, media_type):
    """Return preferred and avoided genre keys for the mood answers."""
    points = defaultdict(float)
    avoid = set()
    for mapping, answer, weight in (
        (FEELING_GENRES, answers.get("feeling", "cheerful"), 3),
        (ENERGY_GENRES, answers.get("energy", "light"), 1),
        (COMPANY_GENRES, answers.get("company", "solo"), 1.5),
    ):
        preferred, avoided = mapping.get(answer, ([], []))
        for key in preferred:
            points[key] += weight
        avoid.update(avoided)

    for key in avoid:
        points.pop(key, None)
    ranked = sorted(points, key=lambda key: -points[key])
    preferred = [key for key in ranked if GENRES[key][media_type] is not None][:4]
    return preferred, avoid


def _apply_time(filters, time):
    """Apply the time budget answer and return a summary label."""
    if time == "short":
        filters["with_runtime.lte"] = 100
        return "under 100 min"
    if time == "episode":
        filters["with_runtime.lte"] = 45
        return "short episodes"
    if time == "binge":
        filters["vote_count.gte"] = 500
        return "bingeable"
    return None


def _apply_novelty(filters, novelty, media_type, today):
    """Apply the familiar-or-fresh answer and return a summary label."""
    date_field = "primary_release_date" if media_type == MOVIE else "first_air_date"
    if novelty == "classic":
        filters[f"{date_field}.lte"] = "1999-12-31"
        filters["vote_count.gte"] = max(filters["vote_count.gte"], 300)
        return "classic"
    if novelty == "recent":
        filters[f"{date_field}.gte"] = f"{today.year - 3}-01-01"
        return "recent"
    if novelty == "fresh":
        filters["sort_by"] = "vote_average.desc"
        filters["vote_count.gte"] = 80
        return "off the beaten path"
    return None


def mood_plan(answers, today=None):
    """Translate mood quiz answers into TMDB discover filters."""
    today = today or timezone.localdate()
    time = answers.get("time", "evening")
    novelty = answers.get("novelty", "comfort")
    media_type = TV_TYPE if time in ("episode", "binge") else MOVIE
    preferred, avoid = _mood_genres(answers, media_type)

    filters = {
        "sort_by": "popularity.desc",
        "vote_count.gte": 100 if media_type == TV_TYPE else 200,
        "vote_average.gte": 7.2 if answers.get("energy") == "engaged" else 6.3,
    }
    required = (
        ["family", "animation"] if answers.get("company") == "family" else preferred
    )
    if required:
        filters["with_genres"] = "|".join(
            str(genre_id) for genre_id in genre_ids(media_type, required)
        )
    avoid_ids = set(genre_ids(media_type, avoid)) - set(
        genre_ids(media_type, required),
    )
    if avoid_ids:
        filters["without_genres"] = ",".join(str(gid) for gid in sorted(avoid_ids))

    labels = [GENRES[key]["label"] for key in preferred[:3]]
    labels.append(_apply_time(filters, time))
    labels.append(_apply_novelty(filters, novelty, media_type, today))

    return {
        "media_type": media_type,
        "filters": filters,
        "preferred": preferred,
        "novelty": novelty,
        "summary": " · ".join(label for label in labels if label),
    }


def mood_recommendations(profile, answers, limit=9, rng=None):
    """Return a top pick and alternatives for the mood quiz answers."""
    rng = rng or random.Random()  # noqa: S311
    plan = mood_plan(answers)
    media_type = plan["media_type"]

    candidates = {}
    for page in (1, 2):
        try:
            results = tmdb.discover(media_type, plan["filters"], page)
        except (services.ProviderAPIError, requests.exceptions.RequestException):
            break
        for item in results["results"]:
            if not profile.is_tracked(media_type, item["media_id"]):
                candidates.setdefault(item["media_id"], item)
        if page >= results["total_pages"]:
            break

    preferred = set(plan["preferred"])
    ranked = []
    for item in candidates.values():
        keys = genre_keys(media_type, item.get("genre_ids"))
        mood_match = len(preferred.intersection(keys)) / max(len(preferred), 1)
        fit = profile.genre_fit(media_type, item.get("genre_ids"))
        taste = (1 - fit) / 2 if plan["novelty"] == "fresh" else (fit + 1) / 2
        score = (
            0.4 * _quality(item)
            + 0.35 * mood_match
            + 0.25 * taste
            + rng.uniform(0, 0.08)
        )
        ranked.append({**item, "match": score})

    ranked.sort(key=lambda item: item["match"], reverse=True)
    ranked = ranked[:limit]
    return {
        "summary": plan["summary"],
        "media_type": media_type,
        "pick": ranked[0] if ranked else None,
        "alternatives": ranked[1:],
    }


def genre_labels(media_type, ids):
    """Return readable genre labels for TMDB genre ids."""
    return [GENRES[key]["label"] for key in genre_keys(media_type, ids)]
