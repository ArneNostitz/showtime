import logging
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

    # Look for the first TV show result
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

        # Check if it's a TV show (href starts with /tv/)
        href = link.get("href")
        if "/tv/" in href:
            film_title = film_name.get("title", "")
            # Check if title matches (case-insensitive partial match)
            title_lower = title.lower()
            film_title_lower = film_title.lower()
            if title_lower in film_title_lower or film_title_lower in title_lower:
                return f"{BASE_URL}{href}"

    return None


def get_episode_url(show_url, season_number, episode_number):
    """Get the direct episode streaming URL from a show page."""
    if not show_url:
        return None

    try:
        response = requests.get(
            show_url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch show page '%s': %s", show_url, e)
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # Look for episode list
    episodes_container = soup.find("div", class_="episodes")
    if not episodes_container:
        return None

    for episode_item in episodes_container.find_all("a"):
        href = episode_item.get("href", "")
        ep_title = episode_item.get("title", "")

        # Check if this is the episode we're looking for
        s_marker = f"s{season_number}"
        e_marker = f"e{episode_number}"
        if s_marker in ep_title.lower() and e_marker in ep_title.lower():
            return f"{BASE_URL}{href}"

        # Alternative: check href pattern for episode number
        if f".{episode_number}" in href and e_marker in ep_title.lower():
            return f"{BASE_URL}{href}"

    return None


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
