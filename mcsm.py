#!/usr/bin/env python3
from __future__ import annotations

# mcsm.py - Minecraft Server & Plugin Manager (Purpur/Paper)
#
# CLI (command-first):
#   mcsm list <platform> [mc_version]
#   mcsm install <platform> <mc_version> [--force-eula-true]
#   mcsm update
#   mcsm init <platform> [--force]
#   mcsm setup
#   mcsm addsrv
#   mcsm rmsrv
#
# Notes:
# - Run mcsm in the server directory (directory that contains mcsm.toml), or pass --config.
# - install/update write state.toml (tool-owned).
# - update requires state.toml (so it can compare installed vs latest).
# - EULA is NOT modified by default; use --force-eula-true explicitly.

import argparse
import datetime as dt
import hashlib
import json
import os
import platform as py_platform
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
# TOML reader (py3.11+: tomllib / py3.10: tomli)
# =========================================================
try:
  import tomllib  # py3.11+
except Exception:
  tomllib = None  # type: ignore

try:
  import tomli  # py3.10 (pip)
except Exception:
  tomli = None  # type: ignore

# =========================================================
# Templates (MC_SERVER_*_TEMPLATE)
# =========================================================
MC_SERVER_SH_TEMPLATE = """#!/bin/sh
set -eu
cd "{server_dir}"
exec java {jvm_args} -jar "{jar}" {extra_args}
"""

MC_SERVER_COMMAND_TEMPLATE = """#!/bin/bash
set -eu
cd "{server_dir}"
exec java {jvm_args} -jar "{jar}" {extra_args}
"""

MC_SERVER_BAT_TEMPLATE = r"""@echo off
setlocal
cd /d "{server_dir}"
java {jvm_args} -jar "{jar}" {extra_args}
endlocal
"""

MC_SERVER_DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=MCSERVER {name}
GenericName=MCSERVER {name}
Comment=Minecraft Server ({platform} {mc_version})
Exec={exec_cmd}
Terminal=true
Categories=Game;Server;
"""

SYSTEMD_USER_SERVICE_TEMPLATE = """[Unit]
Description=MCSERVER {name}
After=network.target

[Service]
Type=simple
WorkingDirectory={server_dir}
ExecStart=java {jvm_args} -jar {jar} {extra_args}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

MAC_LAUNCH_AGENT_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{command_path}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_out}</string>
  <key>StandardErrorPath</key><string>{log_err}</string>
</dict>
</plist>
"""

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
# Friendly output
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

def make_safe_name(s: str) -> str:
  s = s.strip().lower()
  s = re.sub(r"[^a-z0-9._-]+", "-", s)
  s = re.sub(r"-{2,}", "-", s).strip("-")
  return s or "mcserver"

def config_dir_from_path(config_path: str) -> str:
  return os.path.abspath(os.path.dirname(os.path.abspath(config_path)))

def write_text(path: str, text: str, mode: int | None = None) -> None:
  ensure_dir(os.path.dirname(path))
  Path(path).write_text(text, encoding="utf-8")
  if mode is not None and os.name != "nt":
    os.chmod(path, mode)

def which(cmd: str) -> Optional[str]:
  for p in os.environ.get("PATH", "").split(os.pathsep):
    c = os.path.join(p, cmd)
    if os.path.isfile(c) and os.access(c, os.X_OK):
      return c
  return None

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
# APIs: Server (Purpur/Paper)
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

def modrinth_latest_for_mc(slug: str, loaders: List[str], game_ver: str, user_agent: str) -> Tuple[str, str, str]:
  base = f"https://api.modrinth.com/v2/project/{slug}/version"
  url = join_url(base, {
    "loaders": json.dumps(loaders, ensure_ascii=False),
    "game_versions": json.dumps([game_ver], ensure_ascii=False),
  })
  arr = http_get_json(url, user_agent)
  if not arr:
    return "", "(not found)", ""
  v = arr[0]
  version_id = str(v.get("id", ""))
  version_number = str(v.get("version_number", "(unknown)"))
  files = v.get("files", [])
  file_url = str(files[0].get("url", "")) if files else ""
  return version_id, version_number, file_url

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
  latest_id: str
  latest_label: str
  url: str
  out: str

# =========================================================
# Config template (PLACEHOLDER)
# =========================================================
def template_text(platform: str) -> str:
  if platform not in ("purpur", "paper"):
    raise SystemExit(f"ERROR: unknown platform for template: {platform}")

  header = """# =========================================================
