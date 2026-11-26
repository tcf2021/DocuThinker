"""Run from the NEW release in WSL. Back up the installed project before replacing code."""

import argparse
import json
import sys
import datetime
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from app.readiness import check_readiness  # noqa: E402

RELEASE = [
    "app",
    "scripts",
    "tests",
    "examples",
    "requirements.txt",
    "requirements.in",
    "requirements-dev.txt",
    "Dockerfile",
    "compose.yaml",
    "start.sh",
    "README.md",
    "UPGRADE.md",
    "METADATA_STANDARD.md",
    "LLM_INTENT_GUIDE.md",
    "VALIDATION.md",
    "docs",
    ".dockerignore",
    ".gitignore",
]


def main():
    parser = argparse.ArgumentParser(
        description="升级已安装的文档 Agent，保留账号、文件、配置和旧索引。"
    )
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target = args.target.expanduser().resolve()
    if (
        target == SOURCE
        or SOURCE.is_relative_to(target)
        or target.is_relative_to(SOURCE)
    ):
        raise SystemExit(
            "请把新版解压到独立目录，再指定旧项目目录；两个目录不能相同或互相包含。"
        )
    for name in ("settings.json", ".env", "compose.yaml", "app"):
        if not (target / name).exists():
            raise SystemExit(f"目标目录缺少 {name}，请核对旧平台目录：{target}")
    backup = target.parent / (
        target.name
        + "-backup-"
        + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid4().hex[:6]
    )
    print("旧项目：", target)
    print("备份至：", backup)
    print("保留 settings.json、.env、state；备份不复制可重新下载的 state/models 缓存。")
    def preflight():
        check_readiness(target / "state/metadata.sqlite", json.loads((target / "settings.json").read_text()))

    preflight()
    if args.dry_run:
        return
    compose = ["docker", "compose", "--project-directory", str(target)]
    subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(compose + ["stop", "agent"], check=True)

    def ignore(directory, names):
        excluded = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".git"}
        if Path(directory) == target / "state":
            excluded.add("models")
        return excluded.intersection(names)

    try:
        preflight()
        backup.mkdir(mode=0o700)
        shutil.copytree(target, backup, dirs_exist_ok=True, ignore=ignore)
    except Exception:
        subprocess.run(compose + ["start", "agent"], check=False)
        raise
    for name in RELEASE:
        source = SOURCE / name
        if not source.exists():
            continue
        destination = target / name
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "__pycache__", ".pytest_cache", ".ruff_cache"
                ),
            )
        else:
            shutil.copy2(source, destination)
    for retired in ("doclab-han.woff", "FONT-LICENSE.txt"):
        (target / "app/static" / retired).unlink(missing_ok=True)
    print("备份完成。正在构建并启动新版…", flush=True)
    try:
        subprocess.run(
            compose
            + ["up", "-d", "--build", "--wait", "--wait-timeout", "180", "agent"],
            check=True,
        )
    except subprocess.CalledProcessError:
        print(
            "启动未通过，请检查 docker compose logs --tail 80 agent。备份保存在：",
            backup,
        )
        print("完整恢复步骤见 UPGRADE.md。旧 Milvus 集合仍保留。")
        raise SystemExit(1)
    print("升级完成。打开原工作台地址，核对知识库与原文引用。")
    print("备份路径：", backup)


if __name__ == "__main__":
    main()
