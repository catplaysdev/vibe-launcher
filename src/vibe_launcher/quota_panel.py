"""Account quota ("how much of my Codex 5-hour / weekly allowance is left") shown under the accounts list."""
from __future__ import annotations

from .i18n import tr

from datetime import datetime
from typing import Any

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from .kit import Section, dispose_widget, label, make_button
from .theme import C, S

PLANS = {"free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite", "promax": "Pro Max", "team": "Team",
         "business": "Business", "enterprise": "Enterprise", "edu": "Edu"}


def window_label(minutes: int | None) -> str:
    if not minutes:
        return tr('quota_panel.42723e8b498c')
    if minutes == 10080:
        return tr('quota_panel.a8a3dfb18c80')
    if minutes % 1440 == 0:
        return tr('quota_panel.ab2b69cb4764', v0=f'{minutes // 1440}')
    if minutes % 60 == 0:
        return tr('quota_panel.df62b2b872dc', v0=f'{minutes // 60}')
    return tr('quota_panel.b445211874e5', v0=f'{minutes}')


def duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return tr('quota_panel.b46fd2c3ba6b', v0=f'{days}', v1=f'{hours}')
    if hours:
        return tr('quota_panel.0b73b30e2869', v0=f'{hours}', v1=f'{minutes}')
    return tr('quota_panel.b445211874e5', v0=f'{max(1, minutes)}')


def reset_text(resets_at: int | None, now: datetime | None = None) -> str:
    """"09:50 重置（3 小時 51 分後）" — the exact time matters more than the countdown, so both are shown."""
    if not resets_at:
        return tr('quota_panel.60a81b68cf15')
    now = now or datetime.now().astimezone()
    moment = datetime.fromtimestamp(resets_at).astimezone()
    left = (moment - now).total_seconds()
    if left <= 0:
        return tr('quota_panel.ed106f54b324')
    day = moment.date()
    if day == now.date():
        when = tr('quota_panel.84ed87a5e738', v0=f'{moment:%H:%M}')
    elif (day - now.date()).days == 1:
        when = tr('quota_panel.a98079c95e2a', v0=f'{moment:%H:%M}')
    else:
        when = f"{moment:%m/%d %H:%M}"
    return tr('quota_panel.c81be39d1a5f', v0=f'{when}', v1=f'{duration_text(left)}')


def checked_text(checked_at: str | None, now: datetime | None = None) -> str:
    try:
        moment = datetime.fromisoformat(str(checked_at))
    except ValueError:
        return ""
    now = now or datetime.now().astimezone()
    seconds = (now - moment.astimezone()).total_seconds()
    if seconds < 60:
        return tr('quota_panel.42dbeff3eb86')
    return tr('quota_panel.e8d5d4ee24a8', v0=f'{duration_text(seconds)}')


def bar_color(used: float) -> str:
    return C.danger if used >= 90 else C.warning if used >= 70 else C.accent


