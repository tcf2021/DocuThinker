"""Exercise current retrieval against the previously supplied observatory PDFs on the running API."""

import argparse
import getpass
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(
        description="检查真实平台的意图路由和 Metadata 过滤，不调用问答 LLM。"
    )
    parser.add_argument("--base", default="http://127.0.0.1:8020")
    parser.add_argument("--username", default="cf")
    parser.add_argument("--kb", help="已导入天文台分文件 PDF 的知识库名称")
    parser.add_argument("--out", type=Path, default=Path("intent-test-result.json"))
    args = parser.parse_args()
    token = ""

    def req(path, payload=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(
                Request(args.base.rstrip("/") + path, data=data, headers=headers),
                timeout=300,
            ) as response:
                return json.load(response)
        except HTTPError as exc:
            raise SystemExit(
                f"HTTP {exc.code}，请在工作台检查账号、接口及知识库权限。"
            ) from exc
        except URLError as exc:
            raise SystemExit("无法连接平台，请核对 --base 和 Agent 容器。") from exc

    token = req(
        "/api/login",
        {"username": args.username, "password": getpass.getpass("平台密码: ")},
    )["token"]
    try:
        kbs = req("/api/kbs")
        if not args.kb:
            print("可用知识库：")
            for kb in kbs:
                print(" ", kb["name"])
            print('加 --kb "知识库名称" 后重新运行。')
            return
        kb = next((kb for kb in kbs if kb["name"] == args.kb), None)
        if kb is None:
            raise SystemExit("未找到这个名称的知识库。先不带 --kb 运行以列出名称。")
        base = "/api/kbs/" + kb["id"]
        documents = req(base + "/documents")
        if any(d["status"] == "ready" and d["index_version"] != 3 for d in documents):
            raise SystemExit("本知识库仍有 v1 文档，请等待重新索引完成后测试。")
        cases = json.loads((ROOT / "examples/intent-queries.json").read_text())
        rows = []
        for case in cases:
            result = req(
                base + "/ask",
                {
                    "question": case["question"],
                    "filters": case["filters"],
                    "mode": "search",
                    "planner": "rules",
                },
            )
            actual = sorted({r["filename"] for r in result["sources"]})
            expected = case["expected_documents"]
            ok = set(expected) <= set(actual) if expected else not actual
            if case["filters"].get("doc_types"):
                ok = ok and all(
                    r.get("metadata", {}).get("doc_type")
                    in case["filters"]["doc_types"]
                    for r in result["sources"]
                )
            rows.append(
                {**case, "passed": ok, "returned_documents": actual, "result": result}
            )
            print("PASS" if ok else "FAIL", case["question"])
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        print(
            f"{sum(r['passed'] for r in rows)}/{len(rows)} 项检索检查通过。报告：{args.out.resolve()}"
        )
        print("检查的是文档覆盖和过滤；不能据此当作最终答案正确率。")
        if not all(r["passed"] for r in rows):
            raise SystemExit(1)
    finally:
        try:
            req("/api/logout", {})
        except SystemExit:
            pass


if __name__ == "__main__":
    main()
