"""Server-side lookups against the VidSrc info API (https://vsembed.ru/vidsrc/docs/)."""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TIMEOUT = 60 * 60
REQUEST_TIMEOUT = 4
MAX_WORKERS = 8


def _cache_key(tmdb_id):
    return f"vidsrc_tv_ranges_{tmdb_id}"


def _season_ranges(data):
    """Map season number to its (first, last) available episode number."""
    ranges = {}
    for season in (data or {}).get("seasons") or []:
        try:
            ranges[int(season["season"])] = (
                int(season["first_episode"]),
                int(season["last_episode"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return ranges


def _fetch_show(tmdb_id):
    """Return the show's season ranges, ``{}`` if VidSrc lacks it, None if unknown."""
    for domain in settings.VIDSRC_DOMAINS:
        try:
            response = requests.get(
                f"https://{domain}/info/tv/{tmdb_id}.json",
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            continue
        if response.status_code == requests.codes.not_found:
            return {}
        if not response.ok:
            continue
        try:
            return _season_ranges(response.json())
        except ValueError:
            continue
    logger.warning("VidSrc availability unknown for TMDB TV %s", tmdb_id)
    return None


def tv_episode_ranges(tmdb_ids):
    """Return ``{tmdb_id: {season: (first, last)}}`` for the given shows.

    Shows whose availability could not be determined are left out.
    """
    tmdb_ids = {str(tmdb_id) for tmdb_id in tmdb_ids}
    cached = cache.get_many([_cache_key(tmdb_id) for tmdb_id in tmdb_ids])
    result = {}
    to_fetch = []
    for tmdb_id in tmdb_ids:
        key = _cache_key(tmdb_id)
        if key in cached:
            result[tmdb_id] = cached[key]
        else:
            to_fetch.append(tmdb_id)

    if to_fetch:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for tmdb_id, ranges in zip(
                to_fetch,
                pool.map(_fetch_show, to_fetch),
                strict=True,
            ):
                if ranges is None:
                    continue
                cache.set(_cache_key(tmdb_id), ranges, CACHE_TIMEOUT)
                result[tmdb_id] = ranges
    return result


def episode_available(ranges, season_number, episode_number):
    """Whether an episode falls inside VidSrc's available range for its season."""
    season_range = ranges.get(season_number)
    if not season_range:
        return False
    first, last = season_range
    return first <= episode_number <= last