class UsageBar(QWidget):
    """A thin meter: the fill is how much is used, so a nearly full bar means a nearly spent allowance."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.used = 0.0
        self.setFixedHeight(10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_used(self, used: float) -> None:
        self.used = max(0.0, min(100.0, float(used)))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.surface_sunken))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        if self.used > 0:
            fill = QRectF(rect.x(), rect.y(), max(rect.height(), rect.width() * self.used / 100), rect.height())
            painter.setBrush(QColor(bar_color(self.used)))
            painter.drawRoundedRect(fill, rect.height() / 2, rect.height() / 2)


class QuotaPanel(Section):
    """Hidden for API/endpoint accounts (they are billed per use); a hint for Claude; the real meters for Codex."""

    query_requested = Signal(str, str)      # account_id, environment_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(tr('quota_panel.06ba141f00ab'), parent)
        self._account: dict[str, Any] | None = None
        self._signature: Any = None
        self.environment = QComboBox()
        self.environment.setAccessibleName(tr('quota_panel.0fc07519adef'))
        self.environment.setToolTip(tr('quota_panel.1ec4e201a196'))
        self.query_button = make_button(tr('quota_panel.45fa89c99be8'), "secondary", "refresh", small=True, callback=lambda *_: self._ask())
        self.add_head_widget(self.environment)
        self.add_head_widget(self.query_button)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(S.md)
        self.body.addLayout(self.rows)
        self.meta = label("", "meta", wrap=True)
        self.body.addWidget(self.meta)
        self.hide()

    def _ask(self) -> None:
        if self._account is not None and self.environment.currentData():
            self.query_requested.emit(self._account["id"], self.environment.currentData())

    def selected_environment(self) -> str:
        return self.environment.currentData() or "local"

    def show_account(self, account: dict[str, Any] | None, environments: list[dict[str, Any]], report: dict[str, Any] | None,
                     *, busy: bool = False, note: str = "", preferred_environment: str = "") -> None:
        signature = (account and account.get("id"), account and account.get("auth"), account and account.get("tool"),
                     tuple((e.get("id"), e.get("name")) for e in environments), self.selected_environment(),
                     report and report.get("checked_at"), busy, note)
        if signature == self._signature:
            return
        self._signature = signature
        self._account = account
        if account is None or account.get("auth") != "subscription":
            self.hide()           # an endpoint / API key has no quota window to show
            return
        self.show()
        supported = account.get("tool") == "codex"
        current = preferred_environment or self.environment.currentData() or "local"
        self.environment.blockSignals(True)
        self.environment.clear()
        for env in environments:
            self.environment.addItem(env.get("name") or env["id"], env["id"])
        index = self.environment.findData(current)
        self.environment.setCurrentIndex(max(0, index))
        self.environment.blockSignals(False)
        self.environment.setVisible(supported and self.environment.count() > 1)
        self.query_button.setVisible(supported)
        self.query_button.setEnabled(not busy)
        self.query_button.setText(tr('quota_panel.b8bf2f806c91') if busy else tr('quota_panel.45fa89c99be8'))
        self._clear()
        if not supported:
            self.rows.addWidget(label(tr('quota_panel.4e53527290f6'), "muted", wrap=True))
            self.meta.setText("")
            return
        if report is None:
            self.rows.addWidget(label(tr('quota_panel.592ee4fcd647'), "muted", wrap=True))
        else:
            self._render_report(report)
        self.meta.setText(note)

    def _clear(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())
            elif item.layout() is not None:
                self._dispose_layout(item.layout())

    def _dispose_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                dispose_widget(item.widget())

    def _render_report(self, report: dict[str, Any]) -> None:
        facts = []
        buckets = report.get("buckets")
        if isinstance(buckets, dict) and buckets:
            for limit_id, bucket in buckets.items():
                bucket_name = ""
                if len(buckets) > 1 or limit_id != "codex":
                    name = bucket.get("name")
                    bucket_name = f"{name} ({limit_id})" if name and name != limit_id else limit_id
                    heading = label(bucket_name, "body", wrap=True)
                    heading.setTextFormat(Qt.TextFormat.PlainText)
                    self.rows.addWidget(heading)
                facts.extend(self._render_bucket(bucket, bucket_name))
        else:
            facts.extend(self._render_bucket(report))
        if report.get("plan"):
            facts.insert(0, tr('quota_panel.ad6de16c7ffe') + PLANS.get(report["plan"], str(report["plan"])))
        if report.get("reset_credits"):
            facts.append(tr('quota_panel.55f2426ad3f1', v0=f"{report['reset_credits']}"))
        stamp = checked_text(report.get("checked_at"))
        if stamp:
            facts.append(stamp)
        if facts:
            self.rows.addWidget(label(" · ".join(facts), "meta", wrap=True))

    def _render_bucket(self, report: dict[str, Any], bucket_name: str = "") -> list[str]:
        windows = report.get("windows") or []
        if not windows:
            self.rows.addWidget(label(tr('Usage limits not reported for this bucket.') if bucket_name
                                      else tr('quota_panel.d21633520309'), "muted", wrap=True))
        for window in windows:
            used = float(window.get("used_percent", 0))
            block = QWidget()
            column = QVBoxLayout(block)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(S.xs)
            top = QHBoxLayout()
            title = label(window_label(window.get("window_minutes")) + tr('quota_panel.3748bf25da95'), "body")
            percent = label(tr('quota_panel.ffb17d69e66f', v0=f'{used:g}', v1=f'{100 - used:g}'), "body")
            percent.setProperty("tone", "danger" if used >= 90 else "warn" if used >= 70 else "accent")
            percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            top.addWidget(title, 1)
            top.addWidget(percent)
            column.addLayout(top)
            bar = UsageBar()
            bar.set_used(used)
            accessible = tr('quota_panel.ea202405770d', v0=f"{window_label(window.get('window_minutes'))}", v1=f'{used:g}')
            bar.setAccessibleName(f"{bucket_name}: {accessible}" if bucket_name else accessible)
            column.addWidget(bar)
            column.addWidget(label(reset_text(window.get("resets_at")), "meta"))
            self.rows.addWidget(block)
        facts = []
        credits = report.get("credits")
        if credits and credits.get("unlimited"):
            facts.append(tr('quota_panel.8f20a4d3a750'))
        elif credits and credits.get("has_credits") and credits.get("balance"):
            facts.append(tr('quota_panel.8465f26a3a04', v0=f"{credits['balance']}"))
        if report.get("reached"):
            facts.append(tr('quota_panel.43db587ce0a2'))
        if facts and bucket_name:
            self.rows.addWidget(label(" · ".join(facts), "meta", wrap=True))
            return []
        return facts
