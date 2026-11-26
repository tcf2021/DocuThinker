import base64
import binascii
import json
import mimetypes
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import PurePosixPath

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter


class Problem(Exception):
    """Messages are written for operators and never contain secrets or provider response bodies."""


def safe_error(exc):
    if isinstance(exc, Problem):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"上游 HTTP {exc.response.status_code}。请对照当前阶段检查地址、权限、额度或服务日志。"
    if isinstance(exc, httpx.TimeoutException):
        return "上游超时。请检查对应服务的日志和网络，稍后重试。"
    if isinstance(exc, httpx.RequestError):
        return "无法连接上游。检查容器 DNS、端口和服务监听地址。"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
        return f"{type(exc).__name__} ({code})。请检查当前阶段的服务。"
    return f"{type(exc).__name__}。请检查当前阶段的配置、模型缓存和容器日志。"


def parse_pdf(cfg, path):
    with (
        path.open("rb") as pdf,
        httpx.Client(
            trust_env=False, timeout=httpx.Timeout(1800, connect=10)
        ) as client,
    ):
        res = client.post(
            cfg["mineru_url"].rstrip("/") + "/file_parse",
            files={"files": ("document.pdf", pdf, "application/pdf")},
            data={
                "backend": "pipeline",
                "parse_method": "auto",
                "lang_list": "ch",
                "return_md": "true",
                "return_images": "true",
                "return_content_list": "true",
                "response_format_zip": "false",
            },
        )
        res.raise_for_status()
        return unpack(res.json())


def safe_image(name):
    base = PurePosixPath(name).name
    if not re.fullmatch(r"[\w.-]{1,230}\.(?:jpe?g|png|webp|gif|bmp|tiff?)", base, re.I):
        raise Problem("MinerU 返回了无法识别的图片文件名。")
    return "images/" + base


def unpack(payload):
    results = payload.get("results", {}) if isinstance(payload, dict) else {}
    if not isinstance(results, dict) or len(results) != 1:
        raise Problem(
            "MinerU 响应缺少单文件 results。请核对 3.1.0 的 /file_parse 接口。"
        )
    data = next(iter(results.values()))
    if not isinstance(data, dict):
        raise Problem("MinerU 返回的文档结构无效。")
    md = data.get("md_content") or ""
    blocks = data.get("content_list") or []
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError as exc:
            raise Problem("content_list 不是有效 JSON。") from exc
    if (
        not isinstance(blocks, list)
        or not isinstance(md, str)
        or not all(isinstance(x, dict) for x in blocks)
    ):
        raise Problem("MinerU 的 Markdown 或 content_list 类型不符。")
    if not md.strip() and not blocks:
        raise Problem(
            "MinerU 未返回可用的 Markdown 或结构化内容。请用同一份 PDF 在 8000/docs 测试。"
        )
    imgs = {}
    for key, val in (data.get("images") or {}).items():
        name = safe_image(key)
        try:
            raw = base64.b64decode(val.split(",", 1)[-1], validate=True)
        except (AttributeError, ValueError, binascii.Error) as exc:
            raise Problem("MinerU 返回的图片无法解码。") from exc
        if not raw or name in imgs:
            raise Problem("MinerU 返回了空图片或重复文件名。")
        imgs[name] = raw
    return {"md": md, "blocks": blocks, "images": imgs}


class TableText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []

    def handle_data(self, value):
        self.out.append(value)

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.out.append(" | ")
        elif tag == "tr":
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.out.append(" ")


def block_text(block):
    parts = []
    for key in (
        "text",
        "list_items",
        "image_caption",
        "table_caption",
        "chart_caption",
        "code_caption",
        "table_body",
        "code_body",
        "content",
        "image_footnote",
        "table_footnote",
        "chart_footnote",
        "code_footnote",
    ):
        value = block.get(key)
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str) or not item.strip():
                continue
            if key == "table_body":
                parser = TableText()
                parser.feed(item)
                item = "".join(parser.out).strip()
            parts.append(item.strip())
    return "\n".join(parts)


def split_pages(parsed):
    # Keep page boundaries. Attach images as page-level references, without inventing captions.
    grouped, images = defaultdict(list), defaultdict(list)
    section = ""
    for block in parsed["blocks"]:
        idx = block.get("page_idx")
        page = idx + 1 if isinstance(idx, int) and idx >= 0 else 0
        if block.get("text_level") or block.get("type") == "title":
            section = block_text(block).strip()[:240]
        if block.get("type") not in {"header", "footer", "page_number"}:
            if text := block_text(block):
                grouped[(page, section)].append(text)
        path = block.get("img_path")
        if path and PurePosixPath(path).suffix:
            name = safe_image(path)
            if name in parsed["images"] and name not in images[page]:
                images[page].append(name)
    if not any(grouped.values()):
        md = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", parsed["md"]).strip()
        if md:
            grouped[(0, "")] = [md]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=350,
        chunk_overlap=60,
        separators=["\n\n", "\n", "。", "！", "？", ". ", "；", "，", " ", ""],
    )
    chunks = [
        {"page": page, "section": section, "text": part, "images": images[page]}
        for (page, section), parts in grouped.items()
        for part in splitter.split_text("\n\n".join(parts))
        if part.strip()
    ]
    if not chunks:
        raise Problem("未找到可索引文字。请核对扫描件 OCR、图注和 MinerU 解析结果。")
    return chunks


def mime(name):
    return {
        "source.pdf": "application/pdf",
        "document.md": "text/plain; charset=utf-8",
        "content_list.json": "application/json",
    }.get(name, mimetypes.guess_type(name)[0] or "application/octet-stream")
