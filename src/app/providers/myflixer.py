import logging
import re
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://myflixerz.to"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36"
)
HTTP_OK = 200


def search_show(title):
    """Search for a show on MyFlixer and return the show page URL if found."""
    search_url = f"{BASE_URL}/search?q={quote(title)}"
    try:
        response = requests.get(
            search_url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to search MyFlixer for '%s': %s", title, e)
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    film_list = soup.find("div", class_="film_list-wrap")
    if not film_list:
        return None

    for item in film_list.find_all("div", class_="flw-item"):
        film_detail = item.find("div", class_="film-detail")
        if not film_detail:
            continue

        film_name = film_detail.find("h2", class_="film-name")
        if not film_name:
            continue

        link = film_name.find("a")
        if not link or not link.get("href"):
            continue

        href = link.get("href")
        if "/tv/" in href:
            film_title = film_name.get("title", "")
            title_lower = title.lower()
            film_title_lower = film_title.lower()
            if title_lower in film_title_lower or film_title_lower in title_lower:
                return f"{BASE_URL}{href}"

    return None


def _extract_tmdb_id(show_url):
    """Extract TMDB ID from show URL like https://myflixerz.to/tv/rooster-147206."""
    if not show_url:
        return None
    match = re.search(r"-(\d+)$", show_url)
    return match.group(1) if match else None


def _extract_slug(show_url):
    """Extract slug from show URL like https://myflixerz.to/tv/rooster-147206."""
    if not show_url:
        return None
    match = re.search(r"/tv/([^/]+)-\d+$", show_url)
    return match.group(1) if match else None


def get_season_id(tmdb_id):
    """Get the first season ID for a show via AJAX endpoint."""
    try:
        response = requests.get(
            f"{BASE_URL}/ajax/season/list/{tmdb_id}",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch season list for TMDB ID '%s': %s", tmdb_id, e)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    first_season = soup.find("a", {"data-toggle": "tab"})
    if first_season:
        season_id = first_season.get("data-id")
        logger.debug("Found season ID: %s for TMDB ID: %s", season_id, tmdb_id)
        return season_id
    return None


def get_episode_urls_for_season(season_id):
    """Get episode to server ID mapping for a season via AJAX endpoint.

    Returns dict mapping episode_number (int) to server_id (str).
    """
    episode_mapping = {}
    try:
        response = requests.get(
            f"{BASE_URL}/ajax/season/episodes/{season_id}",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch episodes for season ID '%s': %s", season_id, e)
        return episode_mapping

    soup = BeautifulSoup(response.text, "html.parser")

    for eps_item in soup.find_all("a", class_="eps-item"):
        data_id = eps_item.get("data-id")
        title = eps_item.get("title", "")

        match = re.search(r"Eps\s*(\d+)", title, re.IGNORECASE)
        if match and data_id:
            episode_number = int(match.group(1))
            episode_mapping[episode_number] = data_id

    logger.debug("Episode mapping for season %s: %s", season_id, episode_mapping)
    return episode_mapping


def get_episode_url(show_url, season_number, episode_number):
    """Get the direct episode streaming URL from a show page via AJAX.

    Note: season_number is intentionally unused as episodes are fetched dynamically
    via AJAX and matched by episode number.
    """
    if not show_url:
        return None

    tmdb_id = _extract_tmdb_id(show_url)
    if not tmdb_id:
        logger.warning("Could not extract TMDB ID from show_url: %s", show_url)
        return show_url

    season_id = get_season_id(tmdb_id)
    if not season_id:
        logger.warning("Could not get season ID for TMDB ID: %s", tmdb_id)
        return show_url

    episode_mapping = get_episode_urls_for_season(season_id)
    episode_id = episode_mapping.get(episode_number)

    if not episode_id:
        logger.warning(
            "Episode %d not found in season %s mapping",
            episode_number,
            season_id,
        )
        return show_url

    slug = _extract_slug(show_url)
    if not slug:
        logger.warning("Could not extract slug from show_url: %s", show_url)
        return show_url

    episode_url = f"{BASE_URL}/watch-tv/{slug}.{episode_id}"
    logger.debug("Built episode URL: %s", episode_url)
    return episode_url


def verify_url(url):
    """Verify that a URL exists and returns a 200 status code."""
    if not url:
        return False

    try:
        response = requests.head(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
            allow_redirects=True,
        )
    except requests.RequestException:
        return False
    else:
        return response.status_code == HTTP_OK