# mcsm.toml - Minecraft Server & Plugin Manager config
#
# Run mcsm inside your server directory.
# mcsm installs into the directory that contains this mcsm.toml.
#
# PLACEHOLDER format:
#   - PLACEHOLDER_MC_VERSION
#   - PLACEHOLDER_USER_AGENT
#   - PLACEHOLDER_SERVERNAME
# =========================================================

schema = 1
mc_version = "PLACEHOLDER_MC_VERSION"
user_agent = "PLACEHOLDER_USER_AGENT"

"""
  server_block = f"""[server]
type = "{platform}"
name = "PLACEHOLDER_SERVERNAME"
jar_out = "server.jar"
keep_versioned_jar = true
[server.jvm]
xmx = "1024M"
xms = "1024M"
#extra_args = ["nogui"]

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

def _patch_top_level_mc_version(text: str, mc_version: str) -> str:
  text, n = re.subn(r'^\s*mc_version\s*=\s*".*"\s*$', f'mc_version = "{mc_version}"', text, flags=re.M)
  if n == 0:
    text = text.rstrip() + f'\nmc_version = "{mc_version}"\n'
  return text

def _patch_server_table(text: str, platform: str) -> str:
  # line-based patch: only modify [server].type, do not touch [targets.*]
  lines = text.splitlines(True)
  out: List[str] = []
  i = 0
  while i < len(lines):
    line = lines[i]
    if line.strip() == "[server]":
      out.append(line)
      i += 1
      saw_type = False
      while i < len(lines) and not lines[i].lstrip().startswith("["):
        l = lines[i]
        if re.match(r"^\s*type\s*=", l):
          out.append(f'type = "{platform}"\n')
          saw_type = True
        else:
          out.append(l)
        i += 1
      if not saw_type:
        out.append(f'type = "{platform}"\n')
      continue
    out.append(line)
    i += 1
  return "".join(out)

def default_config_text(platform: str, mc_version: str) -> str:
  txt = template_text(platform)
  txt = _patch_top_level_mc_version(txt, mc_version)
  txt = _patch_server_table(txt, platform)
  return txt

def patch_config_text(path: str, platform: str, mc_version: str) -> None:
  txt = Path(path).read_text(encoding="utf-8")
  txt = _patch_top_level_mc_version(txt, mc_version)
  txt = _patch_server_table(txt, platform)
  Path(path).write_text(txt, encoding="utf-8")

# =========================================================
# State
# =========================================================
def state_path(dest_dir: str) -> str:
  return os.path.join(dest_dir, "state.toml")

def save_state(dest_dir: str, state: Dict[str, Any]) -> None:
  Path(state_path(dest_dir)).write_text(toml_dump_simple(state), encoding="utf-8")

def state_meta() -> Dict[str, Any]:
  return {"schema": 1, "checked_at": now_iso_jst(), "installed": {"server": {}, "targets": {}}}

# =========================================================
# Backup
# =========================================================
def bak_root(dest_dir: str) -> str:
  return os.path.join(dest_dir, ".bak")

def new_backup_id() -> str:
  return dt.datetime.now().strftime("%Y%m%d-%H%M%S")

def relpath_from_dest(dest_dir: str, abs_path: str) -> str:
  try:
    return os.path.relpath(abs_path, dest_dir)
  except Exception:
    return os.path.basename(abs_path)

def backup_move(dest_dir: str, backup_id: str, abs_path: str) -> None:
  if not os.path.exists(abs_path):
    return
  rel = relpath_from_dest(dest_dir, abs_path)
  dst = os.path.join(bak_root(dest_dir), backup_id, rel)
  ensure_dir(os.path.dirname(dst))
  shutil.move(abs_path, dst)

# =========================================================
# Config getters
# =========================================================
def get_user_agent(cfg: Dict[str, Any]) -> str:
  ua = str(cfg.get("user_agent", "")).strip()
  if not ua or "PLACEHOLDER" in ua:
    warn("user_agent is placeholder. Set user_agent in mcsm.toml (recommended).")
    return "mcsm/1.0 (you@example.com)"
  return ua

def get_dest_dir(cfg: Dict[str, Any], config_path: str) -> str:
  global _warned_dest_dir_ignored
  if "dest_dir" in cfg and not _warned_dest_dir_ignored:
    warn("dest_dir is ignored. mcsm uses the mcsm.toml directory.")
    _warned_dest_dir_ignored = True
  return config_dir_from_path(config_path)

def get_mc_version(cfg: Dict[str, Any]) -> str:
  v = str(cfg.get("mc_version", "")).strip()
  if not v or v.startswith("PLACEHOLDER"):
    raise SystemExit("ERROR: mc_version is not set.")
  return v

