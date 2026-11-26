"""Export project source for review, excluding settings, credentials, data and model caches."""

import argparse
import zipfile
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument(
        "--out", type=Path, default=Path.cwd() / "agent-platform-local-source.zip"
    )
    args = p.parse_args()
    root, output = args.project.expanduser().resolve(), args.out.expanduser().resolve()
    if not (root / "app/main.py").is_file():
        raise SystemExit("没有找到 app/main.py，请核对 --project。")
    roots = [
        root / n
        for n in (
            "app",
            "scripts",
            "tests",
            "examples",
            "requirements.txt",
            "requirements.in",
            "requirements-dev.txt",
            "Dockerfile",
            "compose.yaml",
            "README.md",
            "UPGRADE.md",
            "METADATA_STANDARD.md",
            "LLM_INTENT_GUIDE.md",
            "docs",
            "VALIDATION.md",
            "start.sh",
            ".github",
            ".gitignore",
            ".dockerignore",
        )
    ]
    blocked = {
        ".env",
        "settings.json",
        "state",
        "__pycache__",
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
    }
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as z:
        for base in roots:
            files = base.rglob("*") if base.is_dir() else [base]
            for path in files:
                rel = path.relative_to(root)
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and not blocked.intersection(rel.parts)
                    and path != output
                ):
                    z.write(path, Path("agent-platform") / rel)
    print(output)
    print(
        "包含源码；未打包 .env、settings.json、state。上传前请检查源码中是否手写过密钥。"
    )


if __name__ == "__main__":
    main()
