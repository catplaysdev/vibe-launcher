"""The plan editor's account check: what differs between the accounts a plan may switch between, and how to fix it.

Environment-layer differences (settings, extension files, approval rules) are aligned to a reference account the user
picks, or the account leaves the plan. Account-level ones (plugins, connected apps, MCP sign-ins) cannot be copied: the
user installs or signs in on that account and checks again, or accepts switching to it without them. The editor owns
the state (report, accepted, reference); this dialog only shows it and calls the editor."""
from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import account_check
from .i18n import tr
from .kit import dispose_widget, label, make_button
from .theme import C


class CheckRunner(QObject):
    """Runs one piece of work off the GUI thread and answers on it; a newer run makes older answers stale."""
    done = Signal(int, object, object)          # generation, result, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self.generation = 0

    def run(self, work: Callable[[], Any]) -> int:
        self.generation += 1
        generation = self.generation

        def target():
            try:
                result, error = work(), None
            except Exception as failure:        # reported to the caller, never raised in the thread
                result, error = None, failure
            try:
                self.done.emit(generation, result, error)
            except RuntimeError:
                pass                            # the window closed meanwhile
        threading.Thread(target=target, daemon=True).start()
        return generation


def finding_text(finding: account_check.Finding, names: dict) -> str:
    name = finding.name
    text = {"settings": tr("Settings or extension file: {name}", name=name),
            "rules": tr("Approval rules: {name}", name=name),
            "login": tr("Not signed in"),
            "plugin": tr("Plugin {name}", name=name),
            "app": tr("Connected app {name}", name=name),
            "mcp": tr("MCP sign-in {name}", name=name),
            "mcp_unknown": tr("MCP sign-in status unknown: {name}", name=name),
            "error": {"extensions": tr("Its extensions could not be refreshed"),
                      "settings": tr("Its settings could not be read"),
                      "capabilities": tr("Its plugins, apps and MCP sign-ins could not be read")}.get(name, name)}[finding.kind]
    if finding.holders:
        text += " · " + tr("{accounts} has it", accounts=", ".join(names.get(item, item) for item in finding.holders))
    return text


class AccountCheckDialog(QDialog):
    """Shows ``editor``'s account check and offers the fixes; rebuilds whenever the editor reports a new state."""
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setWindowTitle(tr("Check accounts"))
        self.resize(640, 560)
        outer = QVBoxLayout(self)
        outer.addWidget(label(tr("Accounts of this plan"), "heading"))
        outer.addWidget(label(tr("Switching keeps the conversation only when settings, extension files and approval rules "
                                 "are the same on every account. Plugins, connected apps and MCP sign-ins belong to each "
                                 "account: install or sign in there and check again, or accept switching without them."),
                              "meta", wrap=True))
        row = QHBoxLayout()
        row.addWidget(label(tr("Reference account")))
        self.reference = QComboBox()
        self.reference.setAccessibleName(tr("Reference account: the others are aligned to it"))
        self.reference.currentIndexChanged.connect(self._reference_chosen)
        row.addWidget(self.reference, 1)
        outer.addLayout(row)
        self.body = QVBoxLayout()
        holder = QWidget()
        holder.setLayout(self.body)
        holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)
        actions = QHBoxLayout()
        self.recheck_button = make_button(tr("Check again"), "secondary", callback=editor.recheck)
        actions.addWidget(self.recheck_button)
        actions.addStretch(1)
        actions.addWidget(make_button(tr("Done"), "primary", callback=self.accept))
        outer.addLayout(actions)
        editor.accountsChecked.connect(self.render)
        self.render()

    def _reference_chosen(self, _index):
        identifier = self.reference.currentData()
        if identifier and identifier != self.editor.reference:
            self.editor.set_reference(identifier)

    def render(self):
        editor = self.editor
        accounts = editor.check_set()
        self.reference.blockSignals(True)
        self.reference.clear()
        for account in accounts:
            self.reference.addItem(editor.names.get(account["id"], account["id"]), account["id"])
        self.reference.setCurrentIndex(max(0, self.reference.findData(editor.reference)))
        self.reference.blockSignals(False)
        while self.body.count():
            item = self.body.takeAt(0)
            if item.widget():
                dispose_widget(item.widget())
        state = editor.check_state
        self.recheck_button.setEnabled(state not in ("checking", "unavailable"))
        if state != "done" or editor.report is None:
            self.body.addWidget(label(editor.account_summary(), wrap=True))
            self.body.addStretch(1)
            return
        open_keys = {(finding.account_id, finding.key) for finding in editor.report.open(editor.accepted_dict())}
        for account in accounts:
            self.body.addWidget(self._account_card(account, open_keys))
        self.body.addStretch(1)

    def _account_card(self, account: dict, open_keys: set) -> QWidget:
        editor, identifier = self.editor, account["id"]
        findings = [finding for finding in editor.report.findings if finding.account_id == identifier]
        card = QFrame()
        card.setObjectName("accountCard")
        card.setStyleSheet("#accountCard { border: 1px solid %s; border-radius: 8px; }" % C.border)
        layout = QVBoxLayout(card)
        pending = sum(1 for finding in findings if (identifier, finding.key) in open_keys)
        title = editor.names.get(identifier, identifier)
        if identifier == editor.reference:
            title += " · " + tr("reference")
        state = tr("Ready") if not pending else tr("{count} to resolve", count=pending)
        heading = label(title + " — " + state, "heading")
        heading.setStyleSheet("color: %s;" % (C.text if not pending else C.warning))
        layout.addWidget(heading)
        environment = [finding for finding in findings if finding.kind in account_check.ALIGNABLE]
        for finding in environment:
            layout.addWidget(label("• " + finding_text(finding, editor.names), wrap=True))
        for finding in findings:
            if finding.kind in ("login", "error"):
                layout.addWidget(label("• " + finding_text(finding, editor.names), wrap=True))
        if any(finding.kind == "login" for finding in findings):
            layout.addWidget(label(tr("Sign in to this account on the Accounts page, then check again."), "meta", wrap=True))
        gaps = [finding for finding in findings if finding.kind not in account_check.ALIGNABLE + ("login", "error")]
        for finding in gaps:
            box = QCheckBox(tr("Switch without it: {item}", item=finding_text(finding, editor.names)))
            box.setChecked((identifier, finding.key) not in open_keys)
            box.toggled.connect(lambda on, key=finding.key: editor.accept_gap(identifier, key, on))
            layout.addWidget(box)
        if gaps:
            layout.addWidget(label(tr("Or install it or sign in on this account, then check again."), "meta", wrap=True))
        buttons = QHBoxLayout()
        if environment and identifier != editor.reference:
            buttons.addWidget(make_button(tr("Align to {account}", account=editor.names.get(editor.reference, editor.reference)),
                                          "secondary", small=True, callback=lambda: editor.align_account(identifier)))
        if findings and identifier != editor.own_account:
            buttons.addWidget(make_button(tr("Remove from this plan"), "ghost", small=True,
                                          callback=lambda: editor.remove_account(identifier)))
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return card