def get_server(cfg: Dict[str, Any]) -> Dict[str, Any]:
  s = cfg.get("server", {})
  if not isinstance(s, dict):
    raise SystemExit("ERROR: [server] is required")
  return s

def get_server_platform(cfg: Dict[str, Any]) -> str:
  t = str(get_server(cfg).get("type", "")).strip()
  if t not in ("purpur", "paper"):
    raise SystemExit(f"ERROR: unsupported server.type={t}")
  return t

def get_server_name(cfg: Dict[str, Any], dest_dir: str) -> str:
  name = str(get_server(cfg).get("name", "")).strip()
  if not name or "PLACEHOLDER" in name:
    return os.path.basename(dest_dir.rstrip("/\\")) or "mcserver"
  return name

def get_server_jar_out(cfg: Dict[str, Any]) -> str:
  return str(get_server(cfg).get("jar_out", "server.jar")).strip()

def get_keep_versioned_jar(cfg: Dict[str, Any]) -> bool:
  return bool(get_server(cfg).get("keep_versioned_jar", True))

def get_targets(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
  t = cfg.get("targets", {})
  return t if isinstance(t, dict) else {}

def select_targets(cfg: Dict[str, Any]) -> List[str]:
  # install/update targets that are PRESENT in [targets.*] (commented-out = absent)
  tmap = get_targets(cfg)
  names = [k for k, v in tmap.items() if isinstance(k, str) and isinstance(v, dict)]
  return sorted(set(names))

def get_jvm_args(cfg: Dict[str, Any]) -> Tuple[str, str]:
  jvm = get_server(cfg).get("jvm", {})
  if not isinstance(jvm, dict):
    jvm = {}
  xmx = str(jvm.get("xmx", "1024M")).strip()
  xms = str(jvm.get("xms", "1024M")).strip()
  extra = jvm.get("extra_args", [])
  if not isinstance(extra, list):
    extra = []
  extra_args = " ".join([str(x) for x in extra if str(x).strip()])
  jvm_args = f"-Xmx{xmx} -Xms{xms}"
  return jvm_args, extra_args

# =========================================================
# Resolvers
# =========================================================
def resolve_server_plan(platform: str, mc_version: str, ua: str) -> ServerPlan:
  if platform == "purpur":
    build = purpur_latest_build(mc_version, ua)
    return ServerPlan(platform, mc_version, f"{mc_version}-{build}", purpur_download_url(mc_version))
  if platform == "paper":
    try:
      build_id, url = fill_v3_latest_stable_download("paper", mc_version, ua)
      return ServerPlan(platform, mc_version, f"{mc_version}-{build_id}", url)
    except Exception:
      b = papermc_v2_latest_build("paper", mc_version, ua)
      return ServerPlan(platform, mc_version, f"{mc_version}-{b}", papermc_v2_download_url("paper", mc_version, b))
  raise SystemExit(f"ERROR: unsupported platform={platform}")

def resolve_target_plan(name: str, td: Dict[str, Any], mc_version: str, ua: str) -> TargetPlan:
  ttype = str(td.get("type", "")).strip()
  out = str(td.get("out", "")).strip()
  if not out:
    raise SystemExit(f"ERROR: target '{name}' missing out")
  if ttype == "modrinth":
    slug = str(td.get("slug", "")).strip()
    loaders = td.get("loaders", [])
    if not slug or not isinstance(loaders, list) or not loaders:
      raise SystemExit(f"ERROR: target '{name}' invalid modrinth config")
    loaders2 = [str(x) for x in loaders if str(x).strip()]
    vid, vnum, url = modrinth_latest_for_mc(slug, loaders2, mc_version, ua)
    return TargetPlan(name, ttype, vid, vnum, url, out)
  if ttype == "geyser":
    project = str(td.get("project", "")).strip()
    platform = str(td.get("platform", "")).strip()
    latest = geyser_latest_version(project, ua)
    url = geyser_download_url(project, platform)
    return TargetPlan(name, ttype, "", latest, url, out)
  raise SystemExit(f"ERROR: target '{name}' unsupported type={ttype}")

# =========================================================
# EULA
# =========================================================
def set_eula_true(dest_dir: str) -> None:
  Path(os.path.join(dest_dir, "eula.txt")).write_text("eula=true\n", encoding="utf-8")

# =========================================================
# Setup/AddSrv/RmSrv backends
# =========================================================
def os_family() -> str:
  if os.name == "nt":
    return "windows"
  sysname = py_platform.system().lower()
  if "darwin" in sysname:
    return "macos"
  return "linux"

def server_script_paths(dest_dir: str, name: str) -> Dict[str, str]:
  safe = make_safe_name(name)
  return {
    "linux": os.path.join(dest_dir, "mcserver.sh"),
    "macos": os.path.join(dest_dir, "mcserver.command"),
    "windows": os.path.join(dest_dir, "mcserver.bat"),
    "safe": safe,
  }

def render_server_scripts(cfg: Dict[str, Any], dest_dir: str) -> Dict[str, str]:
  name = get_server_name(cfg, dest_dir)
  plat = get_server_platform(cfg)
  mc_ver = get_mc_version(cfg)
  jar = get_server_jar_out(cfg)
  jvm_args, extra_args = get_jvm_args(cfg)
  extra_args = extra_args.strip() or "nogui"

  paths = server_script_paths(dest_dir, name)

  sh = MC_SERVER_SH_TEMPLATE.format(server_dir=dest_dir, jvm_args=jvm_args, jar=jar, extra_args=extra_args)
  command = MC_SERVER_COMMAND_TEMPLATE.format(server_dir=dest_dir, jvm_args=jvm_args, jar=jar, extra_args=extra_args)
  bat = MC_SERVER_BAT_TEMPLATE.format(server_dir=dest_dir, jvm_args=jvm_args, jar=jar, extra_args=extra_args)

  write_text(paths["linux"], sh, mode=0o755)
  write_text(paths["macos"], command, mode=0o755)
  write_text(paths["windows"], bat, mode=None)

  return {"name": name, "platform": plat, "mc_version": mc_ver, **paths}

def linux_setup(cfg: Dict[str, Any], dest_dir: str) -> None:
  meta = render_server_scripts(cfg, dest_dir)
  safe = meta["safe"]
  bin_dir = os.path.expanduser("~/.local/bin")
  app_dir = os.path.expanduser("~/.local/share/applications")
  ensure_dir(bin_dir)
  ensure_dir(app_dir)

  launcher = os.path.join(bin_dir, f"mcserver-{safe}")
  wrapper = f"""#!/bin/sh
set -eu
exec "{meta['linux']}"
"""
  write_text(launcher, wrapper, mode=0o755)

  desktop_path = os.path.join(app_dir, f"mcserver-{safe}.desktop")
  desktop = MC_SERVER_DESKTOP_TEMPLATE.format(
    name=meta["name"],
    platform=meta["platform"],
    mc_version=meta["mc_version"],
    exec_cmd=launcher,
  )
  write_text(desktop_path, desktop, mode=None)

  ok(f"Created: {launcher}")
  ok(f"Created: {desktop_path}")
  info("Tip: ensure ~/.local/bin is in PATH.")

def windows_setup(cfg: Dict[str, Any], dest_dir: str) -> None:
  meta = render_server_scripts(cfg, dest_dir)
  safe = meta["safe"]
  appdata = os.environ.get("APPDATA", "")
  if not appdata:
    warn("APPDATA not set; setup will only generate mcserver.bat in server dir.")
    ok(f"Created: {meta['windows']}")
    return
  startmenu = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs")
  ensure_dir(startmenu)
  shortcut_bat = os.path.join(startmenu, f"MCSERVER-{safe}.bat")
  shutil.copy2(meta["windows"], shortcut_bat)
  ok(f"Created: {shortcut_bat}")

def macos_setup(cfg: Dict[str, Any], dest_dir: str) -> None:
  meta = render_server_scripts(cfg, dest_dir)
  safe = meta["safe"]
  bin_dir = os.path.expanduser("~/.local/bin")
  ensure_dir(bin_dir)
  launcher = os.path.join(bin_dir, f"mcserver-{safe}")
  wrapper = f"""#!/bin/sh
set -eu
exec "{meta['macos']}"
"""
  write_text(launcher, wrapper, mode=0o755)
  ok(f"Created: {launcher}")
  info("You can run it from Terminal. (GUI app bundle is out of scope.)")

def cmd_setup(cfg: Dict[str, Any], dest_dir: str) -> int:
  step("Generating server launch scripts")
  fam = os_family()
  if fam == "linux":
    linux_setup(cfg, dest_dir)
  elif fam == "macos":
    macos_setup(cfg, dest_dir)
  else:
    windows_setup(cfg, dest_dir)
  ok("Setup done.")
  return 0

def cmd_addsrv(cfg: Dict[str, Any], dest_dir: str) -> int:
  meta = render_server_scripts(cfg, dest_dir)
  safe = meta["safe"]
  fam = os_family()
  if fam == "linux":
    service_dir = os.path.expanduser("~/.config/systemd/user")
    ensure_dir(service_dir)
    unit_path = os.path.join(service_dir, f"mcserver-{safe}.service")
    jvm_args, extra_args = get_jvm_args(cfg)
    extra_args = extra_args.strip() or "nogui"
    unit = SYSTEMD_USER_SERVICE_TEMPLATE.format(
      name=meta["name"],
      server_dir=dest_dir,
      jvm_args=jvm_args,
      jar=get_server_jar_out(cfg),
      extra_args=extra_args,
    )
    write_text(unit_path, unit, mode=None)
    # enable --user if possible
    if which("systemctl"):
      subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
      subprocess.run(["systemctl", "--user", "enable", "--now", f"mcserver-{safe}.service"], check=False)
      ok(f"Installed systemd user service: mcserver-{safe}.service")
      info("Use: systemctl --user status mcserver-*.service")
    else:
      warn("systemctl not found; wrote unit file only.")
    ok(f"Created: {unit_path}")
    return 0

  if fam == "macos":
    agents = os.path.expanduser("~/Library/LaunchAgents")
    ensure_dir(agents)
    label = f"com.mcsm.mcserver.{safe}"
    plist_path = os.path.join(agents, f"{label}.plist")
    logs = os.path.expanduser("~/Library/Logs")
    ensure_dir(logs)
    plist = MAC_LAUNCH_AGENT_PLIST_TEMPLATE.format(
      label=label,
      command_path=meta["macos"],
      log_out=os.path.join(logs, f"{label}.out.log"),
      log_err=os.path.join(logs, f"{label}.err.log"),
    )
    write_text(plist_path, plist, mode=None)
    if which("launchctl"):
      subprocess.run(["launchctl", "unload", plist_path], check=False)
      subprocess.run(["launchctl", "load", plist_path], check=False)
      ok(f"Loaded LaunchAgent: {label}")
    else:
      warn("launchctl not found; wrote plist only.")
    ok(f"Created: {plist_path}")
    return 0

  # windows: copy bat to Startup
  appdata = os.environ.get("APPDATA", "")
  if not appdata:
    raise SystemExit("ERROR: APPDATA not set")
  startup = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
  ensure_dir(startup)
  dst = os.path.join(startup, f"MCSERVER-{safe}.bat")
  shutil.copy2(meta["windows"], dst)
  ok(f"Added to Startup: {dst}")
  return 0

def cmd_rmsrv(cfg: Dict[str, Any], dest_dir: str) -> int:
  name = get_server_name(cfg, dest_dir)
  safe = make_safe_name(name)
  fam = os_family()
  if fam == "linux":
    unit = f"mcserver-{safe}.service"
    unit_path = os.path.expanduser(f"~/.config/systemd/user/{unit}")
    if which("systemctl"):
      subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False)
      subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if os.path.exists(unit_path):
      os.remove(unit_path)
      ok(f"Removed: {unit_path}")
    else:
      warn(f"Not found: {unit_path}")
    return 0

  if fam == "macos":
    label = f"com.mcsm.mcserver.{safe}"
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    if which("launchctl") and os.path.exists(plist_path):
      subprocess.run(["launchctl", "unload", plist_path], check=False)
    if os.path.exists(plist_path):
      os.remove(plist_path)
      ok(f"Removed: {plist_path}")
    else:
      warn(f"Not found: {plist_path}")
    return 0

  appdata = os.environ.get("APPDATA", "")
  if not appdata:
    raise SystemExit("ERROR: APPDATA not set")
  startup = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
  path = os.path.join(startup, f"MCSERVER-{safe}.bat")
  if os.path.exists(path):
    os.remove(path)
    ok(f"Removed: {path}")
  else:
    warn(f"Not found: {path}")
  return 0


