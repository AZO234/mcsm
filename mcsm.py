#!/usr/bin/env python3
# mcsm.py - Minecraft server & plugin manager (Purpur/Paper)
#
# Design:
# - Run mcsm inside the server directory.
#
# CLI:
#   init <platform>              (optional: writes a commented template for customization)
#   list <platform> [mc_version]
#   install <platform> <mc_version>
#   update
#   setup [--name NAME]          (Linux/Windows/macOS)
#   addsrv [--name NAME]          (Linux: systemd --user, Windows: Startup, macOS: launchd)
#   rmsrv [--name NAME]
#   shortcuts list
#
# Notes:
# - mc_version is ALWAYS the Minecraft game version.
# - install works even if mcsm.toml does not exist (it will be created).
#
#
# Requirements:
# - Python 3.11+: tomllib
# - Python 3.10 : pip install tomli

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# =========================================================
# Embedded templates
# =========================================================
MC_SERVER_SH_TEMPLATE = """#!/bin/sh
set -eu

WORKDIR="{{WORKDIR}}"
JAR="{{JAR}}"
XMX="{{XMX}}"
XMS="{{XMS}}"
EXTRA_ARGS="{{EXTRA_ARGS}}"

cd "$WORKDIR"
exec java -Xmx${XMX} -Xms${XMS} -jar "$JAR" $EXTRA_ARGS
"""

MC_SERVER_DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=Minecraft Server {{DISPLAY_NAME}}
GenericName=Minecraft Server
Comment=Minecraft Server {{DISPLAY_NAME}}
Exec={{EXEC_PATH}}
Terminal=true
Categories=Game;Server;
"""

MC_SERVER_BAT_TEMPLATE = r"""@echo off
cd /d "{{WORKDIR}}"
java -Xmx{{XMX}} -Xms{{XMS}} -jar "{{JAR}}" {{EXTRA_ARGS}}
"""

MC_SERVER_COMMAND_TEMPLATE = """#!/bin/sh
set -eu
exec "{{EXEC_PATH}}"
"""

MC_SERVER_SYSTEMD_SERVICE_TEMPLATE = """[Unit]
Description=Minecraft Server {{DISPLAY_NAME}}
After=network.target

[Service]
Type=simple
WorkingDirectory={{WORKDIR}}
ExecStart={{EXEC_PATH}}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

MC_SERVER_LAUNCHD_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{{LABEL}}</string>

    <key>ProgramArguments</key>
    <array>
      <string>{{EXEC_PATH}}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{{WORKDIR}}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{{WORKDIR}}/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>{{WORKDIR}}/logs/launchd.err.log</string>
  </dict>
</plist>
"""

# =========================================================
# TOML reader:
# - Python 3.11+: tomllib
# - Python 3.10  : tomli
# =========================================================
try:
  import tomllib  # py3.11+
except Exception:  # pragma: no cover
  tomllib = None  # type: ignore

try:
  import tomli  # type: ignore
except Exception:  # pragma: no cover
  tomli = None  # type: ignore


# =========================================================
# Minimal TOML writer for state.toml (tool-owned)
# =========================================================
def _toml_escape(s: str) -> str:
  return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(v: Any) -> str:
  if isinstance(v, bool):
    return "true" if v else "false"
  if isinstance(v, int):
    return str(v)
  if v is None:
    return '""'
  if isinstance(v, str):
    return f"\"{_toml_escape(v)}\""
  return f"\"{_toml_escape(json.dumps(v, ensure_ascii=False))}\""


def toml_dump_simple(d: Dict[str, Any]) -> str:
  lines: List[str] = []

  def emit_table(path: List[str], obj: Dict[str, Any]) -> None:
    if path:
      lines.append("")
      lines.append("[" + ".".join(path) + "]")
    for k, v in obj.items():
      if isinstance(v, dict):
        continue
      lines.append(f"{k} = {_toml_value(v)}")
    for k, v in obj.items():
      if isinstance(v, dict):
        emit_table(path + [k], v)

  emit_table([], d)
  return "\n".join(lines).lstrip("\n") + "\n"


# =========================================================
# Friendly output (emoji disabled on Windows by default)
# =========================================================
def emojis_enabled() -> bool:
  return os.name != "nt"


def tag(plain: str, emoji: str) -> str:
  return (emoji + " " + plain) if emojis_enabled() else plain


def info(msg: str) -> None:
  print(tag(msg, "ℹ️"))


def ok(msg: str) -> None:
  print(tag(msg, "✅"))


def warn(msg: str) -> None:
  print(tag(msg, "⚠️"))


def step(msg: str) -> None:
  print(tag(msg, "➡️"))


def down(msg: str) -> None:
  print(tag(msg, "⬇️"))


def err(msg: str) -> None:
  if emojis_enabled():
    print("❌ " + msg, file=sys.stderr)
  else:
    print("ERROR: " + msg, file=sys.stderr)


# =========================================================
# Paths / utils
# =========================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_warned_dest_dir_ignored = False


def ensure_dir(p: str) -> None:
  os.makedirs(p, exist_ok=True)


def now_iso_jst() -> str:
  return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()


def sha256_file(path: str) -> str:
  h = hashlib.sha256()
  with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()


def relpath_from_dest(dest_dir: str, abs_path: str) -> str:
  try:
    return os.path.relpath(abs_path, dest_dir)
  except Exception:
    return os.path.basename(abs_path)


def make_safe_name(s: str) -> str:
  s = s.strip().lower()
  s = re.sub(r"[^a-z0-9._-]+", "-", s)
  s = re.sub(r"-{2,}", "-", s).strip("-")
  return s or "mcserver"


def run_cmd(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
  return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def which(cmd: str) -> Optional[str]:
  for p in os.environ.get("PATH", "").split(os.pathsep):
    c = os.path.join(p, cmd)
    if os.path.isfile(c) and os.access(c, os.X_OK):
      return c
  return None

def is_macos() -> bool:
  return sys.platform == "darwin"



def config_dir_from_path(config_path: str) -> str:
  return os.path.abspath(os.path.dirname(os.path.abspath(config_path)))


# =========================================================
# Placeholder renderer
# =========================================================
def render_placeholders(text: str, mapping: Dict[str, str]) -> str:
  out = text
  for k, v in mapping.items():
    out = out.replace("{{" + k + "}}", v)
  return out


# =========================================================
# HTTP helpers
# =========================================================
def http_get_json(url: str, user_agent: str) -> Any:
  req = urllib.request.Request(url, headers={"User-Agent": user_agent})
  with urllib.request.urlopen(req, timeout=30) as r:
    data = r.read().decode("utf-8")
  return json.loads(data)


def http_download(url: str, out_path: str, user_agent: str) -> None:
  ensure_dir(os.path.dirname(out_path))
  req = urllib.request.Request(url, headers={"User-Agent": user_agent})
  with urllib.request.urlopen(req, timeout=180) as r:
    with open(out_path, "wb") as f:
      shutil.copyfileobj(r, f)


def join_url(base: str, params: Dict[str, str]) -> str:
  return base + "?" + urllib.parse.urlencode(params, doseq=True)


# =========================================================
# TOML load
# =========================================================
def load_toml(path: str) -> Dict[str, Any]:
  with open(path, "rb") as f:
    if tomllib is not None:
      return tomllib.load(f)
    if tomli is not None:
      return tomli.load(f)
    raise SystemExit("ERROR: TOML reader not available. Use Python 3.11+ or `pip install tomli` on 3.10.")


# =========================================================
# APIs: Server
# =========================================================
def purpur_latest_mc_version(user_agent: str) -> str:
  j = http_get_json("https://api.purpurmc.org/v2/purpur/", user_agent)
  versions = j.get("versions", [])
  if not versions:
    raise RuntimeError("Purpur versions is empty")
  return versions[-1]


def purpur_latest_build(mc_ver: str, user_agent: str) -> int:
  j = http_get_json(f"https://api.purpurmc.org/v2/purpur/{mc_ver}", user_agent)
  return int(j["builds"]["latest"])


def purpur_download_url(mc_ver: str) -> str:
  return f"https://api.purpurmc.org/v2/purpur/{mc_ver}/latest/download"


# PaperMC Fill v3 (preferred)
def fill_v3_project_versions(project: str, user_agent: str) -> List[str]:
  j = http_get_json(f"https://fill.papermc.io/v3/projects/{project}", user_agent)
  versions_obj = j.get("versions", {})
  versions: List[str] = []
  if isinstance(versions_obj, dict):
    for _, arr in versions_obj.items():
      if isinstance(arr, list):
        for v in arr:
          if isinstance(v, str):
            versions.append(v)

  def key(s: str) -> List[Any]:
    parts: List[Any] = []
    for x in re.split(r"[.\-+_]", s):
      if x.isdigit():
        parts.append(int(x))
      else:
        parts.append(x)
    return parts

  return sorted(set(versions), key=key, reverse=True)


def fill_v3_builds(project: str, version: str, user_agent: str) -> List[Dict[str, Any]]:
  j = http_get_json(f"https://fill.papermc.io/v3/projects/{project}/versions/{version}/builds", user_agent)
  if isinstance(j, list):
    return j
  if isinstance(j, dict) and j.get("ok") is False:
    raise RuntimeError(f"Fill v3 error: {j.get('message', 'unknown')}")
  raise RuntimeError("Fill v3: unexpected builds response")


def fill_v3_latest_stable_download(project: str, version: str, user_agent: str) -> Tuple[str, str]:
  builds = fill_v3_builds(project, version, user_agent)
  stable = [b for b in builds if isinstance(b, dict) and b.get("channel") == "STABLE"]
  chosen = stable[0] if stable else (builds[0] if builds else None)
  if not chosen:
    raise RuntimeError("Fill v3: no builds")
  build_id = str(chosen.get("id", ""))
  downloads = chosen.get("downloads", {})
  d = downloads.get("server:default", {}) if isinstance(downloads, dict) else {}
  url = str(d.get("url", "")) if isinstance(d, dict) else ""
  if not url:
    raise RuntimeError("Fill v3: download URL missing (server:default)")
  return build_id, url


# PaperMC v2 fallback
def papermc_v2_latest_version(project: str, user_agent: str) -> str:
  j = http_get_json(f"https://api.papermc.io/v2/projects/{project}", user_agent)
  versions = j.get("versions", [])
  if not versions:
    raise RuntimeError(f"PaperMC v2 versions is empty for project={project}")
  return versions[-1]


def papermc_v2_latest_build(project: str, version: str, user_agent: str) -> int:
  j = http_get_json(f"https://api.papermc.io/v2/projects/{project}/versions/{version}", user_agent)
  builds = j.get("builds", [])
  if not builds:
    raise RuntimeError(f"PaperMC v2 builds is empty for project={project} version={version}")
  return int(builds[-1])


def papermc_v2_download_url(project: str, version: str, build: int) -> str:
  return f"https://api.papermc.io/v2/projects/{project}/versions/{version}/builds/{build}/downloads/{project}-{version}-{build}.jar"


# =========================================================
# APIs: Plugins
# =========================================================
def geyser_latest_version(project: str, user_agent: str) -> str:
  j = http_get_json(f"https://download.geysermc.org/v2/projects/{project}/versions/latest", user_agent)
  return str(j.get("version", "")) or "(unknown)"


def geyser_download_url(project: str, platform: str) -> str:
  return f"https://download.geysermc.org/v2/projects/{project}/versions/latest/builds/latest/downloads/{platform}"


def modrinth_latest_for_mc(slug: str, loaders: List[str], game_ver: str, user_agent: str) -> Tuple[str, str]:
  base = f"https://api.modrinth.com/v2/project/{slug}/version"
  url = join_url(base, {
    "loaders": json.dumps(loaders, ensure_ascii=False),
    "game_versions": json.dumps([game_ver], ensure_ascii=False),
  })
  arr = http_get_json(url, user_agent)
  if not arr:
    return "(not found)", ""
  v = arr[0]
  version_number = str(v.get("version_number", "(unknown)"))
  files = v.get("files", [])
  file_url = str(files[0].get("url", "")) if files else ""
  return version_number, file_url


# =========================================================
# Planning models
# =========================================================
@dataclass
class ServerPlan:
  platform: str
  mc_version: str
  server_label: str
  url: str


@dataclass
class TargetPlan:
  name: str
  ttype: str
  latest: str
  url: str
  out: str


# =========================================================
# Template generation (PLACEHOLDER)
# =========================================================
def template_text(platform: str) -> str:
  if platform not in ("purpur", "paper"):
    raise SystemExit(f"ERROR: unknown platform for template: {platform}")

  header = """# =========================================================
