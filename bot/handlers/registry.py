"""Декларативная регистрация хендлеров: модуль описывает маршруты, а свежий Router
собирается при каждом build() — так бота можно собрать несколько раз (например, в тестах)."""

from collections.abc import Callable
from typing import Any

from aiogram import Router


class Routes:
    def __init__(self, name: str, message_filters: tuple[Any, ...] = ()) -> None:
        self.name = name
        self._message_filters = message_filters
        self._items: list[tuple[str, Callable, tuple[Any, ...]]] = []

    def _decorator(self, kind: str, filters: tuple[Any, ...]):
        def wrap(fn: Callable) -> Callable:
            self._items.append((kind, fn, filters))
            return fn

        return wrap

    def message(self, *filters: Any):
        return self._decorator("message", filters)

    def callback_query(self, *filters: Any):
        return self._decorator("callback_query", filters)

    def build(self) -> Router:
        router = Router(name=self.name)
        if self._message_filters:
            router.message.filter(*self._message_filters)
        for kind, fn, filters in self._items:
            getattr(router, kind).register(fn, *filters)
        return router