# =========================================================
# Status (offline, uses state.toml only)
# =========================================================
def _status_line(label: str, ok_flag: bool, detail: str = "") -> str:
  s = "OK" if ok_flag else "NG"
  if detail:
    return f"{label}: {s} ({detail})"
  return f"{label}: {s}"

def cmd_status(config_path: str) -> int:
  if not os.path.exists(config_path):
    raise SystemExit(f"ERROR: config not found: {config_path}")
  cfg = load_toml(config_path)
  dest_dir = get_dest_dir(cfg, config_path)
  spath = state_path(dest_dir)
  if not os.path.exists(spath):
    raise SystemExit("ERROR: state.toml not found. Run install first.")
  st = load_toml(spath)

  installed = st.get("installed", {})
  ins_server = installed.get("server", {}) if isinstance(installed, dict) else {}
  ins_targets = installed.get("targets", {}) if isinstance(installed, dict) else {}

  print(f"SERVER_DIR: {dest_dir}")
  print(f"STATE     : {spath}")
  print("")

  # --- server ---
  print("Server:")
  if not isinstance(ins_server, dict):
    print("  (invalid state.toml)")
  else:
    platform = str(ins_server.get("type", "")).strip() or "(unknown)"
    mc_ver = str(ins_server.get("mc_version", "")).strip() or "(unknown)"
    server_ver = str(ins_server.get("server_version", "")).strip() or "(unknown)"
    jar_path = str(ins_server.get("jar_path", "server.jar")).strip() or "server.jar"
    sha_expect = str(ins_server.get("sha256", "")).strip()

    jar_abs = os.path.join(dest_dir, jar_path) if not os.path.isabs(jar_path) else jar_path
    exists = os.path.exists(jar_abs)

    status = "OK"
    detail = ""
    if not exists:
      status = "MISSING"
    else:
      if sha_expect:
        sha_now = sha256_file(jar_abs)
        if sha_now != sha_expect:
          status = "MODIFIED"
          detail = "hash mismatch"
      else:
        status = "UNKNOWN"
        detail = "no hash in state"
    print(f"  {platform} {mc_ver} {server_ver}")
    print(f"  file   : {jar_path}")
    if detail:
      print(f"  status : {status} ({detail})")
    else:
      print(f"  status : {status}")
    installed_at = str(ins_server.get("installed_at", "")).strip()
    if installed_at:
      print(f"  at     : {installed_at}")

  print("")
  print("Plugins:")
  if not isinstance(ins_targets, dict) or not ins_targets:
    print("  (none)")
    return 0

  # stable order: by key
  keys = sorted([k for k in ins_targets.keys() if isinstance(k, str)])
  for name in keys:
    it = ins_targets.get(name, {})
    if not isinstance(it, dict):
      continue
    v = str(it.get("resolved_version", "")).strip() or "(unknown)"
    out_rel = str(it.get("out", "")).strip()
    sha_expect = str(it.get("sha256", "")).strip()
    if not out_rel:
      print(f"  {name:<12} {v:<12} INVALID (no out)")
      continue
    out_abs = os.path.join(dest_dir, out_rel) if not os.path.isabs(out_rel) else out_rel
    if not os.path.exists(out_abs):
      print(f"  {name:<12} {v:<12} MISSING")
      continue
    if sha_expect:
      sha_now = sha256_file(out_abs)
      if sha_now != sha_expect:
        print(f"  {name:<12} {v:<12} MODIFIED")
      else:
        print(f"  {name:<12} {v:<12} OK")
    else:
      print(f"  {name:<12} {v:<12} UNKNOWN")
  return 0

