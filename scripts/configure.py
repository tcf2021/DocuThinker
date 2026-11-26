"""WSL host installer. Standard library only. It never edits the previous project's files."""

import argparse
import getpass
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = Path("/mnt/d/AI/Dify/homework/.env")
SETTINGS = ROOT / "settings.json"


def read_env(path):
    vals = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            vals[key.strip()] = value
    return vals


def save(path, text):
    with tempfile.NamedTemporaryFile(
        dir=path.parent, mode="w", encoding="utf-8", delete=False
    ) as f:
        f.write(text)
        name = f.name
    os.replace(name, path)
    path.chmod(0o600)


def docker(*args):
    try:
        r = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=12
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if not r.returncode else ""


def detect_network(oldpath):
    container = (
        docker(
            "compose", "--project-directory", str(oldpath.parent), "ps", "-q", "minio"
        )
        if oldpath.exists()
        else ""
    )
    if not container:
        container = docker(
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.service=minio",
            "--filter",
            "label=com.docker.compose.project=mineru-image-homework",
        )
    if container and len(container.splitlines()) == 1:
        raw = docker(
            "inspect", "--format", "{{json .NetworkSettings.Networks}}", container
        )
        if raw:
            nets = json.loads(raw)
            aliases = [
                k for k, v in nets.items() if "minio" in (v.get("Aliases") or [])
            ]
            if len(aliases) == 1:
                return aliases[0]
            if len(nets) == 1:
                return next(iter(nets))
    return ""


def prompt(label, default=""):
    return input(label + (f" [{default}]" if default else "") + ": ").strip() or default


