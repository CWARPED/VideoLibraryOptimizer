"""Tests for TMDB parsing (no network) and keyword fallback."""

from __future__ import annotations

from vlo.metadata.keywords import looks_like_animation
from vlo.metadata.tmdb import TmdbClient


def _client_with(monkeypatch, payload):
    client = TmdbClient("fake-key")
    monkeypatch.setattr(client, "_get_json", lambda url: payload)
    return client


def test_tmdb_animation_movie(monkeypatch):
    client = _client_with(monkeypatch, {"results": [
        {"id": 9, "genre_ids": [16, 35], "original_language": "en", "origin_country": ["US"]}
    ]})
    info = client.lookup("Some Cartoon", 2010, "movie")
    assert info is not None
    assert info.content_type == "animation"
    assert info.is_anime is False


def test_tmdb_anime_tv(monkeypatch):
    client = _client_with(monkeypatch, {"results": [
        {"id": 5, "genre_ids": [16], "original_language": "ja", "origin_country": ["JP"]}
    ]})
    info = client.lookup("Some Anime", None, "tv")
    assert info.content_type == "animation"
    assert info.is_anime is True


def test_tmdb_live_action(monkeypatch):
    client = _client_with(monkeypatch, {"results": [
        {"id": 1, "genre_ids": [28, 12], "original_language": "en"}
    ]})
    info = client.lookup("Action Movie", 2020, "movie")
    assert info.content_type == "live_action"
    assert info.is_anime is False


def test_tmdb_no_results(monkeypatch):
    client = _client_with(monkeypatch, {"results": []})
    assert client.lookup("Unknown", 2020, "movie") is None


def test_tmdb_network_error_returns_none(monkeypatch):
    client = _client_with(monkeypatch, None)  # _get_json returned None (error)
    assert client.lookup("X", 2020, "movie") is None


def test_tmdb_no_key_returns_none():
    assert TmdbClient("").lookup("X", 2020, "movie") is None


def test_keyword_detection():
    assert looks_like_animation(r"\\nas\Anime\Naruto\S01E01.mkv")
    assert looks_like_animation("/media/Dessins Animés/Tom et Jerry.mkv")
    assert looks_like_animation("/media/Animation/Up (2009).mkv")
    assert not looks_like_animation("/media/Films/Inception (2010).mkv")


def test_keyword_custom_list():
    assert looks_like_animation("/x/Toons/y.mkv", ["toons"])
    assert not looks_like_animation("/x/Anime/y.mkv", ["toons"])
