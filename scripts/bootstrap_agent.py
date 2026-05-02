#!/usr/bin/env python3
"""Bootstrap or upgrade a self-contained Hermes agent workspace.

A workspace is a single directory that contains everything an agent needs:

  <agent_dir>/
    AGENTS.md                     operator-facing notes for this agent
    .env                          symlink → hermes-home/.env (canonical)
                                  Canonical .env lives in hermes-home/ because
                                  Hermes upstream rewrites it via os.replace()
                                  (atomic rename) — a symlink there would be
                                  broken on first write. Symlink inversion puts
                                  the real file where Hermes writes, and a
                                  stable symlink where hmk/systemd read.
    hermes-home/                  HERMES_HOME (Hermes Agent reads this)
      config.yaml                 gateway config
      SOUL.md                     identity / style
      memories/                   MEMORY.md, USER.md (per-turn injections)
      plugins/                    plugins (dialogue-handoff comes bundled)
      skills/                     skills (optional)
    agent-memory/                 durable memory layer
      state/                      ALWAYS-CONTEXT, DIALOGUE-HANDOFF, NOW, ACTIVE-CONTEXT
      plans/, episodes/, index/, evidence/, identity/, library/
      library.db                  FTS5 + embeddings (created on `memoryctl init`)
    wiki/                         optional canonical projection
    scripts/                      tooling (hmk, memoryctl, continuityctl, ...)

After bootstrap, the user is expected to clone hermes-agent into ./app and
create ./venv, then enable the systemd unit. See docs/multi-agent.md.

Modes:
  --with-wiki-templates   Include starter wiki notes (first bootstrap).
  --upgrade               Refresh tooling in an existing v3 workspace.
"""
from __future__ import annotations

import argparse
import filecmp
import os
import re
import shutil
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"
SCRIPTS_DIR = REPO_ROOT / "scripts"
ENV_TEMPLATE = REPO_ROOT / ".env.template"

# Agent name validation: lowercase alphanumeric + dash, first char not a dash.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_name(name: str) -> None:
    """Hard-fail if `name` is not safe as a systemd instance identifier."""
    if not NAME_RE.match(name):
        sys.stderr.write(
            f"ERROR: invalid agent name '{name}'.\n"
            "       Must match ^[a-z0-9][a-z0-9-]*$ (lowercase alphanumeric\n"
            "       plus dash, first char not a dash).\n"
            "       Examples of valid names: hermes-prime, hermes-minecraft-ermitano.\n"
            "       Agent name is used as a systemd instance identifier (%i)\n"
            "       and as the workspace basename.\n"
        )
        sys.exit(2)


def detect_v2_layout(agent_dir: Path) -> bool:
    """v3 requires hermes-home/. v2 workspaces have agent-memory/ but NO hermes-home/."""
    if not agent_dir.exists():
        return False
    has_agent_memory = (agent_dir / "agent-memory").is_dir()
    has_hermes_home = (agent_dir / "hermes-home").is_dir()
    return has_agent_memory and not has_hermes_home


# ---- copy helpers ----------------------------------------------------

def copy_if_missing(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def copy_tree_overwrite(src: Path, dst: Path, preserve_exec: bool = False) -> None:
    """Replace *dst* with *src* atomically.

    WARNING: this destroys everything in *dst*. Use ``copy_tree_merge`` when
    *dst* may contain user-authored files alongside kit-shipped ones (e.g.
    the agent's ``scripts/`` directory).
    """
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    if preserve_exec and dst.is_dir():
        for p in dst.rglob("*"):
            if p.suffix == ".sh" or p.name == "hmk":
                try:
                    os.chmod(p, 0o755)
                except Exception:
                    pass


def copy_tree_merge(
    src: Path,
    dst: Path,
    *,
    preserve_exec: bool = False,
    backup_dir=None,
):
    """Copy *src* into *dst*, overwriting colliding files but preserving any
    files in *dst* that *src* does NOT ship.

    This is the correct semantics for an `--upgrade`: the kit refreshes its
    own tooling without deleting the agent's custom scripts (mc_runtime.py,
    domain-specific helpers, ``.bak`` history, etc.). The previous
    behaviour (``rmtree`` of *dst* before ``copytree``) caused data loss on
    every upgrade.

    If *backup_dir* is provided, the pre-image of every overwritten file is
    archived into ``backup_dir/<rel>`` before being replaced. That makes a
    bad upgrade recoverable from disk without going to off-host backups.

    Returns ``{"copied": N, "overwritten": N, "preserved": N}``.
    """
    if not src.is_dir():
        raise ValueError(f"copy_tree_merge: src must be a directory, got {src}")
    dst.mkdir(parents=True, exist_ok=True)

    counts = {"copied": 0, "overwritten": 0, "preserved": 0}
    src_rels = set()

    for src_path in src.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(src)
        src_rels.add(rel)
        dst_path = dst / rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        if dst_path.exists():
            if backup_dir is not None:
                bak_target = backup_dir / rel
                bak_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst_path, bak_target)
            counts["overwritten"] += 1
        else:
            counts["copied"] += 1

        shutil.copy2(src_path, dst_path)
        if preserve_exec and (dst_path.suffix == ".sh" or dst_path.name == "hmk"):
            try:
                os.chmod(dst_path, 0o755)
            except Exception:
                pass

    for dst_path in dst.rglob("*"):
        if not dst_path.is_file():
            continue
        if dst_path.relative_to(dst) not in src_rels:
            counts["preserved"] += 1

    return counts


