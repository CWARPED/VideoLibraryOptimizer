"""Tests for movie/series classification (pure, path-based)."""

from __future__ import annotations

from vlo.core.enums import MediaKind
from vlo.scan.classifier import classify, slugify


def test_sxxexx_filename():
    c = classify("/lib/Show.Name.S01E02.1080p.BluRay.x265.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 1 and c.episode == 2
    assert c.series_title == "Show Name"
    assert c.series_slug == "show-name"


def test_nxnn_filename():
    c = classify("/lib/Series Name - 1x05.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 1 and c.episode == 5
    assert c.series_title == "Series Name"


def test_season_folder_with_sxxexx():
    c = classify("/lib/Breaking Bad/Season 03/Breaking Bad - S03E07 - Felina.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 3 and c.episode == 7
    assert c.series_slug == "breaking-bad"


def test_season_folder_episode_only():
    c = classify("/lib/Breaking Bad/Saison 1/Episode 04.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 1 and c.episode == 4
    assert c.series_title == "Breaking Bad"


def test_multi_episode():
    c = classify("/lib/Show.S01E01E02.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 1 and c.episode == 1  # first episode of the range


def test_specials_folder():
    c = classify("/lib/Show/Specials/Show S00E01.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 0 and c.episode == 1


def test_movie_with_year():
    c = classify("/lib/Inception (2010) 1080p BluRay x265.mkv")
    assert c.kind is MediaKind.MOVIE
    assert c.title == "Inception"
    assert c.year == 2010


def test_movie_named_like_number_is_not_episode():
    # "2160p" must not be read as 21x60; a numeric title must still stay a movie.
    c = classify("/lib/1922.2021.2160p.WEB-DL.x265.mkv")
    assert c.kind is MediaKind.MOVIE


def test_resolution_not_parsed_as_episode():
    c = classify("/lib/Some.Movie.1920x1080.mkv")
    assert c.kind is MediaKind.MOVIE


def test_dotted_release_name():
    c = classify("/lib/The.Show.S02E10.MULTI.1080p.WEB.H264-GROUP.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 2 and c.episode == 10
    assert c.series_title == "The Show"


def test_slugify_merges_accents_and_case():
    assert slugify("Téléfilm Génial") == slugify("telefilm genial")
    assert slugify("Game of Thrones") == "game-of-thrones"


def test_windows_path():
    c = classify(r"D:\Films\Series\Dark\Season 02\Dark.S02E03.mkv")
    assert c.kind is MediaKind.EPISODE
    assert c.season == 2 and c.episode == 3
    assert c.series_slug == "dark"
