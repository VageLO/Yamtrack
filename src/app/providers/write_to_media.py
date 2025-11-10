import os
import re
import sys
import logging
import argparse
import requests
from django.conf import settings
import requests
import langcodes
import subprocess
from pathlib import Path
from contextlib import suppress
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


def build_payload(
    title: str, year: int, episode: Optional[int], season: Optional[int]
) -> Dict[str, object]:
    payload = {"title": title, "year": year}
    if episode is not None:
        payload["episode"] = episode
    if season is not None:
        payload["season"] = season
    return payload


def post_to_webhook(
    url: str, payload: Dict[str, Any]
) -> (List[str], Optional[Dict[str, str]], str):
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        response = response.json()

        if "error" in response:
            raise ValueError(response["error"])

        links = response.get("links", [])
        if not links:
            raise ValueError("Expected a non-empty list of string URLs in response.")
        logger.info("Received %d URL(s) in response.", len(links))

        subs = response.get("subtitles", {})
        logger.info("Received %d SUBS in response.", len(subs))

        return links, subs, ""

    except Exception as e:
        logger.error("Webhook request failed: %s", e)
        return [], [], str(e)


def get_language_code(language_name: str) -> str:
    """Returns ".en" or empty str"""
    try:
        lang = langcodes.find(language_name)
        return f".{lang.language}"
    except LookupError:
        return ""


def download_file(url: str, path: str) -> None:
    try:
        logger.info("Downloading from URL: %s to %s", url, path)
        response = requests.get(url)
        if response.ok:
            with open(path, "wb") as file:
                file.write(response.content)
                file.close()
            logger.info("File downloaded successfully")
        else:
            logger.error("Failed to download file")
        # subprocess.run(["curl", "-L", "-o", path, url], check=True).check_returncode()
    except subprocess.CalledProcessError as e:
        logger.error("Download failed: %s", e)


