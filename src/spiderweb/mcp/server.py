"""MCP stdio server for Spiderweb — 4-tool interface (think/read/record/ingest)."""
import os
import sys
import json
import time
import argparse
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from ..engine.db import get_db, get_db_path
from ..debug import init as debug_init, tool_call as debug_call, tool_result as debug_result, tool_error as debug_error
from ..engine.domain import load_domain, DomainConfig
from ..service.reading_service import ReadingService

server = Server("spiderweb")
_config: DomainConfig = None
_reading: ReadingService = None


def get_config() -> DomainConfig:
    global _config
    if _config is None:
        domain_path = os.environ.get("SPIDERWEB_DOMAIN", "")
        if not domain_path:
            raise RuntimeError("SPIDERWEB_DOMAIN not set and --domain not provided")
        _config = load_domain(domain_path)
    return _config


def _json_result(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@server.list_tools()
async def list_tools():
    return [
        Tool(name="think",
             description="探索概念关联网络。给出一个概念/人名作为锚点，返回按深度展开的关联路径（每层top3）。停止条件：发现热点连接（足迹深的熟路）。",
             inputSchema={"type": "object", "properties": {
                 "anchor": {"type": "string", "description": "概念锚点，如'妾'、'王阳明'——不是自然语言问句"},
             }, "required": ["anchor"]}),
        Tool(name="read",
             description="阅读原文段落。搜索书库中匹配的原文，返回完整段落+出处。",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string", "description": "搜索词"},
                 "source": {"type": "string", "description": "可选，限定搜索某本书"},
                 "top_n": {"type": "integer", "description": "返回条数，默认3", "minimum": 1, "maximum": 20},
             }, "required": ["query"]}),
        Tool(name="record",
             description="记录阅读心得，自动关联到知识图谱。",
             inputSchema={"type": "object", "properties": {
                 "content": {"type": "string", "description": "心得原文"},
                 "source": {"type": "string", "description": "可选，来源文档名"},
             }, "required": ["content"]}),
        Tool(name="ingest",
             description="收录新书。自动解析格式、分块、索引、向量化、建图谱，全部完成后返回。",
             inputSchema={"type": "object", "properties": {
                 "source": {"type": "string", "description": "文件路径"},
             }, "required": ["source"]}),
    ]


def _validate_and_sanitize_args(name: str, arguments: dict, config: DomainConfig) -> dict | None:
    """Validate and sanitize tool arguments at trust boundary.

    Returns sanitized dict on success, or error dict on failure.
    """
    # Check required fields
    required = {
        "think": ["anchor"],
        "read": ["query"],
        "record": ["content"],
        "ingest": ["source"],
    }

    for field in required.get(name, []):
        if field not in arguments:
            return {"error": f"Missing required parameter: {field}", "error_type": "validation"}

    # Type and value validation
    if name == "read":
        try:
            top_n = int(arguments.get("top_n", 3))
            top_n = max(1, min(top_n, 20))  # clamp to [1, 20]
            arguments = dict(arguments)  # copy to avoid mutating input
            arguments["top_n"] = top_n
        except (ValueError, TypeError):
            return {"error": "top_n must be an integer", "error_type": "validation"}

    # Path validation for ingest
    if name == "ingest":
        source = arguments.get("source", "").strip()
        if not source:
            return {"error": "source cannot be empty", "error_type": "validation"}

        try:
            source_path = Path(source).resolve()
            ingest_root = (Path(config.data_dir) / "books").resolve()

            # Security checks
            if not source_path.is_relative_to(ingest_root):
                return {"error": f"source must be under {ingest_root}", "error_type": "security"}

            if source_path.is_symlink():
                return {"error": "symlinks not allowed", "error_type": "security"}

            if not source_path.is_file():
                return {"error": "source must be a regular file", "error_type": "validation"}

            arguments = dict(arguments)
            arguments["source"] = str(source_path)  # use resolved canonical path
        except (ValueError, OSError) as e:
            return {"error": f"Invalid path: {e}", "error_type": "validation"}

    return arguments


@server.call_tool()
async def call_tool(name: str, arguments: dict, context=None):
    config = get_config()
    if os.environ.get("SPIDERWEB_DEBUG") == "1":
        debug_init(config.data_dir)

    debug_call(name, arguments)
    t0 = time.time()

    try:
        # Validate and sanitize inputs at trust boundary
        validation_result = _validate_and_sanitize_args(name, arguments, config)
        if isinstance(validation_result, dict) and "error" in validation_result:
            result = validation_result
        else:
            # validation_result is the sanitized arguments
            arguments = validation_result or arguments

            if name == "think":
                result = await _reading.think(arguments["anchor"])
            elif name == "read":
                result = await _reading.read(
                    arguments["query"],
                    arguments.get("source"),
                    arguments.get("top_n", 3),
                )
            elif name == "record":
                result = await _reading.record(
                    arguments["content"],
                    arguments.get("source"),
                )
            elif name == "ingest":
                result = await _reading.ingest(arguments["source"])
            else:
                result = {"error": f"Unknown tool: {name}", "error_type": "tool_error"}

        elapsed = (time.time() - t0) * 1000
        debug_result(name, result, elapsed)
        return [TextContent(type="text", text=_json_result(result))]
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        debug_error(name, str(e), elapsed)
        # Return structured error, not raw exception
        error_result = {
            "error": "Internal error",
            "error_type": "internal",
            "detail": str(e)  # Internal detail only
        }
        return [TextContent(type="text", text=_json_result(error_result))]


async def main(domain_path: str | None = None):
    if domain_path:
        os.environ["SPIDERWEB_DOMAIN"] = domain_path

    parser = argparse.ArgumentParser(description="Spiderweb MCP server")
    parser.add_argument("--domain", help="Path to domain pack directory")
    args, _ = parser.parse_known_args()
    if args.domain:
        os.environ["SPIDERWEB_DOMAIN"] = args.domain

    if not os.environ.get("SPIDERWEB_DOMAIN"):
        print("Error: Set SPIDERWEB_DOMAIN env var or use --domain", file=sys.stderr, flush=True)
        sys.exit(1)

    global _config
    _config = load_domain(os.environ["SPIDERWEB_DOMAIN"])

    # Prevent duplicate MCP processes
    pid_file = os.path.join(_config.data_dir, "mcp.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            sys.stderr.write(f"[spiderweb] PID lock: instance already running (PID {old_pid}), this is normal — skipping duplicate start\n")
            sys.exit(0)
        except (OSError, ValueError):
            os.remove(pid_file)
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    # Init debug logging
    if os.environ.get("SPIDERWEB_DEBUG") == "1":
        debug_init(_config.data_dir)
        sys.stderr.write(f"[spiderweb] debug logging to {_config.data_dir}/debug.log\n")

    # Initialize ReadingService
    db_path = get_db_path(_config.data_dir)
    db = get_db(db_path)
    global _reading
    _reading = ReadingService(db, _config)

    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        try:
            os.remove(pid_file)
        except OSError:
            pass
