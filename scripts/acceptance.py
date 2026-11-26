"""Run from WSL without additional Python packages. Uses a dedicated example knowledge base."""

import argparse
import getpass
import json
import re
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(
        description="通过真实平台接口上传样例，检查就绪、检索、页码及原文。"
    )
    ap.add_argument("--base", default="http://127.0.0.1:8020")
    ap.add_argument("--username", default="cf")
    ap.add_argument("--chat", action="store_true", help="还会付诸一次实际的模型问答")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    token = ""

    def req(path, method="GET", obj=None, content_type=None, raw=False):
        data = (
            json.dumps(obj, ensure_ascii=False).encode()
            if obj is not None and not isinstance(obj, bytes)
            else obj
        )
        headers = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            headers["Content-Type"] = content_type or "application/json"
        try:
            with urlopen(
                Request(base + path, data=data, method=method, headers=headers),
                timeout=300,
            ) as response:
                body = response.read()
            return body if raw else json.loads(body)
        except HTTPError as exc:
            raise SystemExit(
                f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:350]}"
            ) from exc
        except URLError as exc:
            raise SystemExit(
                f"连接失败：{base}；请检查平台端口和 docker compose ps。"
            ) from exc

    login = req(
        "/api/login",
        "POST",
        {"username": args.username, "password": getpass.getpass("平台密码: ")},
    )
    token = login["token"]
    print("1/5 登录成功。")
    name = "松岚验收"
    kb = next((k for k in req("/api/kbs") if k["name"] == name), None)
    if kb is None:
        kb = req("/api/kbs", "POST", {"name": name})
    pre = "/api/kbs/" + kb["id"]
    pdf = (ROOT / "examples/songlan-demo.pdf").read_bytes()
    b = "agent-" + uuid.uuid4().hex
    upload = (
        (
            f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="songlan-demo.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + pdf
        + f"\r\n--{b}--\r\n".encode()
    )
    doc = req(pre + "/documents", "POST", upload, "multipart/form-data; boundary=" + b)
    path = "/api/documents/" + doc["id"]
    print(
        "2/5 文档已登记。", "使用本库已有副本。" if doc["duplicate"] else "进入队列。"
    )
    last = None
    deadline = time.monotonic() + 2400
    while time.monotonic() < deadline:
        state = req(path)
        marker = (state["status"], state["stage"])
        if marker != last:
            print("   ", marker, flush=True)
            last = marker
        if state["status"] == "ready":
            break
        if state["status"] == "failed":
            raise SystemExit(
                "入库失败：" + state["error"] + "。修复后在网页文档详情中点击重试。"
            )
        time.sleep(2)
    else:
        raise SystemExit("等待已超过 40 分钟。请打开网页检查阶段；任务仍由平台管理。")
    print("3/5 就绪。", state["n_chunks"], "个片段；", len(state["files"]), "个文件。")
    for name in ("source.pdf", "document.md", "content_list.json"):
        if name not in state["files"]:
            raise SystemExit("缺少解析文件 " + name)
    if req(path + "/files/source.pdf", raw=True) != pdf:
        raise SystemExit("原 PDF 回读内容不一致。")
    question = "松岚计划的原始 PDF 在 MinIO 保留多少天？"
    result = req(pre + "/ask", "POST", {"question": question, "mode": "search"})
    matching = [
        s
        for s in result["sources"]
        if s["doc"] == doc["id"] and s["page"] == 2 and "45" in s["text"]
    ]
    if not matching:
        raise SystemExit("检索没有命中样例第 2 页的 45 天，请打开解析 JSON 核对。")
    print("4/5 真实检索命中第 2 页的 45 天，且原文件字节一致。")
    if args.chat:
        result = req(pre + "/ask", "POST", {"question": question, "mode": "chat"})
        ok = result["status"] == "answered" and re.search(
            r"45|四十五", result["answer"]
        )
        cited = any(
            s["doc"] == doc["id"] and s["page"] == 2 and "45" in s["quote"]
            for s in result["sources"]
        )
        if not ok or not cited:
            raise SystemExit("模型回答或逐字引文未通过样例核对：" + result["answer"])
        print("5/5 模型答案与第 2 页引文通过。", result["answer"])
    else:
        print("5/5 检索验收完成。配置模型后加 --chat 验收生成。")
    req("/api/logout", "POST")


if __name__ == "__main__":
    main()
