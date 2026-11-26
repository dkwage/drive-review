"""Launch Drive Review."""

import argparse
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from drive_review.ui.window import MainWindow


def main():
    parser = argparse.ArgumentParser(description="Drive Review")
    parser.add_argument("folder", nargs="?", help="Folder or drive root to open at startup")
    parser.add_argument("--data-dir", help="Use a separate directory for local app data")
    args = parser.parse_args()

    app = QApplication([sys.argv[0]])
    app.setApplicationName("Drive Review")
    app.setStyle("Fusion")
    try:
        window = MainWindow(args.data_dir, list_drives=args.folder is None)
    except Exception as error:
        QMessageBox.critical(None, "Drive Review", str(error))
        return 1
    window.show()
    if args.folder:
        window.open_root(args.folder)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
