"""Explicit review and copy of a polished result, without target-app input."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)
from paths import ROOT


class PolishResultDialog(QDialog):
    def __init__(self, original: str | None, polished: str, parent=None, *,
                 title="Odicto · Polish", explanation=None):
        super().__init__(parent)
        font_id = QFontDatabase.addApplicationFont(str(ROOT / "assets/fonts/InstrumentSans-VF.ttf"))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families:
            self.setFont(QFont(families[0], 11))
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(560, 440)
        self.setMinimumSize(320, 320)
        self.setStyleSheet("""
            QDialog { background: #1b1d20; color: #eef1ed; }
            QLabel { color: #c2c7c3; }
            QPlainTextEdit { background: #111415; color: #eef1ed; border: 1px solid #343936;
                border-radius: 8px; padding: 10px; }
            QPushButton { background: #252a28; color: #eef1ed; border: 1px solid #454e49;
                border-radius: 8px; padding: 8px 16px; }
            QPushButton:focus { border-color: #8edcbb; }
            QPushButton#copyPolish { background: #26785e; color: white; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        help_text = QLabel(explanation or "Review the edit, then copy it when ready. Your document has not been changed.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.original = None
        if original is not None:
            layout.addWidget(QLabel("Original text"))
            self.original = QPlainTextEdit(original)
            self.original.setReadOnly(True)
            self.original.setAccessibleName("Original text")
            self.original.setMaximumHeight(110)
            layout.addWidget(self.original)
        layout.addWidget(QLabel("Polished text" if original is not None else "Text ready to copy"))
        self.polished = QPlainTextEdit(polished)
        self.polished.setAccessibleName("Polished text" if original is not None else "Text ready to copy")
        layout.addWidget(self.polished, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.clicked.connect(self.reject)
        buttons.addWidget(self.dismiss_button)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setObjectName("copyPolish")
        self.copy_button.clicked.connect(self.copy_result)
        buttons.addWidget(self.copy_button)
        layout.addLayout(buttons)

    def copy_result(self):
        QApplication.clipboard().setText(self.polished.toPlainText())
        self.copy_button.setText("Copied")
