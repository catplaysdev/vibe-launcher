"""Project tags: collecting them, matching them, and the row of chips that filters by them."""
from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from .kit import FilterChip, FlowLayout, dispose_widget, make_button
from .theme import S


def project_tags(project: dict) -> list[str]:
    tags = project.get("tags")
    return [t for t in tags if isinstance(t, str) and t] if isinstance(tags, list) else []


def collect_tags(projects: Iterable[dict]) -> dict[str, int]:
    """Every tag in use with how many projects carry it. "UI" and "ui" are one tag (the first spelling is shown)."""
    shown: dict[str, str] = {}
    counts: dict[str, int] = {}
    for project in projects:
        for tag in {t.casefold(): t for t in project_tags(project)}.values():
            key = tag.casefold()
            shown.setdefault(key, tag)
            counts[key] = counts.get(key, 0) + 1
    return {shown[key]: counts[key] for key in sorted(counts, key=lambda k: shown[k].casefold())}


def matches_tags(project: dict, selected: Iterable[str]) -> bool:
    """A project passes when it carries EVERY selected tag, so each extra chip narrows the list further."""
    have = {t.casefold() for t in project_tags(project)}
    return all(tag.casefold() in have for tag in selected)


class TagFilterBar(QWidget):
    """Toggle chips, one per tag in use. Hidden while no project has a tag. ``changed`` fires only on user clicks."""

    changed = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._layout = FlowLayout(self, S.xs + 2, S.xs + 2)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._counts: dict[str, int] = {}
        self._selected: list[str] = []
        self._signature: object = None
        self.chips: dict[str, FilterChip] = {}
        self.clear_button = None
        self.hide()

    def selected(self) -> list[str]:
        return list(self._selected)

    def set_selected(self, tags: Iterable[str]) -> None:
        self._selected = [t for t in tags if isinstance(t, str) and t]
        self._rebuild()

    def set_tags(self, counts: dict[str, int]) -> bool:
        """Show these tags. A selected tag that no longer exists is dropped; returns True when that changed the selection."""
        known = {t.casefold(): t for t in counts}
        kept = [known[t.casefold()] for t in self._selected if t.casefold() in known]
        pruned = kept != self._selected
        self._selected, self._counts = kept, dict(counts)
        self._rebuild()
        return pruned

    def _toggle(self, tag: str) -> None:
        if tag in self._selected:
            self._selected.remove(tag)
        else:
            self._selected.append(tag)
        self._rebuild()
        self.changed.emit(self.selected())

    def _clear(self) -> None:
        self._selected = []
        self._rebuild()
        self.changed.emit([])

    def _rebuild(self) -> None:
        signature = (tuple(self._counts.items()), tuple(self._selected))
        if signature == self._signature:
            return
        self._signature = signature
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())
        self.chips = {}
        for tag, count in self._counts.items():
            chip = FilterChip(tag, "#" + tag, self)
            chip.set_count(count)
            chip.setChecked(tag in self._selected)
            chip.setToolTip(f"只看有「{tag}」標籤的專案（可複選，同時符合）")
            chip.clicked.connect(lambda _=False, t=tag: self._toggle(t))
            self._layout.addWidget(chip)
            self.chips[tag] = chip
        self.clear_button = None
        if self._selected:
            self.clear_button = make_button("清除標籤篩選", "ghost", "x", small=True, callback=lambda *_: self._clear())
            self._layout.addWidget(self.clear_button)
        self.setVisible(bool(self._counts))
        self.updateGeometry()