def render_template(src: Path, dst: Path, values: dict) -> None:
    """Copy `src` to `dst` replacing `{{KEY}}` tokens with values."""
    text = src.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


# ---- bootstrap -------------------------------------------------------

HERMES_HOME_SUBDIRS = [
    "memories",
    "plugins",
    "plugin-backups",  # see bootstrap_agent.py --upgrade + docs/migration-v3.md
    "skills",
    "sessions",
    "logs",
    "cron",
    "audio_cache",
    "image_cache",
    "pastes",
]

AGENT_MEMORY_SUBDIRS = [
    "state",
    "plans",
    "episodes",
    "index",
    "evidence",
    "identity",
    "library",
]


def bootstrap(agent_dir: Path, name: str, with_wiki_templates: bool) -> None:
    if detect_v2_layout(agent_dir):
        sys.stderr.write(
            f"ERROR: {agent_dir} looks like a v2.x workspace\n"
            "       (has agent-memory/ but no hermes-home/).\n"
            "       Auto-upgrade from v2 to v3 is NOT supported by bootstrap.\n"
            "       See docs/migration-v3.md for the migration playbook.\n"
        )
        sys.exit(2)

    agent_dir.mkdir(parents=True, exist_ok=True)

    values = {
        "AGENT_NAME": name,
        "WORKSPACE_ROOT": str(agent_dir),
        "DEFAULT_MODEL": "nvidia/llama-3.3-nemotron-super-49b-v1",
        "DEFAULT_PROVIDER": "nvidia",
        "USER_PROFILE": "<!-- Describe the primary operator here: name, role, style, domains. -->",
    }

    # AGENTS.md (workspace root)
    copy_if_missing(TEMPLATES / "AGENTS.md", agent_dir / "AGENTS.md")

    # hermes-home/ skeleton
    hh = agent_dir / "hermes-home"
    hh.mkdir(exist_ok=True)
    for sub in HERMES_HOME_SUBDIRS:
        (hh / sub).mkdir(exist_ok=True)

    # Rendered templates in hermes-home/
    if not (hh / "config.yaml").exists():
        render_template(
            TEMPLATES / "hermes-home" / "config.yaml.template",
            hh / "config.yaml",
            values,
        )
    if not (hh / "SOUL.md").exists():
        render_template(
            TEMPLATES / "hermes-home" / "SOUL.md.template",
            hh / "SOUL.md",
            values,
        )
    if not (hh / "memories" / "MEMORY.md").exists():
        render_template(
            TEMPLATES / "hermes-home" / "memories" / "MEMORY.md.template",
            hh / "memories" / "MEMORY.md",
            values,
        )
    if not (hh / "memories" / "USER.md").exists():
        render_template(
            TEMPLATES / "hermes-home" / "memories" / "USER.md.template",
            hh / "memories" / "USER.md",
            values,
        )

    # Plugins (dialogue-handoff + whatever else is under templates/plugins)
    plugins_src = TEMPLATES / "plugins"
    if plugins_src.exists():
        for plugin_dir in plugins_src.iterdir():
            if plugin_dir.is_dir() and not plugin_dir.name.startswith("__"):
                copy_if_missing(plugin_dir, hh / "plugins" / plugin_dir.name)

    # plugin-backups/ README: explain the convention so no one drops .bak
    # sibling directories under hermes-home/plugins/ (which Hermes would
    # load as active plugins, silently overriding the enabled one).
    pb_readme = hh / "plugin-backups" / "README.md"
    if not pb_readme.exists():
        pb_readme.write_text(
            "# plugin-backups/\n\n"
            "Snapshot of plugins rotated out by `bootstrap_agent.py --upgrade`.\n"
            "Hermes does NOT scan this directory, so stale plugin versions here\n"
            "will not register hooks and will not override the live plugin.\n\n"
            "Do NOT place `.bak` copies inside `hermes-home/plugins/` — Hermes\n"
            "loads every directory with a valid `plugin.yaml` under `plugins/`,\n"
            "regardless of `config.yaml: plugins.enabled`. A stale sibling runs\n"
            "its hooks alongside the live one and the last writer wins — silent\n"
            "source of state corruption. See docs/migration-v3.md.\n"
        )

    # Skills copy (template skills into hermes-home/skills/)
    skills_src = TEMPLATES / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if skill_dir.is_dir():
                copy_if_missing(skill_dir, hh / "skills" / skill_dir.name)

    # agent-memory/ skeleton (from templates/memory/)
    am = agent_dir / "agent-memory"
    am.mkdir(exist_ok=True)
    for sub in AGENT_MEMORY_SUBDIRS:
        (am / sub).mkdir(exist_ok=True)

    # State templates (ALWAYS-CONTEXT, NOW, DIALOGUE-HANDOFF placeholders)
    mem_tpl = TEMPLATES / "memory"
    if mem_tpl.exists():
        for rel in ("state/ALWAYS-CONTEXT.md", "state/NOW.md",
                    "episodes/HERMES-LOG.md", "index/INDEX.md",
                    "plans/MEMORY-ARCHITECTURE.md"):
            src = mem_tpl / rel
            if src.exists():
                copy_if_missing(src, am / rel)
        # DIALOGUE-HANDOFF.md starts from template if present, else empty
        handoff_src = mem_tpl / "state" / "DIALOGUE-HANDOFF.md"
        handoff_dst = am / "state" / "DIALOGUE-HANDOFF.md"
        if handoff_src.exists():
            copy_if_missing(handoff_src, handoff_dst)
        if handoff_dst.exists():
            try:
                os.chmod(handoff_dst, 0o600)
            except Exception:
                pass

    # Wiki
    wiki = agent_dir / "wiki"
    wiki.mkdir(exist_ok=True)
    if with_wiki_templates:
        wiki_src = TEMPLATES / "wiki"
        if wiki_src.exists():
            for entry in wiki_src.iterdir():
                copy_if_missing(entry, wiki / entry.name)

    # Scripts (hmk wrapper + memoryctl + etc.)
    copy_if_missing(SCRIPTS_DIR, agent_dir / "scripts")
    for name_exec in ("hmk", "smoke-test.sh", "bootstrap_agent.py",
                      "bootstrap_workspace.py", "memoryctl.py",
                      "continuityctl.py", "ingest_any.py",
                      "export_obsidian.py", "embed_benchmark.py",
                      "embed_clear.py", "embed_verify.py"):
        p = agent_dir / "scripts" / name_exec
        if p.exists():
            try:
                os.chmod(p, 0o755)
            except Exception:
                pass

    # .env: canonical in hermes-home/ (Hermes upstream writes it via os.replace()
    # in config.py:3292/3395/3451; a symlink there would be broken on first write).
    # agent_root/.env is a relative symlink so hmk wrapper and systemd
    # EnvironmentFile= can read via either path.
    hh_env = hh / ".env"
    root_env = agent_dir / ".env"
    if not hh_env.exists():
        render_template(ENV_TEMPLATE, hh_env, values)
        try:
            os.chmod(hh_env, 0o600)
        except Exception:
            pass
    # Symlink agent_root/.env -> hermes-home/.env (relative path for portability)
    if not root_env.exists() and not root_env.is_symlink():
        try:
            os.symlink("hermes-home/.env", root_env)
        except FileExistsError:
            pass
        except Exception as exc:
            print(f"warning: could not create .env symlink at {root_env}: {exc}")