# =========================================================
# Commands: init/list/install/update (core)
# =========================================================
def ensure_config_for_install(platform: str, mc_version: str, config_path: str) -> None:
  if os.path.exists(config_path):
    patch_config_text(config_path, platform, mc_version)
    return
  step(f"Creating config: {config_path}")
  Path(config_path).write_text(default_config_text(platform, mc_version), encoding="utf-8")

def cmd_init(platform: str, config_path: str, force: bool) -> int:
  p = Path(config_path)
  if p.exists() and not force:
    raise SystemExit(f"ERROR: {config_path} exists. Use --force.")
  p.write_text(template_text(platform), encoding="utf-8")
  ok(f"Wrote template: {config_path}")
  return 0

def cmd_list(platform: str, mc_version: Optional[str], config_path: str) -> int:
  ua = "mcsm/1.0 (you@example.com)"
  cfg = load_toml(config_path) if os.path.exists(config_path) else None
  if cfg is not None:
    ua = get_user_agent(cfg)

  if not mc_version:
    mc_version = purpur_latest_mc_version(ua) if platform == "purpur" else (
      fill_v3_project_versions("paper", ua)[0] if fill_v3_project_versions("paper", ua) else papermc_v2_latest_version("paper", ua)
    )

  sp = resolve_server_plan(platform, mc_version, ua)
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

  print(f"{'NAME':<18} {'TYPE':<10} {'LATEST':<24}")
  print(f"{'-'*18} {'-'*10} {'-'*24}")
  for name, td in tmap.items():
    try:
      tp = resolve_target_plan(name, td, mc_version, ua)
      print(f"{name:<18} {tp.ttype:<10} {tp.latest_label:<24}")
    except Exception as e:
      print(f"{name:<18} {'(error)':<10} {'(error)':<24} {e}")
  return 0

