"""Account quota ("how much of my Codex 5-hour / weekly allowance is left") shown under the accounts list."""
from __future__ import annotations

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
        return "用量"
    if minutes == 10080:
        return "每週"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小時"
    return f"{minutes} 分鐘"


def duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小時"
    if hours:
        return f"{hours} 小時 {minutes} 分"
    return f"{max(1, minutes)} 分鐘"


def reset_text(resets_at: int | None, now: datetime | None = None) -> str:
    """"09:50 重置（3 小時 51 分後）" — the exact time matters more than the countdown, so both are shown."""
    if not resets_at:
        return "重置時間未提供"
    now = now or datetime.now().astimezone()
    moment = datetime.fromtimestamp(resets_at).astimezone()
    left = (moment - now).total_seconds()
    if left <= 0:
        return "已重置，重新查詢可取得最新用量"
    day = moment.date()
    if day == now.date():
        when = f"今天 {moment:%H:%M}"
    elif (day - now.date()).days == 1:
        when = f"明天 {moment:%H:%M}"
    else:
        when = f"{moment:%m/%d %H:%M}"
    return f"{when} 重置（{duration_text(left)}後）"


def checked_text(checked_at: str | None, now: datetime | None = None) -> str:
    try:
        moment = datetime.fromisoformat(str(checked_at))
    except ValueError:
        return ""
    now = now or datetime.now().astimezone()
    seconds = (now - moment.astimezone()).total_seconds()
    if seconds < 60:
        return "剛剛查詢"
    return f"{duration_text(seconds)}前查詢"


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
        super().__init__("用量配額", parent)
        self._account: dict[str, Any] | None = None
        self._signature: Any = None
        self.environment = QComboBox()
        self.environment.setAccessibleName("查詢配額的環境")
        self.environment.setToolTip("登入資料存放在各環境，所以要選擇查詢哪個環境上的這個帳號")
        self.query_button = make_button("查詢配額", "secondary", "refresh", small=True, callback=lambda *_: self._ask())
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
        self.query_button.setText("查詢中…" if busy else "查詢配額")
        self._clear()
        if not supported:
            self.rows.addWidget(label("Claude 訂閱沒有提供給外部程式查詢的配額介面。請在 Claude CLI 內輸入 /usage 查看。", "muted", wrap=True))
            self.meta.setText("")
            return
        if report is None:
            self.rows.addWidget(label("尚未查詢。按「查詢配額」取得 5 小時與每週的用量與重置時間。", "muted", wrap=True))
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
        windows = report.get("windows") or []
        if not windows:
            self.rows.addWidget(label("這個帳號沒有回報任何用量上限。", "muted", wrap=True))
        for window in windows:
            used = float(window.get("used_percent", 0))
            block = QWidget()
            column = QVBoxLayout(block)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(S.xs)
            top = QHBoxLayout()
            title = label(window_label(window.get("window_minutes")) + " 額度", "body")
            percent = label(f"已用 {used:g}% · 剩 {100 - used:g}%", "body")
            percent.setStyleSheet(f"color: {bar_color(used)}; font-weight: 600;")
            percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            top.addWidget(title, 1)
            top.addWidget(percent)
            column.addLayout(top)
            bar = UsageBar()
            bar.set_used(used)
            bar.setAccessibleName(f"{window_label(window.get('window_minutes'))}額度已用 {used:g}%")
            column.addWidget(bar)
            column.addWidget(label(reset_text(window.get("resets_at")), "meta"))
            self.rows.addWidget(block)
        facts = []
        if report.get("plan"):
            facts.append("方案 " + PLANS.get(report["plan"], str(report["plan"])))
        credits = report.get("credits")
        if credits and credits.get("unlimited"):
            facts.append("額外點數：無上限")
        elif credits and credits.get("has_credits") and credits.get("balance"):
            facts.append(f"額外點數餘額 {credits['balance']}")
        if report.get("reset_credits"):
            facts.append(f"可用的額度重置券 {report['reset_credits']} 張")
        if report.get("reached"):
            facts.append("已達使用上限")
        stamp = checked_text(report.get("checked_at"))
        if stamp:
            facts.append(stamp)
        if facts:
            self.rows.addWidget(label(" · ".join(facts), "meta", wrap=True))