def usable(v):
    return v and not v.lower().startswith(("replace-", "your-"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--old-env", type=Path, default=OLD)
    p.add_argument("--network", help="Name of the existing MinIO Docker network")
    p.add_argument(
        "--intent-mode",
        choices=["auto", "rules", "llm", "manual"],
        help="Intent planner mode; rules makes no intent-model call",
    )
    p.add_argument(
        "--chat",
        action="store_true",
        help="Configure or update the chat provider interactively",
    )
    p.add_argument(
        "--agent",
        action="store_true",
        help="配置用于 Metadata/Intent 的内部 OpenAI-compatible LLM",
    )
    args = p.parse_args()
    oldpath = args.old_env.expanduser().resolve()
    prev = read_env(oldpath)
    if SETTINGS.exists():
        cfg = json.loads(SETTINGS.read_text())
        if (
            not args.chat
            and not args.agent
            and not args.network
            and not args.intent_mode
            and (ROOT / ".env").is_file()
        ):
            print("使用已有 settings.json 和 .env。")
            return
    else:
        print(
            "已找到原项目 .env。"
            if prev
            else "请按原 MinIO 的配置填写。密码输入时隐藏。"
        )
        s3_user = prev.get("S3_ACCESS_KEY") or prev.get("MINIO_ROOT_USER")
        s3_secret = prev.get("S3_SECRET_KEY") or prev.get("MINIO_ROOT_PASSWORD")
        cfg = {
            "mineru_url": prev.get("MINERU_API_URL")
            or "http://host.docker.internal:8000",
            "s3_endpoint": prev.get("S3_ENDPOINT")
            or prompt("MinIO S3 地址", "http://minio:9000"),
            "s3_bucket": prev.get("S3_BUCKET") or prompt("MinIO 桶名", "mineru-bucket"),
            "s3_region": prev.get("S3_REGION") or "us-east-1",
            "s3_key": s3_user
            if usable(s3_user)
            else prompt("MinIO Access Key / 原控制台用户名"),
            "s3_secret": s3_secret
            if usable(s3_secret)
            else getpass.getpass("MinIO Secret Key / 原控制台密码: "),
            "chat_base": "",
            "chat_model": "",
            "chat_key": "",
            "milvus_uri": "",
            "min_rerank_score": 0.0,
            "intent_mode": "auto",
        }
        if not cfg["s3_key"] or not cfg["s3_secret"]:
            raise SystemExit("缺少 MinIO 登录信息。请指定旧项目的实际 .env。")
    if args.chat:
        print("启用后，点击“生成答案”会将问题和候选片段发送至下方服务。")
        oldbase = prev.get("VLM_BASE_URL", "").rstrip("/")
        base = prompt(
            "Chat Completions 基础地址",
            (cfg.get("chat_base") or oldbase or "https://openrouter.ai/api/v1"),
        ).rstrip("/")
        if not base.startswith(("https://", "http://")) or base.endswith(
            "/chat/completions"
        ):
            raise SystemExit("基础地址应包含 http(s)://，以 /v1 等 API 基路径结束。")
        default_model = (
            cfg.get("chat_model", "") if cfg.get("chat_base") == base else ""
        )
        if not default_model and oldbase == base and usable(prev.get("VLM_MODEL")):
            default_model = prev["VLM_MODEL"]
        model = prompt("模型的完整 ID", default_model)
        if not usable(model):
            raise SystemExit("请提供真实可用的模型 ID。")
        reusable = cfg.get("chat_key", "") if cfg.get("chat_base") == base else ""
        if not reusable and oldbase == base and usable(prev.get("VLM_API_KEY")):
            reusable = prev["VLM_API_KEY"]
        key = (
            getpass.getpass(
                "API Key（回车使用同一地址的已保存密钥）: "
                if reusable
                else "API Key（支持免鉴权的本地模型可留空）: "
            )
            or reusable
        )
        if "openrouter.ai" in base and not key:
            raise SystemExit("OpenRouter 需要 API Key，配置尚未写入。")
        cfg.update(chat_base=base, chat_model=model, chat_key=key)
    if args.agent:
        print(
            "内部模型用于自动识别文档字段和用户检索意图；文档片段和用户上下文会发送到该地址。"
        )
        base = prompt(
            "内部 Agent API 基础地址",
            cfg.get("agent_base", "http://host.docker.internal:8001/v1"),
        ).rstrip("/")
        if not base.startswith(("http://", "https://")) or base.endswith(
            "/chat/completions"
        ):
            raise SystemExit(
                "请填写 OpenAI-compatible API 基础地址，不含 /chat/completions。"
            )
        model = prompt(
            "内部模型 ID",
            cfg.get("agent_model", "") if base == cfg.get("agent_base") else "",
        )
        if not model:
            raise SystemExit("模型 ID 不能为空。")
        saved = cfg.get("agent_key", "") if base == cfg.get("agent_base") else ""
        key = (
            getpass.getpass(
                "内部模型 API Key（回车沿用同地址已保存 Key；免鉴权服务可留空）: "
            )
            or saved
        )
        cfg.update(
            agent_base=base, agent_model=model, agent_key=key, intent_mode="auto"
        )
    if args.intent_mode:
        cfg["intent_mode"] = args.intent_mode
    env = read_env(ROOT / ".env")
    network = args.network or env.get("MINIO_NETWORK") or detect_network(oldpath)
    if not network:
        network = prompt(
            "MinIO 容器所在的 Docker 网络（参考手册第 3 步）",
            "mineru-image-homework_default",
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", network):
        raise SystemExit("Docker 网络名无效。")
    port = env.get("AGENT_PORT", "8020")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise SystemExit(".env 中 AGENT_PORT 无效。")
    (ROOT / "state").mkdir(exist_ok=True, mode=0o700)
    save(SETTINGS, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    save(
        ROOT / ".env",
        f"MINIO_NETWORK={network}\nAGENT_PORT={port}\nLOCAL_UID={os.getuid()}\nLOCAL_GID={os.getgid()}\n",
    )
    print(
        f"已配置。Docker 网络 {network}，平台端口 {port}。密钥保存在本目录的 settings.json。"
    )


if __name__ == "__main__":
    main()