def _server_versioned_jar_path(dest_dir: str, platform: str, server_label: str) -> str:
  safe = make_safe_name(f"{platform}-{server_label}")
  return os.path.join(dest_dir, f"{safe}.jar")

def _apply_install_or_update(cfg: Dict[str, Any], config_path: str, require_state: bool, force_eula_true: bool) -> int:
  ua = get_user_agent(cfg)
  dest_dir = get_dest_dir(cfg, config_path)
  platform = get_server_platform(cfg)
  mc_version = get_mc_version(cfg)
  jar_out = get_server_jar_out(cfg)
  keep_ver_jar = get_keep_versioned_jar(cfg)

  ensure_dir(dest_dir)
  ensure_dir(os.path.join(dest_dir, "plugins"))
  ensure_dir(bak_root(dest_dir))

  old_state = None
  if require_state:
    spath = state_path(dest_dir)
    if not os.path.exists(spath):
      raise SystemExit("ERROR: state.toml not found. Run install first.")
    old_state = load_toml(spath)

  step("Resolving latest versions")
  sp = resolve_server_plan(platform, mc_version, ua)

  tmap = get_targets(cfg)
  names = select_targets(cfg)
  plans: List[TargetPlan] = [resolve_target_plan(n, tmap[n], mc_version, ua) for n in names if n in tmap]

  jar_out_abs = os.path.join(dest_dir, jar_out)
  verfile = _server_versioned_jar_path(dest_dir, platform, sp.server_label) if keep_ver_jar else jar_out_abs

  server_need = True
  plugin_need: Dict[str, bool] = {p.name: True for p in plans}

  if require_state and isinstance(old_state, dict):
    ins = old_state.get("installed", {})
    ins_server = ins.get("server", {}) if isinstance(ins, dict) else {}
    if isinstance(ins_server, dict) and str(ins_server.get("server_version", "")) == sp.server_label and os.path.exists(jar_out_abs):
      server_need = False

    ins_targets = ins.get("targets", {}) if isinstance(ins, dict) else {}
    if isinstance(ins_targets, dict):
      for p in plans:
        it = ins_targets.get(p.name, {})
        if not isinstance(it, dict):
          continue
        if p.ttype == "modrinth":
          if str(it.get("resolved_id", "")) and str(it.get("resolved_id", "")) == p.latest_id and os.path.exists(os.path.join(dest_dir, p.out)):
            plugin_need[p.name] = False
        else:
          if str(it.get("resolved_version", "")) == p.latest_label and os.path.exists(os.path.join(dest_dir, p.out)):
            plugin_need[p.name] = False

    info(f"server: {'update' if server_need else 'up-to-date'} (latest={sp.server_label})")
    for p in plans:
      info(f"{p.name}: {'update' if plugin_need[p.name] else 'up-to-date'} (latest={p.latest_label})")

  backup_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
  need_any = (not require_state) or server_need or any(plugin_need.values())
  if need_any:
    step(f"Backup id: {backup_id}")
    if (not require_state) or server_need:
      backup_move(dest_dir, backup_id, jar_out_abs)
    for p in plans:
      if (not require_state) or plugin_need[p.name]:
        backup_move(dest_dir, backup_id, os.path.join(dest_dir, p.out))

  if (not require_state) or server_need:
    down(f"server: {sp.server_label}")
    http_download(sp.url, verfile, ua)
    if keep_ver_jar:
      try:
        if os.path.exists(jar_out_abs) or os.path.islink(jar_out_abs):
          os.remove(jar_out_abs)
        os.symlink(os.path.basename(verfile), jar_out_abs)
      except OSError:
        shutil.copy2(verfile, jar_out_abs)

  for p in plans:
    if require_state and not plugin_need[p.name]:
      continue
    if not p.url:
      raise SystemExit(f"ERROR: no download url for target '{p.name}' (latest={p.latest_label})")
    out_abs = os.path.join(dest_dir, p.out)
    down(f"{p.name}: {p.latest_label}")
    http_download(p.url, out_abs, ua)

  if force_eula_true:
    set_eula_true(dest_dir)
    ok("Wrote eula.txt (eula=true)")

  st = {"schema": 1, "checked_at": now_iso_jst(), "installed": {"server": {}, "targets": {}}}
  st["installed"]["server"] = {
    "type": platform,
    "mc_version": mc_version,
    "server_version": sp.server_label,
    "url": sp.url,
    "jar_path": os.path.basename(verfile) if keep_ver_jar else os.path.basename(jar_out_abs),
    "sha256": sha256_file(verfile if os.path.exists(verfile) else jar_out_abs),
    "installed_at": now_iso_jst(),
  }
  for p in plans:
    out_abs = os.path.join(dest_dir, p.out)
    if os.path.exists(out_abs):
      st["installed"]["targets"][p.name] = {
        "type": p.ttype,
        "resolved_id": p.latest_id,
        "resolved_version": p.latest_label,
        "url": p.url,
        "out": p.out,
        "sha256": sha256_file(out_abs),
        "installed_at": now_iso_jst(),
      }
  save_state(dest_dir, st)

  ok("Done.")
  info(f"SERVER_DIR: {dest_dir}")
  info(f"State     : {state_path(dest_dir)}")
  if need_any:
    info(f"Backup    : {os.path.join(bak_root(dest_dir), backup_id)}")
  return 0