# mcsm.toml - Minecraft server & plugin manager config
#
# How mcsm decides the installation directory:
#   - mcsm always installs into the directory that contains this mcsm.toml.
#   - Run mcsm inside your server directory.
#
# PLACEHOLDER format:
#   - PLACEHOLDER_MC_VERSION
#   - PLACEHOLDER_USER_AGENT
#   - PLACEHOLDER_SERVERNAME
#
# Notes:
# - install will set mc_version by editing this file while preserving comments.
# =========================================================

schema = 1
mc_version = "PLACEHOLDER_MC_VERSION"
user_agent = "PLACEHOLDER_USER_AGENT"

# (Deprecated) dest_dir is ignored. mcsm uses the mcsm.toml directory.
#dest_dir = "PLACEHOLDER_DEST_DIR"

"""

  server_block = f"""[server]
type = "{platform}"
name = "PLACEHOLDER_SERVERNAME"
jar_out = "server.jar"
keep_versioned_jar = true

[server.jvm]
xmx = "1024M"
xms = "1024M"
# extra_args will be appended after "-jar <server.jar>"
#extra_args = ["nogui"]

default_targets = ["viaversion", "geyser", "floodgate"]

"""

  targets_core = """[targets.viaversion]
type = "modrinth"
slug = "viaversion"
loaders = ["paper", "purpur", "spigot", "bukkit"]
out = "plugins/ViaVersion.jar"

[targets.geyser]
type = "geyser"
project = "geyser"
platform = "spigot"
out = "plugins/Geyser-spigot.jar"

[targets.floodgate]
type = "geyser"
project = "floodgate"
platform = "spigot"
out = "plugins/Floodgate-spigot.jar"

"""

  targets_examples = """# ---------------------------------------------------------
# Other plugin examples (commented out)
# ---------------------------------------------------------

#[targets.discordsrv]
#type = "modrinth"
#slug = "discordsrv"
#loaders = ["paper", "purpur", "spigot", "bukkit"]
#out = "plugins/DiscordSRV.jar"

#[targets.fawe]
#type = "modrinth"
#slug = "fastasyncworldedit"
#loaders = ["paper", "purpur", "spigot", "bukkit"]
#out = "plugins/FastAsyncWorldEdit.jar"

#[targets.mapmodcompanion]
#type = "modrinth"
#slug = "mapmodcompanion"
#loaders = ["paper", "purpur", "spigot", "bukkit"]
#out = "plugins/MapModCompanion.jar"

