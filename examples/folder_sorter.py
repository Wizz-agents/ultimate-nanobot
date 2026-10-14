"""folder_sorter (GENERAL avenue): sort loose files into folders by extension.

Dry-run by default: it only PLANS moves until you pass --live. It never
overwrites a file and never deletes anything.

    python -m ultimate_nanobot promote examples.folder_sorter:bot
    python -m ultimate_nanobot run examples.folder_sorter:bot --input '{"root": "~/Downloads"}'
    python -m ultimate_nanobot run examples.folder_sorter:bot --input '{"root": "~/Downloads"}' --live

Or run this file for a self-contained demo in a temp directory:

    python examples/folder_sorter.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ultimate_nanobot import Action, Avenue, Canary, Nanobot, Result, Runner  # noqa: E402

DEFAULT_RULES = {
    "images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"],
    "documents": [".pdf", ".txt", ".md", ".doc", ".docx", ".odt", ".csv", ".xlsx"],
    "archives": [".zip", ".tar", ".gz", ".7z", ".rar"],
    "audio": [".mp3", ".wav", ".flac", ".m4a"],
    "video": [".mp4", ".mov", ".mkv", ".webm"],
}


def _folder_for(path: Path, rules: dict) -> str | None:
    ext = path.suffix.lower()
    for folder, exts in rules.items():
        if ext in exts:
            return folder
    return None


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 1
    while True:
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _move(action: Action) -> None:
    src, dest = Path(action.payload["src"]), Path(action.payload["dest"])
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


class FolderSorter(Nanobot):
    name = "folder-sorter"
    cadence = 15 * 60
    avenue = Avenue.GENERAL
    dry_run = True  # plan only until --live
    expected_empty_ok = True  # an already-tidy folder is a normal outcome
    description = "Sort loose files into folders by extension."

    def work(self, ctx) -> Result:
        root = ctx.input.get("root") or os.environ.get("FOLDER_SORTER_ROOT")
        if not root:
            raise ValueError("set input.root or FOLDER_SORTER_ROOT")
        root = Path(os.path.expanduser(root))
        if not root.is_dir():
            raise NotADirectoryError(root)
        rules = ctx.input.get("rules") or DEFAULT_RULES
        planned: list[Action] = []
        taken: set[Path] = set()
        for f in sorted(root.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            folder = _folder_for(f, rules)
            if folder is None:
                continue
            dest = _unique(root / folder / f.name)
            while dest in taken:  # two files planned to the same name this run
                dest = _unique(dest.with_name(f"{dest.stem}_{len(taken)}{dest.suffix}"))
            taken.add(dest)
            planned.append(
                Action(
                    kind="move",
                    key=str(f),
                    payload={"src": str(f), "dest": str(dest)},
                    perform=_move,
                    description=f"{f.name} -> {folder}/",
                )
            )
        by_folder = Counter(Path(a.payload["dest"]).parent.name for a in planned)
        return Result(
            produced=len(planned),
            artifact={"root": str(root), "planned": len(planned), "by_folder": dict(by_folder)},
            actions=planned,
        )

    def canary(self) -> Canary:
        tmp = Path(tempfile.mkdtemp(prefix="nanobot-canary-sorter-"))
        for name in ("photo.jpg", "report.pdf", "mystery.xyz"):
            (tmp / name).write_text("canary")
        return Canary(
            input={"root": str(tmp)},
            expect_produced=2,
            expect_artifact={"by_folder": {"images": 1, "documents": 1}},
            expect_action_kinds=["move", "move"],
        )


bot = FolderSorter()


def demo() -> str:
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
        for name in ("holiday.jpg", "invoice.pdf", "song.mp3", "notes.unknown"):
            Path(root, name).write_text("demo")
        runner = Runner(home)
        runner.promote(bot)
        dry = runner.run_once(bot, input={"root": root})
        assert all(a.status == "dry_run" for a in dry.actions)
        live = runner.run_once(bot, input={"root": root}, live=True)
        moved = sum(a.status == "performed" for a in live.actions)
        folders = sorted(p.name for p in Path(root).iterdir() if p.is_dir())
        runner.close()
        return (
            f"folder_sorter: dry-run planned {dry.produced} moves (0 touched), "
            f"--live moved {moved} into {folders}; verdict={live.verdict}"
        )


if __name__ == "__main__":
    print(demo())