def cmd_install(platform: str, mc_version: str, config_path: str, force_eula_true: bool) -> int:
  ensure_config_for_install(platform, mc_version, config_path)
  cfg = load_toml(config_path)
  return _apply_install_or_update(cfg, config_path, require_state=False, force_eula_true=force_eula_true)

def cmd_update(config_path: str) -> int:
  if not os.path.exists(config_path):
    raise SystemExit(f"ERROR: config not found: {config_path}")
  cfg = load_toml(config_path)
  return _apply_install_or_update(cfg, config_path, require_state=True, force_eula_true=False)

# =========================================================
# CLI
# =========================================================
def build_argparser() -> argparse.ArgumentParser:
  ap = argparse.ArgumentParser(prog="mcsm")
  ap.add_argument("--config", default="mcsm.toml", help="path to mcsm.toml (default: ./mcsm.toml)")

  cmd = ap.add_subparsers(dest="cmd", required=True)

  p_list = cmd.add_parser("list", help="show latest server/plugins for (optional) mc_version")
  p_list.add_argument("platform", choices=["purpur", "paper"])
  p_list.add_argument("mc_version", nargs="?", default=None)

  p_install = cmd.add_parser("install", help="create mcsm.toml if missing, lock mc_version, and install server/plugins")
  p_install.add_argument("platform", choices=["purpur", "paper"])
  p_install.add_argument("mc_version")
  p_install.add_argument("--force-eula-true", action="store_true", help="write eula=true to eula.txt")

  cmd.add_parser("update", help="update server/plugins using mcsm.toml (requires state.toml)")

  p_init = cmd.add_parser("init", help="write a commented template mcsm.toml for customization (optional)")
  p_init.add_argument("platform", choices=["purpur", "paper"])
  p_init.add_argument("--force", action="store_true", help="overwrite if exists")

  cmd.add_parser("status", help="show installed server/plugins status from state.toml (offline)")

  cmd.add_parser("setup", help="generate launcher scripts and desktop/startmenu shortcut (OS-specific)")
  cmd.add_parser("addsrv", help="register server as a user service (systemd/launchd/startup)")
  cmd.add_parser("rmsrv", help="remove user service registration")

  return ap

