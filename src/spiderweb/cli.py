"""Spiderweb CLI — init, install-skill, and MCP server entry."""
import os
import sys
import shutil
import argparse
from pathlib import Path


def cmd_init(args):
    """Initialize a new domain pack from a template."""
    target = Path(args.target).expanduser().resolve()
    template = args.template or "reading"

    # Find built-in template
    builtin = Path(__file__).parent.parent.parent / "domains" / template
    if not builtin.is_dir():
        print(f"Template '{template}' not found. Available: reading")
        sys.exit(1)

    if target.exists() and not args.force:
        print(f"Target {target} already exists. Use --force to overwrite.")
        sys.exit(1)

    shutil.copytree(builtin, target, dirs_exist_ok=args.force)
    print(f"Domain pack initialized at {target}")
    print(f"Next: edit {target}/config.yaml and {target}/.env.example → .env")
    print(f"Then: spiderweb install-skill --domain {target}")


def cmd_install_skill(args):
    """Install domain SKILL.md to Claude Code skills directory."""
    domain = Path(args.domain).expanduser().resolve()
    skill_md = domain / "SKILL.md"
    if not skill_md.exists():
        print(f"No SKILL.md found in {domain}")
        sys.exit(1)

    # Find Claude Code skills dir
    skills_dir = None
    for base in [Path.home() / ".claude", Path.home() / ".claude-code"]:
        if (base / "skills").is_dir():
            skills_dir = base / "skills"
            break

    if not skills_dir:
        # Check project-level
        cwd = Path.cwd()
        if (cwd / ".claude" / "skills").is_dir():
            skills_dir = cwd / ".claude" / "skills"

    if not skills_dir:
        print("No .claude/skills/ directory found. Create one first.")
        sys.exit(1)

    domain_name = domain.name
    target_dir = skills_dir / f"spiderweb-{domain_name}"
    target_dir.mkdir(exist_ok=True)
    shutil.copy2(skill_md, target_dir / "SKILL.md")
    print(f"Skill installed: {target_dir}/SKILL.md")


def cmd_serve(args):
    """Start MCP stdio server."""
    # Delegate to the MCP server's main
    from .mcp.server import main as mcp_main
    import asyncio

    # Pass domain from args
    if args.domain:
        os.environ["SPIDERWEB_DOMAIN"] = args.domain

    asyncio.run(mcp_main())


def cmd_migrate_reading(args):
    """Migrate reading-graph (old 读书郎) data into spiderweb."""
    from .engine.db import get_db_path
    from .migrate_reading import migrate

    old_db = os.path.expanduser(args.old_db)
    if not os.path.exists(old_db):
        print(f"Old database not found: {old_db}")
        sys.exit(1)

    target = Path(args.target).expanduser().resolve() if args.target else Path.home() / ".spiderweb" / "reading"
    new_db = get_db_path(str(target))
    os.makedirs(os.path.dirname(new_db), exist_ok=True)

    views_dir = args.views or os.path.join(os.path.dirname(old_db), "..", "views")

    print(f"Migrating: {old_db} → {new_db}")
    print(f"Views dir: {views_dir}")

    stats = migrate(old_db, new_db, views_dir)

    for key, val in stats.items():
        print(f"  {key}: {val}")
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Spiderweb — LLM-powered document graph engine")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize a domain pack")
    p_init.add_argument("--target", required=True, help="Target directory")
    p_init.add_argument("--template", default="reading", help="Template domain (default: reading)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing")
    p_init.set_defaults(func=cmd_init)

    p_skill = sub.add_parser("install-skill", help="Install domain SKILL.md to Claude Code")
    p_skill.add_argument("--domain", required=True, help="Domain pack directory")
    p_skill.set_defaults(func=cmd_install_skill)

    p_serve = sub.add_parser("serve", help="Start MCP stdio server")
    p_serve.add_argument("--domain", help="Domain pack directory (or set SPIDERWEB_DOMAIN)")
    p_serve.set_defaults(func=cmd_serve)

    p_migrate = sub.add_parser("migrate-reading", help="Migrate reading-graph data into spiderweb")
    p_migrate.add_argument("--old-db", required=True, help="Path to reading-graph doc_index.db")
    p_migrate.add_argument("--target", help="Target spiderweb domain dir (default: ~/.spiderweb/reading)")
    p_migrate.add_argument("--views", help="Path to views directory (default: auto-detect)")
    p_migrate.set_defaults(func=cmd_migrate_reading)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