def check_file_size(url: str) -> int:
    response = requests.head(url)
    if "Content-Length" in response.headers:
        size = int(response.headers["Content-Length"])
        return size
    return 0


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are invalid in filenames."""
    # Remove anything not alphanumeric, space, dash, underscore, or parentheses
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", name)
    sanitized = sanitized.strip()
    return sanitized


def pad_number(num: int) -> str:
    """Pad season/episode numbers with leading zero if needed."""
    return f"{num:02d}"


def join_path_from_env(
    env_var_name: str, *path_components: str, strict: bool = False
) -> Optional[Path]:
    """
    Constructs a path by joining an environment variable's value with additional path components.

    Args:
        env_var_name (str): Name of the environment variable containing the base path.
        *path_components (str): Variable number of path components to join.
        strict (bool): If True, raises an exception if the environment variable is not set.
                      If False, returns None when the variable is not set. Defaults to False.

    Returns:
        Optional[Path]: The joined Path object, or None if the environment variable is not set
                       and strict is False.

    Raises:
        ValueError: If the environment variable is not set and strict is True.
        OSError: If path construction fails due to invalid characters or permissions.

    Example:
        >>> os.environ['BASE_PATH'] = '/home/user'
        >>> join_path_from_env('BASE_PATH', 'folder', 'file.txt')
        PosixPath('/home/user/folder/file.txt')
    """
    try:
        base_path = os.getenv(env_var_name)

        if base_path is None:
            if strict:
                logger.error(f"Environment variable '{env_var_name}' not found")
                raise ValueError(f"Environment variable '{env_var_name}' is not set")
            logger.debug(
                f"Environment variable '{env_var_name}' not found, returning None"
            )
            return None

        # Normalize and validate base path
        base_path = os.path.expanduser(base_path.strip())
        if not base_path:
            if strict:
                logger.error(f"Environment variable '{env_var_name}' is empty")
                raise ValueError(f"Environment variable '{env_var_name}' is empty")
            logger.debug(
                f"Environment variable '{env_var_name}' is empty, returning None"
            )
            return None

        with suppress(ValueError, OSError):
            full_path = Path(base_path).joinpath(*path_components).resolve()

            logger.debug(f"Constructed path: {full_path}")
            return full_path

        logger.error(f"Invalid path components provided for {env_var_name}")
        raise OSError("Failed to construct valid path from components")

    except Exception as e:
        logger.exception(f"Error constructing path for {env_var_name}: {str(e)}")
        raise


def write_strm_file(
    title: str,
    year: int,
    url: str,
    download: bool,
    subtitles: Optional[Dict[str, str]] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> None:
    """
    Creates folder structure and writes .strm file with the URL.

    Folder: "Title (Year)/"
    If season & episode provided: "Title (Year) SXXEXX/"
    Filename inside folder follows the same naming convention.

    Args:
        title: Show or movie title.
        year: Year of release.
        url: URL to put inside the .strm file.
        season: Optional season number.
        episode: Optional episode number.
    """
    try:
        # Sanitize inputs
        safe_title = sanitize_filename(title)

        if season is not None and episode is not None:
            base_folder_name = f"{safe_title} ({year})"
            # base_folder_path = os.path.abspath(os.path.join("Shows", base_folder_name))

            env_var = "MEDIA_FOLDER"
            path_components = ["shows", base_folder_name]

            base_folder_path = join_path_from_env(env_var, *path_components)
        else:
            base_folder_name = f"{safe_title} ({year})"
            # base_folder_path = os.path.abspath(os.path.join("Movies", base_folder_name))

            env_var = "MEDIA_FOLDER"
            path_components = ["movies", base_folder_name]

        base_folder_path = join_path_from_env(env_var, *path_components, strict=True)

        os.makedirs(base_folder_path, exist_ok=True)
        logger.debug(f"Created/verified base folder: {base_folder_path}")

        # filepath: Path

        if season is not None and episode is not None:
            season_str = pad_number(season)
            episode_str = pad_number(episode)
            subfolder_name = f"Season S{season_str}"
            subfolder_path = base_folder_path.joinpath(
                sanitize_filename(subfolder_name)
            )

            os.makedirs(subfolder_path, exist_ok=True)

            filename = f"{safe_title} S{season_str}E{episode_str}"

            if download:
                filename += ".mp4"
            else:
                filename += ".strm"

            filepath = subfolder_path.joinpath(sanitize_filename(filename))

            if subtitles is not None:
                for key, value in subtitles.items():
                    code = get_language_code(key)
                    if code != ".en":
                        continue

                    sub_filename = f"{safe_title} S{season_str}E{episode_str}{code}.vtt"
                    sub_path = subfolder_path.joinpath(sub_filename)

                    # Download subs
                    download_file(url=value, path=sub_path)
        else:
            # No season/episode provided: just put .strm file in base folder
            filename = f"{base_folder_name}"
            if download:
                filename += ".mp4"
            else:
                filename += ".strm"

            filepath = base_folder_path.joinpath(sanitize_filename(filename))

            if subtitles is not None:
                for key, value in subtitles.items():
                    code = get_language_code(key)
                    if code != ".en":
                        continue

                    sub_filename = f"{base_folder_name}{code}.vtt"
                    sub_path = base_folder_path.joinpath(sub_filename)

                    # Download subs
                    download_file(url=value, path=sub_path)

        # Download mp4 file
        if download:
            download_file(url=url, path=filepath)
            logger.info("Created .mp4 file at: %s", filepath)
            return

        # Write the URL into the .strm file
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(url + "\n")

        logger.info("Created .strm file at: %s", filepath)

    except Exception as e:
        logger.error("Failed to create folder or write .strm file: %s", e)
        raise


def find_largest_file(urls: List[str]) -> (Optional[str], Optional[int]):
    """
    Find the URL with the largest file size and compute unique size + index sums.

    Args:
        urls (List[str]): List of file URLs to check.

    Returns:
        Optional[str]: URL of the largest file, or None if no valid sizes are found.
    """
    unique_sums = set()
    max_size = -1
    max_size_mb = -1
    max_size_url = None

    for index, url in enumerate(urls):
        size = check_file_size(url)
        size_mb = int(size / (1024 * 1024))
        logger.debug(f"URL: {url}, Size: {size} bytes; {size_mb} mb, Index: {index}")

        # Compute size + index and add to set
        total = size + index
        unique_sums.add(total)

        # Track the largest file
        if size > max_size:
            max_size = size
            max_size_mb = size_mb
            max_size_url = url

    logger.debug(f"Unique sums of size + index: {unique_sums}")

    if max_size_url:
        logger.info(
            f"Largest file: {max_size_url} ({max_size} bytes, {max_size_mb} mb)"
        )
    else:
        logger.error("No valid file sizes found")

    return max_size_url, max_size_mb


def write_media(
    title: str,
    year: int,
    episode: Optional[int],
    season: Optional[int],
    download: Optional[bool] = False,
) -> str:
    payload = build_payload(title, year, episode, season)

    i = 0
    while i < 5:
        urls, subs, error = post_to_webhook(settings.N8N_URL, payload)
        if error != "No url's found":
            break
        i += 1

    if i == 5:
        return "Try again"

    largest_url, size = find_largest_file(urls)

    write_strm_file(
        title=title,
        year=year,
        url=largest_url,
        subtitles=subs,
        season=season,
        episode=episode,
        download=download,
    )

    # Scan Jellyfin library
    url = settings.JELLY_URL
    headers = {
        "Authorization": f"MediaBrowser Token={settings.JELLY_TOKEN}",
    }
    response = requests.post(url=url, headers=headers)

    if response.ok:
        logger.info(f"Scanning Jellyfin library: {url}")
    else:
        logger.error(f"Jellyfin status code: {response.status_code}")

    return f"Loaded {size} MB"