def upgrade(agent_dir: Path) -> None:
    """Refresh tooling in an existing v3 workspace. Preserves user data."""
    if not agent_dir.exists():
        sys.stderr.write(f"ERROR: workspace does not exist: {agent_dir}\n")
        sys.exit(2)

    if detect_v2_layout(agent_dir):
        sys.stderr.write(
            f"ERROR: {agent_dir} is a v2.x workspace. --upgrade does NOT\n"
            "       migrate v2 to v3. See docs/migration-v3.md.\n"
        )
        sys.exit(2)

    # Refresh scripts/ via MERGE — never wipe agent's custom scripts.
    # Pre-images of overwritten files are archived in script-backups/ so a
    # botched upgrade can be reverted without going to off-host backups.
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    script_backups_dir = agent_dir / "script-backups" / f"scripts-merge.{ts}"
    counts = copy_tree_merge(
        SCRIPTS_DIR,
        agent_dir / "scripts",
        preserve_exec=True,
        backup_dir=script_backups_dir,
    )
    if counts["overwritten"] == 0 and counts["copied"] == 0:
        # Nothing the kit ships changed; clean the empty backup dir.
        try:
            shutil.rmtree(script_backups_dir, ignore_errors=True)
            (agent_dir / "script-backups").rmdir()
        except OSError:
            pass
    else:
        print(
            f"  scripts/: copied={counts['copied']} "
            f"overwritten={counts['overwritten']} "
            f"preserved={counts['preserved']} "
            f"(pre-images in script-backups/{script_backups_dir.name}/)"
        )
    for name_exec in ("hmk", "smoke-test.sh"):
        p = agent_dir / "scripts" / name_exec
        if p.exists():
            try:
                os.chmod(p, 0o755)
            except Exception:
                pass

    # Refresh plugins inside hermes-home (not config.yaml / SOUL.md).
    # Rotate any existing plugin dir to hermes-home/plugin-backups/ BEFORE the
    # overwrite. Never leave the old version as a sibling under plugins/: Hermes
    # loads every valid plugin.yaml regardless of `enabled`, so a stale sibling
    # silently runs its hooks and overrides the refreshed plugin.
    import datetime as _dt
    plugins_src = TEMPLATES / "plugins"
    hh_plugins = agent_dir / "hermes-home" / "plugins"
    hh_backups = agent_dir / "hermes-home" / "plugin-backups"
    if plugins_src.exists() and hh_plugins.exists():
        hh_backups.mkdir(exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        for plugin_dir in plugins_src.iterdir():
            if plugin_dir.is_dir() and not plugin_dir.name.startswith("__"):
                target = hh_plugins / plugin_dir.name
                if target.exists():
                    backup = hh_backups / f"{plugin_dir.name}.{ts}.bak"
                    shutil.move(str(target), str(backup))
                    print(f"  rotated {plugin_dir.name} → plugin-backups/{backup.name}")
                copy_tree_overwrite(plugin_dir, target)

    # Refresh skills (same)
    skills_src = TEMPLATES / "skills"
    hh_skills = agent_dir / "hermes-home" / "skills"
    if skills_src.exists() and hh_skills.exists():
        for skill_dir in skills_src.iterdir():
            if skill_dir.is_dir():
                copy_tree_overwrite(skill_dir, hh_skills / skill_dir.name)

    # Refresh .env.template reference (for diffing); keep user's .env intact.
    # The workspace .env.example is a symlink ideally; this is just a sanity touch.
    print(f"upgraded tooling + plugins/skills in: {agent_dir}")
    print("note: config.yaml, SOUL.md, memories/*, .env NOT touched (user data).")


def print_next_steps(agent_dir: Path, name: str) -> None:
    home = str(Path.home())
    expected = Path(home) / "agents" / name
    is_standard = str(agent_dir.resolve()) == str(expected.resolve())

    print()
    print("=" * 60)
    print(f"  Agent '{name}' bootstrapped at:")
    print(f"    {agent_dir}")
    print("=" * 60)
    print()
    print("Next steps:")
    print(f"  cd {agent_dir}")
    print("  # 1. Fill in API keys and platform tokens in .env")
    print("  # 2. Clone hermes-agent upstream + install:")
    print("  git clone https://github.com/NousResearch/hermes-agent.git app")
    print("  python3 -m venv venv && ./venv/bin/pip install -e ./app")
    print("  # 3. Enable the systemd user service:")
    # The kit's systemd template is NOT copied into the agent workspace;
    # emit absolute path so users can cp directly from the kit.
    kit_unit = REPO_ROOT / "templates" / "systemd" / "hermes-gateway@.service"
    print(f"  cp {kit_unit} \\")
    print("     ~/.config/systemd/user/")
    print("  systemctl --user daemon-reload")
    if is_standard:
        print(f"  systemctl --user enable --now hermes-gateway@{name}.service")
    else:
        print(f"  # NOTE: {agent_dir} is OUTSIDE the standard ~/agents/{name}/ location.")
        print("  # The template systemd unit won't find it via %h/agents/%i.")
        print("  # Use a non-template unit with absolute paths instead.")
    print()


def main():
    ap = argparse.ArgumentParser(
        description="Bootstrap or upgrade a self-contained Hermes agent workspace",
    )
    ap.add_argument("agent_dir", help="Agent directory (e.g. ~/agents/hermes-prime)")
    ap.add_argument("--name", help="Agent name (default: basename of agent_dir)")
    ap.add_argument("--with-wiki-templates", action="store_true",
                    help="Copy starter wiki notes into wiki/")
    ap.add_argument("--upgrade", action="store_true",
                    help="Refresh tooling in an existing v3 workspace")
    args = ap.parse_args()

    agent_dir = Path(args.agent_dir).expanduser().resolve()
    name = args.name or agent_dir.name

    validate_name(name)

    if args.upgrade:
        upgrade(agent_dir)
    else:
        bootstrap(agent_dir, name, args.with_wiki_templates)
        print_next_steps(agent_dir, name)


if __name__ == "__main__":
    main()