def main(argv: List[str]) -> int:
  ns = build_argparser().parse_args(argv)
  config_path = ns.config
  cfg = load_toml(config_path) if os.path.exists(config_path) else None

  if ns.cmd == "list":
    return cmd_list(ns.platform, ns.mc_version, config_path)
  if ns.cmd == "install":
    return cmd_install(ns.platform, ns.mc_version, config_path, ns.force_eula_true)
  if ns.cmd == "update":
    return cmd_update(config_path)
  if ns.cmd == "status":
    return cmd_status(config_path)
  if ns.cmd == "init":
    return cmd_init(ns.platform, config_path, ns.force)

  # setup/addsrv/rmsrv need config
  if cfg is None:
    raise SystemExit(f"ERROR: config not found: {config_path}")
  dest_dir = get_dest_dir(cfg, config_path)

  if ns.cmd == "setup":
    return cmd_setup(cfg, dest_dir)
  if ns.cmd == "addsrv":
    return cmd_addsrv(cfg, dest_dir)
  if ns.cmd == "rmsrv":
    return cmd_rmsrv(cfg, dest_dir)

  raise SystemExit("ERROR: unknown command")

if __name__ == "__main__":
  try:
    sys.exit(main(sys.argv[1:]))
  except KeyboardInterrupt:
    err("Interrupted.")
    sys.exit(130)
