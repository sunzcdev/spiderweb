"""MCP stdio server for Spiderweb — 3-tool interface (discover/record/ingest)."""
import os
import sys
import json
import time
import argparse
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
        Tool(name="discover",
             description="搜一切。summary→图谱脉络，detail→原文详情。",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string", "description": "自然语言问题"},
                 "mode": {"type": "string", "description": "summary-图谱脉络 / detail-原文详情", "enum": ["summary", "detail"]},
             }}),
        Tool(name="record",
             description="记录阅读心得，自动关联到知识图谱。",
             inputSchema={"type": "object", "properties": {
                 "content": {"type": "string", "description": "心得原文"},
                 "source": {"type": "string", "description": "可选，来源文档名"},
             }}),
        Tool(name="ingest",
             description="收录新书。自动解析格式、分块、索引、向量化、建图谱，全部完成后返回。",
             inputSchema={"type": "object", "properties": {
                 "source": {"type": "string", "description": "文件路径"},
             }}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict, context=None):
    config = get_config()
    if os.environ.get("SPIDERWEB_DEBUG") == "1":
        debug_init(config.data_dir)

    debug_call(name, arguments)
    t0 = time.time()

    try:
        if name == "discover":
            result = await _reading.discover(arguments["query"], arguments.get("mode", "summary"))
        elif name == "record":
            result = await _reading.record(
                arguments["content"],
                arguments.get("source"),
            )
        elif name == "ingest":
            result = await _reading.ingest(arguments["source"])
        else:
            result = {"error": f"Unknown tool: {name}"}

        elapsed = (time.time() - t0) * 1000
        debug_result(name, result, elapsed)
        return [TextContent(type="text", text=_json_result(result))]
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        debug_error(name, str(e), elapsed)
        return [TextContent(type="text", text=f"Error: {e}")]


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
