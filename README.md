[<img src="images/minecraft.svg" width="64" alt="Minecraft icon">](https://www.minecraft.net/) [<img src="images/modrinth.webp" width="64" alt="Modrinth icon">](https://modrinth.com/) [<img src="images/purpur.svg" width="64" alt="Modrinth icon">](https://purpurmc.org/) [<img src="images/papermc.svg" width="64" alt="Paper icon">](https://papermc.io/)

# mcsm (Minecraft Server & Plugin Manager) 🧰

mcsm is a CLI tool that installs and updates **Purpur / Paper** server JARs and key plugins. 🚀

## Requirements 🧪

- Python **3.11+** recommended (`tomllib`)
- Python **3.10**: `pip install tomli`

Network access is required (Purpur API, PaperMC API, GeyserMC API, Modrinth API). 🌐

## Quick start ⚡

```bash
mkdir -p ~/servers/myserver
cd ~/servers/myserver
python3 mcsm.py install purpur 1.21.11
```

To server and plugins update:

```bash
python3 mcsm.py update
```

## Supported ✅

### Server
- Purpur 🟪
- Paper 📄

### Plugins
- GeyserMC 🌉 (GeyserMC API)
- Floodgate 🔐 (GeyserMC API)
- ViaVersion 🧭 (Modrinth API)

Other plugins can be added by editing `mcsm.toml` (examples are included but commented out). ✍️
You can check and add plugin's interface name of Modrinth API.

## Commands 🧾

- `init <platform>`
- `list <platform> [mc_version]`
- `install <platform> <mc_version>`
- `update`
- `setup` / `addsrv` / `rmsrv`
- `shortcuts list`

## Optional: Install mcsm globally 🧩

You may place `mcsm.py` in a directory on your PATH (e.g. `~/.local/bin`) to call it from anywhere.

```bash
install -m 755 mcsm.py ~/.local/bin/mcsm
```

After that, run `mcsm` from any server directory:

```bash
cd ~/servers/myserver
mcsm install purpur 1.21.11
mcsm update
```

## Shortcuts & auto-start 🖥️

- Linux: `setup` creates a launcher script + `.desktop`. `addsrv` registers a **systemd --user** service.
- Windows: `setup` creates a Start Menu `.bat`. `addsrv` copies it into Startup.
- macOS: `setup` creates a `.command` in `~/Applications`. `addsrv` registers a **LaunchAgent** (launchd).

## License 📜

MIT License

# Donation!

[![Buy Me a Coffee](https://img.shields.io/badge/buy_me_an-emerald_coffee!-3C9A3C?style=for-the-badge&logo=minecraft)](https://coff.ee/azo234) ☕💚

[![Sponsor with Diamond](https://img.shields.io/badge/please-diamond_sponsor_me!-00ccff?style=for-the-badge&logo=minecraft)](https://github.com/sponsors/azo234) 💎✨