#[targets.voicechat]
#type = "modrinth"
#slug = "simple-voice-chat"
#loaders = ["paper", "purpur", "spigot", "bukkit"]
#out = "plugins/voicechat.jar"
"""
  return header + server_block + targets_core + targets_examples


def default_config_text(platform: str, mc_version: str) -> str:
  txt = template_text(platform)
  txt = re.sub(r'^\s*mc_version\s*=\s*".*"\s*$', f'mc_version = "{mc_version}"', txt, flags=re.M)
  return txt


def patch_config_text(path: str, platform: str, mc_version: str) -> None:
  txt = Path(path).read_text(encoding="utf-8")

  txt, n1 = re.subn(r'^\s*mc_version\s*=\s*".*"\s*$', f'mc_version = "{mc_version}"', txt, flags=re.M)
  if n1 == 0:
    txt = re.sub(r'^\s*schema\s*=\s*\d+\s*$',
                 lambda m: m.group(0) + f'\nmc_version = "{mc_version}"',
                 txt, flags=re.M)

  def patch_server_type(all_text: str) -> str:
    m = re.search(r'^\[server\]\s*$(.*?)(^\[|\Z)', all_text, flags=re.M | re.S)
    if not m:
      return all_text
    block = m.group(1)
    new_block, n = re.subn(r'^\s*type\s*=\s*".*"\s*$', f'type = "{platform}"', block, flags=re.M)
    if n == 0:
      new_block = f'type = "{platform}"\n' + block.lstrip("\n")
    return all_text[:m.start(1)] + new_block + all_text[m.end(1):]

  txt = patch_server_type(txt)
  Path(path).write_text(txt, encoding="utf-8")


# =========================================================
# State (tool-owned)
# =========================================================
def state_path(dest_dir: str) -> str:
  return os.path.join(dest_dir, "state.toml")


def load_state(dest_dir: str) -> Dict[str, Any]:
  p = state_path(dest_dir)
  if not os.path.exists(p):
    return {}
  return load_toml(p)


def save_state(dest_dir: str, state: Dict[str, Any]) -> None:
  p = state_path(dest_dir)
  ensure_dir(os.path.dirname(p))
  Path(p).write_text(toml_dump_simple(state), encoding="utf-8")


def state_set_server(state: Dict[str, Any], platform: str, mc_version: str, server_label: str, url: str, sha256: str) -> None:
  state["schema"] = 1
  state.setdefault("installed", {})
  state["installed"].setdefault("server", {})
  state["installed"]["server"].update({
    "type": platform,
    "mc_version": mc_version,
    "server_version": server_label,
    "url": url,
    "sha256": sha256,
    "installed_at": now_iso_jst(),
  })


def state_set_target(state: Dict[str, Any], name: str, ttype: str, resolved: str, url: str, out: str, sha256: str) -> None:
  state["schema"] = 1
  state.setdefault("installed", {})
  state["installed"].setdefault("targets", {})
  state["installed"]["targets"].setdefault(name, {})
  state["installed"]["targets"][name].update({
    "type": ttype,
    "resolved_version": resolved,
    "url": url,
    "out": out,
    "sha256": sha256,
    "installed_at": now_iso_jst(),
  })


# =========================================================
# Backup (simple .bak/<id>/...)
# =========================================================
def bak_root(dest_dir: str) -> str:
  return os.path.join(dest_dir, ".bak")


def new_backup_id() -> str:
  return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_move(dest_dir: str, backup_id: str, abs_path: str) -> Optional[str]:
  if not (os.path.exists(abs_path) or os.path.islink(abs_path)):
    return None
  rel = relpath_from_dest(dest_dir, abs_path)
  dst = os.path.join(bak_root(dest_dir), backup_id, rel)
  ensure_dir(os.path.dirname(dst))
  if os.path.exists(dst) or os.path.islink(dst):
    if os.path.isdir(dst):
      shutil.rmtree(dst)
    else:
      os.remove(dst)
  shutil.move(abs_path, dst)
  return rel


# =========================================================
# Parse config (mcsm.toml)
# =========================================================
def get_user_agent(cfg: Dict[str, Any]) -> str:
  ua = str(cfg.get("user_agent", "")).strip()
  if not ua or "PLACEHOLDER" in ua or "you@example.com" in ua:
    warn("user_agent is not set (or placeholder). Please set user_agent in mcsm.toml for better API compatibility.")
    if not ua:
      ua = "mcsm/1.0 (you@example.com)"
  return ua


def get_dest_dir(cfg: Dict[str, Any], config_path: str) -> str:
  global _warned_dest_dir_ignored
  if "dest_dir" in cfg and not _warned_dest_dir_ignored:
    warn("dest_dir in mcsm.toml is ignored. mcsm installs into the mcsm.toml directory.")
    _warned_dest_dir_ignored = True
  return config_dir_from_path(config_path)


def get_mc_version(cfg: Dict[str, Any]) -> str:
  v = str(cfg.get("mc_version", "")).strip()
  if not v or v.startswith("PLACEHOLDER"):
    raise SystemExit('ERROR: mc_version is not set. Run: install <platform> <mc_version>')
  return v


def get_server_platform(cfg: Dict[str, Any]) -> str:
  s = cfg.get("server", {})
  if not isinstance(s, dict):
    raise SystemExit("ERROR: [server] section is required")
  t = str(s.get("type", "")).strip()
  if t not in ("purpur", "paper"):
    raise SystemExit(f"ERROR: unsupported server.type={t}")
  return t


def get_server_name(cfg: Dict[str, Any]) -> str:
  s = cfg.get("server", {})
  if not isinstance(s, dict):
    return "server"
  name = str(s.get("name", "server")).strip()
  return name or "server"


def get_server_jar_out(cfg: Dict[str, Any]) -> str:
  s = cfg.get("server", {})
  if not isinstance(s, dict):
    return "server.jar"
  return str(s.get("jar_out", "server.jar")).strip() or "server.jar"


def get_keep_versioned_jar(cfg: Dict[str, Any]) -> bool:
  s = cfg.get("server", {})
  if not isinstance(s, dict):
    return True
  return bool(s.get("keep_versioned_jar", True))


def get_jvm(cfg: Dict[str, Any]) -> Tuple[str, str, List[str]]:
  s = cfg.get("server", {})
  if not isinstance(s, dict):
    return "1024M", "1024M", ["nogui"]
  j = s.get("jvm", {})
  if not isinstance(j, dict):
    return "1024M", "1024M", ["nogui"]

  xmx = str(j.get("xmx", "1024M")).strip() or "1024M"
  xms = str(j.get("xms", "1024M")).strip() or "1024M"
  extra_args = j.get("extra_args", ["nogui"])
  if not isinstance(extra_args, list):
    extra_args = ["nogui"]
  extra = [str(a) for a in extra_args if str(a).strip()]
  if not extra:
    extra = ["nogui"]
  return xmx, xms, extra


def get_default_targets(cfg: Dict[str, Any]) -> List[str]:
  arr = cfg.get("default_targets", [])
  if not isinstance(arr, list):
    return []
  out: List[str] = []
  for x in arr:
    if isinstance(x, str) and x.strip():
      out.append(x.strip())
  return out


def get_targets(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
  t = cfg.get("targets", {})
  return t if isinstance(t, dict) else {}


def select_targets(cfg: Dict[str, Any], names: Optional[List[str]] = None) -> List[str]:
  tmap = get_targets(cfg)
  if names:
    for n in names:
      if n not in tmap:
        raise SystemExit(f"ERROR: unknown target: {n}")
    return names
  default = get_default_targets(cfg)
  out: List[str] = []
  for n in default:
    td = tmap.get(n)
    if not isinstance(td, dict):
      continue
    enabled = bool(td.get("enabled", True))
    if enabled:
      out.append(n)
  return out


# =========================================================
# Icon mapping (Linux)
# =========================================================
def icon_id_for_platform(platform: str) -> str:
  return "papermc" if platform == "paper" else "purpur"


def bundled_svg_for_platform(platform: str) -> str:
  return os.path.join("images", icon_id_for_platform(platform) + ".svg")


# =========================================================
# Planning: latest server + targets for a given platform/mc_version
# =========================================================
def resolve_server_plan(platform: str, mc_version: str, user_agent: str) -> ServerPlan:
  if platform == "purpur":
    build = purpur_latest_build(mc_version, user_agent)
    label = f"{mc_version}-{build}"
    return ServerPlan(platform=platform, mc_version=mc_version, server_label=label, url=purpur_download_url(mc_version))

  if platform == "paper":
    try:
      build_id, url = fill_v3_latest_stable_download("paper", mc_version, user_agent)
      label = f"{mc_version}-{build_id}"
      return ServerPlan(platform=platform, mc_version=mc_version, server_label=label, url=url)
    except Exception:
      b = papermc_v2_latest_build("paper", mc_version, user_agent)
      label = f"{mc_version}-{b}"
      return ServerPlan(platform=platform, mc_version=mc_version, server_label=label, url=papermc_v2_download_url("paper", mc_version, b))

  raise SystemExit(f"ERROR: unsupported platform: {platform}")


def resolve_target_plan(name: str, td: Dict[str, Any], mc_version: str, user_agent: str) -> TargetPlan:
  ttype = str(td.get("type", "")).strip()
  out = str(td.get("out", "")).strip()
  if not out:
    raise SystemExit(f"ERROR: target '{name}' missing 'out'")

  if ttype == "modrinth":
    slug = str(td.get("slug", "")).strip()
    loaders = td.get("loaders", [])
    if not slug or not isinstance(loaders, list) or not loaders:
      raise SystemExit(f"ERROR: target '{name}' invalid modrinth config (slug/loaders)")
    loaders2 = [str(x) for x in loaders if str(x).strip()]
    latest, url = modrinth_latest_for_mc(slug, loaders2, mc_version, user_agent)
    return TargetPlan(name=name, ttype=ttype, latest=latest, url=url, out=out)

  if ttype == "geyser":
    project = str(td.get("project", "")).strip()
    platform = str(td.get("platform", "")).strip()
    if project not in ("geyser", "floodgate") or not platform:
      raise SystemExit(f"ERROR: target '{name}' invalid geyser config (project/platform)")
    latest = geyser_latest_version(project, user_agent)
    url = geyser_download_url(project, platform)
    return TargetPlan(name=name, ttype=ttype, latest=latest, url=url, out=out)

  raise SystemExit(f"ERROR: target '{name}' unsupported type: {ttype}")


# =========================================================
# Shortcuts / services helpers
# =========================================================
def default_shortcut_name(server_name: str, mc_version: str) -> str:
  return f"{server_name}-{mc_version}"


def win_appdata_dir() -> str:
  appdata = os.environ.get("APPDATA")
  if not appdata:
    raise SystemExit("ERROR: APPDATA is not set.")
  return appdata


def win_start_menu_programs() -> str:
  return os.path.join(win_appdata_dir(), r"Microsoft\Windows\Start Menu\Programs")


def win_startup_dir() -> str:
  return os.path.join(win_start_menu_programs(), r"Startup")


def make_windows_bat(workdir: str, jar_name: str, xmx: str, xms: str, extra_args: List[str]) -> str:
  return render_placeholders(MC_SERVER_BAT_TEMPLATE, {
    "WORKDIR": workdir,
    "JAR": jar_name,
    "XMX": xmx,
    "XMS": xms,
    "EXTRA_ARGS": " ".join(extra_args),
  })


def systemd_user_service_text(display_name: str, exec_path: str, workdir: str) -> str:
  return render_placeholders(MC_SERVER_SYSTEMD_SERVICE_TEMPLATE, {
    "DISPLAY_NAME": display_name,
    "EXEC_PATH": exec_path,
    "WORKDIR": workdir,
  })



def linux_shortcut_paths() -> Tuple[str, str, str]:
  home = os.path.expanduser("~")
  bin_dir = os.path.join(home, ".local", "bin")
  apps_dir = os.path.join(home, ".local", "share", "applications")
  service_dir = os.path.join(home, ".config", "systemd", "user")
  return bin_dir, apps_dir, service_dir

def mac_shortcut_paths() -> Tuple[str, str, str]:
  home = os.path.expanduser("~")
  bin_dir = os.path.join(home, ".local", "bin")
  apps_dir = os.path.join(home, "Applications")
  agents_dir = os.path.join(home, "Library", "LaunchAgents")
  return bin_dir, apps_dir, agents_dir


def mac_command_wrapper_text(exec_path: str) -> str:
  # .command is executed by Terminal on double click
  return render_placeholders(MC_SERVER_COMMAND_TEMPLATE, {
    "EXEC_PATH": exec_path,
  })


def mac_launch_agent_label(safe_id: str) -> str:
  return f"mcsm.mcserver.{safe_id}"


def mac_launch_agent_plist_text(label: str, exec_path: str, workdir: str) -> str:
  return render_placeholders(MC_SERVER_LAUNCHD_PLIST_TEMPLATE, {
    "LABEL": label,
    "EXEC_PATH": exec_path,
    "WORKDIR": workdir,
  })




def expected_shortcut_ids_from_config(cfg: Dict[str, Any], config_path: str, override_name: Optional[str]) -> Tuple[str, str]:
  mc_version = get_mc_version(cfg)
  server_name = get_server_name(cfg)
  display = override_name or default_shortcut_name(server_name, mc_version)
  safe = make_safe_name(display)
  return display, safe


# =========================================================
# Shortcuts / services commands
# =========================================================

# =========================================================
# OS backends: setup / addsrv / rmsrv
# =========================================================
def setup_backend_windows(dest_dir: str, safe: str, jar_out: str, xmx: str, xms: str, extra_args: List[str]) -> str:
  programs = win_start_menu_programs()
  ensure_dir(programs)
  bat_name = f"mcserver-{safe}.bat"
  bat_path = os.path.join(programs, bat_name)
  Path(bat_path).write_text(make_windows_bat(dest_dir, jar_out, xmx, xms, extra_args), encoding="utf-8")
  return bat_path


def setup_backend_macos(dest_dir: str, safe: str, jar_out: str, xmx: str, xms: str, extra_args: List[str]) -> Tuple[str, str]:
  bin_dir, apps_dir, _agents_dir = mac_shortcut_paths()

  script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
  command_path = os.path.join(apps_dir, f"mcserver-{safe}.command")

  sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
    "WORKDIR": dest_dir,
    "JAR": jar_out,
    "XMX": xmx,
    "XMS": xms,
    "EXTRA_ARGS": " ".join(extra_args),
  })
  ensure_dir(bin_dir)
  Path(script_path).write_text(sh_text, encoding="utf-8")
  os.chmod(script_path, 0o755)

  ensure_dir(apps_dir)
  Path(command_path).write_text(mac_command_wrapper_text(script_path), encoding="utf-8")
  os.chmod(command_path, 0o755)

  return script_path, command_path


def setup_backend_linux(dest_dir: str, display: str, safe: str, jar_out: str, xmx: str, xms: str, extra_args: List[str]) -> Tuple[str, str]:
  bin_dir, apps_dir, _service_dir = linux_shortcut_paths()

  script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
  desktop_path = os.path.join(apps_dir, f"mcserver-{safe}.desktop")

  sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
    "WORKDIR": dest_dir,
    "JAR": jar_out,
    "XMX": xmx,
    "XMS": xms,
    "EXTRA_ARGS": " ".join(extra_args),
  })
  ensure_dir(bin_dir)
  Path(script_path).write_text(sh_text, encoding="utf-8")
  os.chmod(script_path, 0o755)

  desktop_text = render_placeholders(MC_SERVER_DESKTOP_TEMPLATE, {
    "DISPLAY_NAME": display,
    "EXEC_PATH": script_path,
  })
  ensure_dir(apps_dir)
  Path(desktop_path).write_text(desktop_text, encoding="utf-8")

  return script_path, desktop_path


def addsrv_backend_windows(dest_dir: str, safe: str, jar_out: str, xmx: str, xms: str, extra_args: List[str]) -> str:
  programs = win_start_menu_programs()
  startup = win_startup_dir()
  ensure_dir(programs)
  ensure_dir(startup)

  bat_name = f"mcserver-{safe}.bat"
  src = os.path.join(programs, bat_name)
  if not os.path.exists(src):
    Path(src).write_text(make_windows_bat(dest_dir, jar_out, xmx, xms, extra_args), encoding="utf-8")

  dst = os.path.join(startup, bat_name)
  shutil.copy2(src, dst)
  return dst


def addsrv_backend_macos(dest_dir: str, safe: str, jar_out: str, xmx: str, xms: str, extra_args: List[str]) -> str:
  bin_dir, _apps_dir, agents_dir = mac_shortcut_paths()

  script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
  if not os.path.exists(script_path):
    sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
      "WORKDIR": dest_dir,
      "JAR": jar_out,
      "XMX": xmx,
      "XMS": xms,
      "EXTRA_ARGS": " ".join(extra_args),
    })
    ensure_dir(bin_dir)
    Path(script_path).write_text(sh_text, encoding="utf-8")
    os.chmod(script_path, 0o755)

  label = mac_launch_agent_label(safe)
  plist_path = os.path.join(agents_dir, f"{label}.plist")

  ensure_dir(os.path.join(dest_dir, "logs"))
  ensure_dir(agents_dir)
  Path(plist_path).write_text(mac_launch_agent_plist_text(label, script_path, dest_dir), encoding="utf-8")

  uid = os.getuid()
  run_cmd(["launchctl", "bootstrap", f"gui/{uid}", plist_path], check=False)
  run_cmd(["launchctl", "enable", f"gui/{uid}/{label}"], check=False)
  run_cmd(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)

  return label


def addsrv_backend_linux(dest_dir: str, display: str, safe: str, jar_out: str, xmx: str, xms: str, extra_args: List[str]) -> str:
  if which("systemctl") is None:
    raise SystemExit("ERROR: systemctl not found. (systemd user service requires systemd)")

  bin_dir, _apps_dir, service_dir = linux_shortcut_paths()

  script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
  if not os.path.exists(script_path):
    sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
      "WORKDIR": dest_dir,
      "JAR": jar_out,
      "XMX": xmx,
      "XMS": xms,
      "EXTRA_ARGS": " ".join(extra_args),
    })
    ensure_dir(bin_dir)
    Path(script_path).write_text(sh_text, encoding="utf-8")
    os.chmod(script_path, 0o755)

  service_name = f"mcserver-{safe}.service"
  service_path = os.path.join(service_dir, service_name)
  ensure_dir(service_dir)
  Path(service_path).write_text(
    systemd_user_service_text(display_name=display, exec_path=script_path, workdir=dest_dir),
    encoding="utf-8"
  )

  run_cmd(["systemctl", "--user", "daemon-reload"], check=False)
  run_cmd(["systemctl", "--user", "enable", "--now", service_name])
  return service_name


def rmsrv_backend_windows(safe: str) -> str:
  startup = win_startup_dir()
  bat_name = f"mcserver-{safe}.bat"
  p = os.path.join(startup, bat_name)
  if os.path.exists(p):
    os.remove(p)
  return p


def rmsrv_backend_macos(safe: str) -> Tuple[str, str]:
  _bin_dir, _apps_dir, agents_dir = mac_shortcut_paths()
  label = mac_launch_agent_label(safe)
  plist_path = os.path.join(agents_dir, f"{label}.plist")
  uid = os.getuid()
  run_cmd(["launchctl", "bootout", f"gui/{uid}", plist_path], check=False)
  if os.path.exists(plist_path):
    os.remove(plist_path)
  return label, plist_path


def rmsrv_backend_linux(safe: str) -> Tuple[str, str]:
  if which("systemctl") is None:
    raise SystemExit("ERROR: systemctl not found.")
  _bin_dir, _apps_dir, service_dir = linux_shortcut_paths()
  service_name = f"mcserver-{safe}.service"
  service_path = os.path.join(service_dir, service_name)
  run_cmd(["systemctl", "--user", "disable", "--now", service_name], check=False)
  if os.path.exists(service_path):
    os.remove(service_path)
  run_cmd(["systemctl", "--user", "daemon-reload"], check=False)
  return service_name, service_path

def cmd_setup_from_config(cfg: Dict[str, Any], config_path: str, override_name: Optional[str]) -> int:
  dest_dir = get_dest_dir(cfg, config_path)
  mc_version = get_mc_version(cfg)
  server_name = get_server_name(cfg)
  jar_out = get_server_jar_out(cfg)
  xmx, xms, extra_args = get_jvm(cfg)

  display = override_name or default_shortcut_name(server_name, mc_version)
  safe = make_safe_name(display)

  if os.name == "nt":
    bat_path = setup_backend_windows(dest_dir, safe, jar_out, xmx, xms, extra_args)
    ok(f"Wrote Start Menu launcher: {bat_path}")
    info("Tip: addsrv will copy it into Startup.")
    return 0

  if is_macos():
    script_path, command_path = setup_backend_macos(dest_dir, safe, jar_out, xmx, xms, extra_args)
    ok(f"Wrote launcher: {script_path}")
    ok(f"Wrote command : {command_path}")
    return 0

  script_path, desktop_path = setup_backend_linux(dest_dir, display, safe, jar_out, xmx, xms, extra_args)
  ok(f"Wrote launcher: {script_path}")
  ok(f"Wrote desktop : {desktop_path}")
  return 0

  if is_macos():
    bin_dir, apps_dir, _agents_dir = mac_shortcut_paths()

    script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
    command_path = os.path.join(apps_dir, f"mcserver-{safe}.command")

    sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
      "WORKDIR": dest_dir,
      "JAR": jar_out,
      "XMX": xmx,
      "XMS": xms,
      "EXTRA_ARGS": " ".join(extra_args),
    })
    ensure_dir(bin_dir)
    Path(script_path).write_text(sh_text, encoding="utf-8")
    os.chmod(script_path, 0o755)

    ensure_dir(apps_dir)
    Path(command_path).write_text(mac_command_wrapper_text(script_path), encoding="utf-8")
    os.chmod(command_path, 0o755)

    ok(f"Wrote launcher: {script_path}")
    ok(f"Wrote command : {command_path}")
    return 0

  bin_dir, apps_dir, _service_dir = linux_shortcut_paths()


  script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
  desktop_path = os.path.join(apps_dir, f"mcserver-{safe}.desktop")

  sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
    "WORKDIR": dest_dir,
    "JAR": jar_out,
    "XMX": xmx,
    "XMS": xms,
    "EXTRA_ARGS": " ".join(extra_args),
  })
  ensure_dir(bin_dir)
  Path(script_path).write_text(sh_text, encoding="utf-8")
  os.chmod(script_path, 0o755)

  desktop_text = render_placeholders(MC_SERVER_DESKTOP_TEMPLATE, {
    "DISPLAY_NAME": display,
    "EXEC_PATH": script_path,
    "ICON_ID": icon_id,
  })
  ensure_dir(apps_dir)
  Path(desktop_path).write_text(desktop_text, encoding="utf-8")

  ok(f"Wrote launcher: {script_path}")
  ok(f"Wrote desktop : {desktop_path}")
  return 0


def cmd_addsrv_from_config(cfg: Dict[str, Any], config_path: str, override_name: Optional[str]) -> int:
  dest_dir = get_dest_dir(cfg, config_path)
  mc_version = get_mc_version(cfg)
  server_name = get_server_name(cfg)
  jar_out = get_server_jar_out(cfg)
  xmx, xms, extra_args = get_jvm(cfg)

  display = override_name or default_shortcut_name(server_name, mc_version)
  safe = make_safe_name(display)

  if os.name == "nt":
    dst = addsrv_backend_windows(dest_dir, safe, jar_out, xmx, xms, extra_args)
    ok(f"Added to Startup: {dst}")
    return 0

  if is_macos():
    label = addsrv_backend_macos(dest_dir, safe, jar_out, xmx, xms, extra_args)
    ok(f"Registered LaunchAgent: {label}")
    uid = os.getuid()
    print("Check with:")
    print(f"  launchctl print gui/{uid}/{label}")
    return 0

  service_name = addsrv_backend_linux(dest_dir, display, safe, jar_out, xmx, xms, extra_args)
  ok(f"Registered user service: {service_name}")
  print("Check status with:")
  print(f"  systemctl --user status {service_name}")
  return 0

  if is_macos():
    bin_dir, _apps_dir, agents_dir = mac_shortcut_paths()

    # Ensure launcher exists
    script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
    if not os.path.exists(script_path):
      sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
        "WORKDIR": dest_dir,
        "JAR": jar_out,
        "XMX": xmx,
        "XMS": xms,
        "EXTRA_ARGS": " ".join(extra_args),
      })
      ensure_dir(bin_dir)
      Path(script_path).write_text(sh_text, encoding="utf-8")
      os.chmod(script_path, 0o755)

    label = mac_launch_agent_label(safe)
    plist_path = os.path.join(agents_dir, f"{label}.plist")

    ensure_dir(os.path.join(dest_dir, "logs"))
    ensure_dir(agents_dir)
    Path(plist_path).write_text(mac_launch_agent_plist_text(label, script_path, dest_dir), encoding="utf-8")

    uid = os.getuid()
    # bootstrap: load / enable agent
    run_cmd(["launchctl", "bootstrap", f"gui/{uid}", plist_path], check=False)
    run_cmd(["launchctl", "enable", f"gui/{uid}/{label}"], check=False)
    run_cmd(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)

    ok(f"Registered LaunchAgent: {label}")
    print("Check with:")
    print(f"  launchctl print gui/{uid}/{label}")
    return 0

  if is_macos():
    _bin_dir, _apps_dir, agents_dir = mac_shortcut_paths()
    label = mac_launch_agent_label(safe)
    plist_path = os.path.join(agents_dir, f"{label}.plist")
    uid = os.getuid()

    run_cmd(["launchctl", "bootout", f"gui/{uid}", plist_path], check=False)
    if os.path.exists(plist_path):
      os.remove(plist_path)
      ok(f"Removed LaunchAgent: {label} ({display})")
    else:
      info(f"(not found) {plist_path}")
    return 0

  if which("systemctl") is None:


    raise SystemExit("ERROR: systemctl not found. (systemd user service requires systemd)")

  bin_dir, _apps_dir, service_dir = linux_shortcut_paths()

  script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
  if not os.path.exists(script_path):
    sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
      "WORKDIR": dest_dir,
      "JAR": jar_out,
      "XMX": xmx,
      "XMS": xms,
      "EXTRA_ARGS": " ".join(extra_args),
    })
    ensure_dir(bin_dir)
    Path(script_path).write_text(sh_text, encoding="utf-8")
    os.chmod(script_path, 0o755)

  service_name = f"mcserver-{safe}.service"
  service_path = os.path.join(service_dir, service_name)
  ensure_dir(service_dir)
  Path(service_path).write_text(
    systemd_user_service_text(display_name=display, exec_path=script_path, workdir=dest_dir),
    encoding="utf-8"
  )

  run_cmd(["systemctl", "--user", "daemon-reload"], check=False)
  run_cmd(["systemctl", "--user", "enable", "--now", service_name])

  ok(f"Registered user service: {service_name}")
  print("Check status with:")
  print(f"  systemctl --user status {service_name}")
  return 0


def cmd_rmsrv_from_config(cfg: Dict[str, Any], config_path: str, override_name: Optional[str]) -> int:
  display, safe = expected_shortcut_ids_from_config(cfg, config_path, override_name)

  if os.name == "nt":
    p = rmsrv_backend_windows(safe)
    if os.path.exists(p):
      ok(f"Removed from Startup: {p}")
    else:
      info(f"(not found) {p}")
    return 0

  if is_macos():
    label, plist_path = rmsrv_backend_macos(safe)
    if os.path.exists(plist_path):
      ok(f"Removed LaunchAgent: {label} ({display})")
    else:
      info(f"(not found) {plist_path}")
    return 0

  service_name, _service_path = rmsrv_backend_linux(safe)
  ok(f"Removed user service: {service_name} ({display})")
  return 0

  if is_macos():
    bin_dir, _apps_dir, agents_dir = mac_shortcut_paths()

    # Ensure launcher exists
    script_path = os.path.join(bin_dir, f"mcserver-{safe}.sh")
    if not os.path.exists(script_path):
      sh_text = render_placeholders(MC_SERVER_SH_TEMPLATE, {
        "WORKDIR": dest_dir,
        "JAR": jar_out,
        "XMX": xmx,
        "XMS": xms,
        "EXTRA_ARGS": " ".join(extra_args),
      })
      ensure_dir(bin_dir)
      Path(script_path).write_text(sh_text, encoding="utf-8")
      os.chmod(script_path, 0o755)

    label = mac_launch_agent_label(safe)
    plist_path = os.path.join(agents_dir, f"{label}.plist")

    ensure_dir(os.path.join(dest_dir, "logs"))
    ensure_dir(agents_dir)
    Path(plist_path).write_text(mac_launch_agent_plist_text(label, script_path, dest_dir), encoding="utf-8")

    uid = os.getuid()
    # bootstrap: load / enable agent
    run_cmd(["launchctl", "bootstrap", f"gui/{uid}", plist_path], check=False)
    run_cmd(["launchctl", "enable", f"gui/{uid}/{label}"], check=False)
    run_cmd(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)

    ok(f"Registered LaunchAgent: {label}")
    print("Check with:")
    print(f"  launchctl print gui/{uid}/{label}")
    return 0

  if is_macos():
    _bin_dir, _apps_dir, agents_dir = mac_shortcut_paths()
    label = mac_launch_agent_label(safe)
    plist_path = os.path.join(agents_dir, f"{label}.plist")
    uid = os.getuid()

    run_cmd(["launchctl", "bootout", f"gui/{uid}", plist_path], check=False)
    if os.path.exists(plist_path):
      os.remove(plist_path)
      ok(f"Removed LaunchAgent: {label} ({display})")
    else:
      info(f"(not found) {plist_path}")
    return 0

  if which("systemctl") is None:


    raise SystemExit("ERROR: systemctl not found.")

  _bin_dir, _apps_dir, service_dir = linux_shortcut_paths()
  service_name = f"mcserver-{safe}.service"
  service_path = os.path.join(service_dir, service_name)

  run_cmd(["systemctl", "--user", "disable", "--now", service_name], check=False)
  if os.path.exists(service_path):
    os.remove(service_path)
  run_cmd(["systemctl", "--user", "daemon-reload"], check=False)

  ok(f"Removed user service: {service_name} ({display})")
  return 0


def list_shortcuts_linux() -> Dict[str, Dict[str, str]]:
  bin_dir, apps_dir, service_dir = linux_shortcut_paths()

  sh_list = sorted(Path(bin_dir).glob("mcserver-*.sh")) if os.path.isdir(bin_dir) else []
  de_list = sorted(Path(apps_dir).glob("mcserver-*.desktop")) if os.path.isdir(apps_dir) else []
  ic_list = sorted(Path(icon_dir).glob("*.svg")) if os.path.isdir(icon_dir) else []
  sv_list = sorted(Path(service_dir).glob("mcserver-*.service")) if os.path.isdir(service_dir) else []

  def key(p: Path) -> str:
    return p.stem.replace("mcserver-", "")

  out: Dict[str, Dict[str, str]] = {}
  for p in sh_list:
    out.setdefault(key(p), {})["launcher"] = str(p)
  for p in de_list:
    out.setdefault(key(p), {})["desktop"] = str(p)
  for p in sv_list:
    out.setdefault(key(p), {})["service"] = str(p)

  return out


def list_shortcuts_windows() -> Dict[str, Dict[str, str]]:
  programs = win_start_menu_programs()
  startup = win_startup_dir()

  p_list = sorted(Path(programs).glob("mcserver-*.bat")) if os.path.isdir(programs) else []
  s_list = sorted(Path(startup).glob("mcserver-*.bat")) if os.path.isdir(startup) else []

  def key(p: Path) -> str:
    return p.stem.replace("mcserver-", "")

  out: Dict[str, Dict[str, str]] = {}
  for p in p_list:
    out.setdefault(key(p), {})["start_menu"] = str(p)
  for p in s_list:
    out.setdefault(key(p), {})["startup"] = str(p)
  return out

def list_shortcuts_macos() -> Dict[str, Dict[str, str]]:
  bin_dir, apps_dir, agents_dir = mac_shortcut_paths()

  sh_list = sorted(Path(bin_dir).glob("mcserver-*.sh")) if os.path.isdir(bin_dir) else []
  cmd_list = sorted(Path(apps_dir).glob("mcserver-*.command")) if os.path.isdir(apps_dir) else []
  ag_list = sorted(Path(agents_dir).glob("mcsm.mcserver.*.plist")) if os.path.isdir(agents_dir) else []

  def key_from_prefix(p: Path, prefix: str) -> str:
    return p.stem.replace(prefix, "")

  out: Dict[str, Dict[str, str]] = {}
  for p in sh_list:
    out.setdefault(key_from_prefix(p, "mcserver-"), {})["launcher"] = str(p)
  for p in cmd_list:
    out.setdefault(key_from_prefix(p, "mcserver-"), {})["command"] = str(p)
  for p in ag_list:
    # label includes full safe id after last dot
    m = re.match(r"mcsm\.mcserver\.(.+)\.plist$", p.name)
    if m:
      sid = m.group(1)
      out.setdefault(sid, {})["launchagent"] = str(p)

  return out



def cmd_shortcuts_list(config_path: str) -> int:
  expected_safe: Optional[str] = None
  expected_display: Optional[str] = None

  if os.path.exists(config_path):
    try:
      cfg = load_toml(config_path)
      expected_display, expected_safe = expected_shortcut_ids_from_config(cfg, config_path, None)
    except Exception as e:
      warn(f"failed to read config for expected shortcut: {e}")

  print("SHORTCUTS")
  if expected_safe:
    print(f"Expected (from {config_path}):")
    print(f"  display : {expected_display}")
    print(f"  id      : {expected_safe}")
    print()

  if os.name == "nt":
    found = list_shortcuts_windows()
    if not found:
      info("No shortcuts found.")
      return 0

    print(f"{'ID':<28} {'START MENU':<48} {'STARTUP':<48}")
    print(f"{'-'*28} {'-'*48} {'-'*48}")
    for sid in sorted(found.keys()):
      sm = found[sid].get("start_menu", "")
      st = found[sid].get("startup", "")
      mark = " <==" if expected_safe and sid == expected_safe else ""
      print(f"{sid:<28} {sm:<48} {st:<48}{mark}")
    return 0

  if is_macos():
    found = list_shortcuts_macos()
    if not found:
      info("No shortcuts found.")
      return 0

    print(f"{'ID':<28} {'LAUNCHER':<46} {'COMMAND':<46} {'LAUNCHAGENT':<46}")
    print(f"{'-'*28} {'-'*46} {'-'*46} {'-'*46}")
    for sid in sorted(found.keys()):
      ln = found[sid].get("launcher", "")
      cm = found[sid].get("command", "")
      ag = found[sid].get("launchagent", "")
      mark = " <==" if expected_safe and sid == expected_safe else ""
      print(f"{sid:<28} {ln:<46} {cm:<46} {ag:<46}{mark}")
    return 0

  found = list_shortcuts_linux()
  if not found:
    info("No shortcuts found.")
    return 0

  print(f"{'ID':<28} {'LAUNCHER':<46} {'DESKTOP':<46}")
  print(f"{'-'*28} {'-'*46} {'-'*46}")
  for sid in sorted(found.keys()):
    ln = found[sid].get("launcher", "")
    de = found[sid].get("desktop", "")
    mark = " <==" if expected_safe and sid == expected_safe else ""
    print(f"{sid:<28} {ln:<46} {de:<46}{mark}")

  print()
  print(f"{'ID':<28} {'SERVICE':<46}")
  print(f"{'-'*28} {'-'*46}")
  for sid in sorted(found.keys()):
    if "service" in found[sid]:
      sv = found[sid].get("service", "")
      mark = " <==" if expected_safe and sid == expected_safe else ""
      print(f"{sid:<28} {sv:<46}{mark}")

  return 0


def cmd_init(platform: str, out_path: str, force: bool) -> int:
  p = Path(out_path)
  if p.exists() and not force:
    raise SystemExit(f"ERROR: {out_path} already exists. Use --force to overwrite.")

  p.parent.mkdir(parents=True, exist_ok=True)
  p.write_text(template_text(platform), encoding="utf-8")
  ok(f"Template written: {out_path}")
  print()
  print("Next:")
  print(f"  1) Edit placeholders in {out_path} (at least user_agent / server.name)")
  print(f"  2) Run install:")
  print(f"     python3 mcsm.py install {platform} 1.21.4")
  return 0


def cmd_list(platform: str, mc_version: Optional[str], config_path: str) -> int:
  user_agent = "mcsm/1.0 (you@example.com)"
  cfg: Optional[Dict[str, Any]] = None

  if os.path.exists(config_path):
    try:
      cfg = load_toml(config_path)
      user_agent = get_user_agent(cfg)
    except Exception as e:
      warn(f"failed to read config: {e}")

  if not mc_version:
    if platform == "purpur":
      mc_version = purpur_latest_mc_version(user_agent)
    else:
      try:
        mc_version = fill_v3_project_versions("paper", user_agent)[0]
      except Exception:
        mc_version = papermc_v2_latest_version("paper", user_agent)
    info(f"mc_version omitted; using latest detected: {mc_version}")

  sp = resolve_server_plan(platform, mc_version, user_agent)

  print(f"PLATFORM   : {platform}")
  print(f"MC_VERSION : {mc_version}")
  print()
  print(f"{'server':<18} {sp.server_label}")
  print()

  tmap: Dict[str, Dict[str, Any]] = {}
  if cfg is not None:
    try:
      if get_server_platform(cfg) == platform:
        tmap = get_targets(cfg)
    except Exception:
      tmap = {}

  if not tmap:
    tmap = {
      "viaversion": {"type": "modrinth", "slug": "viaversion", "loaders": ["paper", "purpur", "spigot", "bukkit"], "out": "plugins/ViaVersion.jar"},
      "geyser": {"type": "geyser", "project": "geyser", "platform": "spigot", "out": "plugins/Geyser-spigot.jar"},
      "floodgate": {"type": "geyser", "project": "floodgate", "platform": "spigot", "out": "plugins/Floodgate-spigot.jar"},
    }

  print(f"{'NAME':<18} {'TYPE':<10} {'LATEST':<24} NOTE")
  print(f"{'-'*18} {'-'*10} {'-'*24} {'-'*20}")
  for name, td in tmap.items():
    try:
      tp = resolve_target_plan(name, td, mc_version, user_agent)
      note = ("for " + mc_version) if tp.ttype == "modrinth" else "unfiltered"
      print(f"{name:<18} {tp.ttype:<10} {tp.latest:<24} {note}")
    except Exception as e:
      print(f"{name:<18} {'(error)':<10} {'(error)':<24} {e}")

  return 0


def ensure_config_for_install(platform: str, mc_version: str, config_path: str) -> None:
  if os.path.exists(config_path):
    patch_config_text(config_path, platform=platform, mc_version=mc_version)
    return
  step(f"Config not found, creating default config: {config_path}")
  Path(config_path).parent.mkdir(parents=True, exist_ok=True)
  Path(config_path).write_text(default_config_text(platform, mc_version), encoding="utf-8")


def cmd_install(platform: str, mc_version: str, config_path: str) -> int:
  if platform not in ("purpur", "paper"):
    raise SystemExit("ERROR: platform must be purpur|paper")
  ensure_config_for_install(platform, mc_version, config_path)
  return cmd_update(config_path, must_platform=platform, must_mc_version=mc_version)


def cmd_update(config_path: str, must_platform: Optional[str] = None, must_mc_version: Optional[str] = None) -> int:
  if not os.path.exists(config_path):
    raise SystemExit(f"ERROR: config not found: {config_path} (run install or init first)")

  cfg = load_toml(config_path)
  user_agent = get_user_agent(cfg)
  dest_dir = get_dest_dir(cfg, config_path)
  platform = get_server_platform(cfg)
  mc_version = get_mc_version(cfg)

  if must_platform and platform != must_platform:
    raise SystemExit(f"ERROR: config server.type={platform} does not match requested platform={must_platform}")
  if must_mc_version and mc_version != must_mc_version:
    raise SystemExit(f"ERROR: config mc_version={mc_version} does not match requested mc_version={must_mc_version}")

  server_name = get_server_name(cfg)
  jar_out = get_server_jar_out(cfg)
  keep_ver_jar = get_keep_versioned_jar(cfg)

  ensure_dir(dest_dir)
  ensure_dir(os.path.join(dest_dir, "plugins"))
  ensure_dir(bak_root(dest_dir))

  targets_all = get_targets(cfg)
  target_names = select_targets(cfg)

  step("Resolving latest versions")
  sp = resolve_server_plan(platform, mc_version, user_agent)

  plans: List[TargetPlan] = []
  for n in target_names:
    td = targets_all.get(n)
    if not isinstance(td, dict):
      raise SystemExit(f"ERROR: target '{n}' not found in [targets]")
    enabled = bool(td.get("enabled", True))
    if not enabled:
      continue
    plans.append(resolve_target_plan(n, td, mc_version, user_agent))

  bid = new_backup_id()
  step(f"Backup id: {bid}")
  step(f"Backing up into: {os.path.join(bak_root(dest_dir), bid)}")

  r = backup_move(dest_dir, bid, os.path.join(dest_dir, jar_out))
  if r:
    info(f"backup: {r}")

  for tp in plans:
    r2 = backup_move(dest_dir, bid, os.path.join(dest_dir, tp.out))
    if r2:
      info(f"backup: {r2}")

  step("Installing server")
  down(f"server: {sp.server_label}")

  if keep_ver_jar:
    safe = make_safe_name(f"{platform}-{sp.server_label}")
    verfile = os.path.join(dest_dir, f"{safe}.jar")
  else:
    verfile = os.path.join(dest_dir, jar_out)

  http_download(sp.url, verfile, user_agent)

  jar_out_abs = os.path.join(dest_dir, jar_out)
  if keep_ver_jar:
    try:
      if os.path.exists(jar_out_abs) or os.path.islink(jar_out_abs):
        os.remove(jar_out_abs)
      os.symlink(os.path.basename(verfile), jar_out_abs)
    except OSError:
      shutil.copy2(verfile, jar_out_abs)

  step("Installing targets")
  state = load_state(dest_dir)
  state_set_server(state, platform, mc_version, sp.server_label, sp.url, sha256_file(verfile))

  for tp in plans:
    if not tp.url:
      raise SystemExit(f"ERROR: no download url for target '{tp.name}' (latest={tp.latest})")
    out_abs = os.path.join(dest_dir, tp.out)
    down(f"{tp.name}: {tp.latest}")
    http_download(tp.url, out_abs, user_agent)
    state_set_target(state, tp.name, tp.ttype, tp.latest, tp.url, tp.out, sha256_file(out_abs))

  save_state(dest_dir, state)

  suggested = default_shortcut_name(server_name, mc_version)
  print()
  ok("update/install done.")
  info(f"SERVER_DIR: {dest_dir}")
  info(f"Backup    : {os.path.join(bak_root(dest_dir), bid)}")
  info(f"State     : {state_path(dest_dir)}")
  print()
  ok(f"Suggested shortcut name: {suggested}")
  print("Next:")
  print("  python3 mcsm.py setup")
  print("  python3 mcsm.py addsrv   # optional")
  print("  python3 mcsm.py shortcuts list")
  return 0


def build_argparser() -> argparse.ArgumentParser:
  ap = argparse.ArgumentParser(prog="mcsm.py")
  ap.add_argument("--config", default="mcsm.toml", help="path to mcsm.toml (server directory is derived from this path)")
  sub = ap.add_subparsers(dest="cmd", required=True)

  p_list = sub.add_parser("list", help="show latest server/plugins for platform and (optional) mc_version")
  p_list.add_argument("platform", choices=["purpur", "paper"])
  p_list.add_argument("mc_version", nargs="?", default=None)

  p_install = sub.add_parser("install", help="create mcsm.toml if missing, lock mc_version, and install server/plugins")
  p_install.add_argument("platform", choices=["purpur", "paper"])
  p_install.add_argument("mc_version")

  sub.add_parser("update", help="update server/plugins using mcsm.toml (backup + overwrite)")

  p_init = sub.add_parser("init", help="write a commented template mcsm.toml for customization (optional)")
  p_init.add_argument("platform", choices=["purpur", "paper"])
  p_init.add_argument("--out", default=None, help="output path (default: --config)")
  p_init.add_argument("--force", action="store_true", help="overwrite if exists")

  p_setup = sub.add_parser("setup", help="create launcher (Linux: .desktop, Windows: Start Menu, macOS: .command)")
  p_setup.add_argument("--name", default=None, help="override shortcut display name (default: server.name + mc_version)")

  p_add = sub.add_parser("addsrv", help="register auto-start (Linux: systemd --user, Windows: Startup, macOS: launchd)")
  p_add.add_argument("--name", default=None, help="override shortcut display name")

  p_rm = sub.add_parser("rmsrv", help="remove auto-start (Linux: systemd --user, Windows: Startup, macOS: launchd)")
  p_rm.add_argument("--name", default=None, help="override shortcut display name")

  p_sc = sub.add_parser("shortcuts", help="shortcuts inventory")
  sc_sub = p_sc.add_subparsers(dest="shortcuts_cmd", required=True)
  sc_sub.add_parser("list", help="list installed shortcuts")

  return ap


def main(argv: List[str]) -> int:
  ap = build_argparser()
  ns = ap.parse_args(argv)
  config_path = ns.config

  if ns.cmd == "init":
    out_path = ns.out if ns.out else config_path
    return cmd_init(ns.platform, out_path, ns.force)

  if ns.cmd == "list":
    return cmd_list(ns.platform, ns.mc_version, config_path)

  if ns.cmd == "install":
    return cmd_install(ns.platform, ns.mc_version, config_path)

  if ns.cmd == "update":
    return cmd_update(config_path)

  if ns.cmd == "shortcuts":
    if ns.shortcuts_cmd == "list":
      return cmd_shortcuts_list(config_path)
    raise SystemExit("ERROR: unknown shortcuts subcommand")

  if not os.path.exists(config_path):
    raise SystemExit(f"ERROR: config not found: {config_path} (run install or init first)")

  cfg = load_toml(config_path)

  if ns.cmd == "setup":
    return cmd_setup_from_config(cfg, config_path, ns.name)

  if ns.cmd == "addsrv":
    return cmd_addsrv_from_config(cfg, config_path, ns.name)

  if ns.cmd == "rmsrv":
    return cmd_rmsrv_from_config(cfg, config_path, ns.name)

  raise SystemExit("ERROR: unknown command")


if __name__ == "__main__":
  try:
    sys.exit(main(sys.argv[1:]))
  except KeyboardInterrupt:
    err("Interrupted.")
    sys.exit(130)
