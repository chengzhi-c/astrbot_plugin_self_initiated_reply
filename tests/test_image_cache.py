"""Byte-budget tests for the in-memory Vision description cache."""

from __future__ import annotations

import importlib

from .test_vision import PACKAGE_NAME, _load_modules


def _cache_class():
    _load_modules()
    module = importlib.import_module(f"{PACKAGE_NAME}.image.cache")
    return module.ImageCache


def test_image_cache_evicts_old_entries_when_byte_budget_is_exceeded() -> None:
    image_cache = _cache_class()(max_size=10, max_bytes=5)

    image_cache.put("old", "1234")
    image_cache.put("new", "12")

    assert image_cache.get("old") is None
    assert image_cache.get("new") == "12"
    assert image_cache.bytes_used == 2


def test_image_cache_replacement_updates_byte_accounting() -> None:
    image_cache = _cache_class()(max_size=10, max_bytes=5)

    image_cache.put("same", "1234")
    image_cache.put("same", "1")
    image_cache.put("other", "23456")

    assert image_cache.get("same") is None
    assert image_cache.get("other") == "23456"
    assert image_cache.bytes_used == 5
