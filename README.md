# Persona

A keyboard-driven RPG character sheet for the terminal, with a cyberpunk look.
Sheets are plain Markdown, so a character is readable (and editable) in any
text editor as well as in the app.

Built for a Raspberry Pi 400 cyberdeck, but it runs in any terminal: over SSH,
on a desktop, or on a Linux console.

## Requirements

- Python 3.8+ (standard library only for the sheet itself)
- For the second-display HUD: Pillow and a monospace font
  (`sudo apt install python3-pil fonts-dejavu-core`)
- Windows: `pip install windows-curses`

## Usage

```
./persona.py                       pick a system and character
./persona.py -g sra2 -n "Name"     new character from a template
./persona.py -c name               open a character
./persona.py --list                list templates and characters
```

Options: `--theme green|amber|cyan`, `--ascii`, `--no-splash`, `--dir PATH`,
`--hud [FB]`, `--hud-font PX`. Press `?` in the app for keys.

Every change saves immediately; a `.bak` copy is made each time a sheet opens.
`u` undoes, and `e` opens the whole sheet in `$EDITOR`.

## Where things live

| What | Where |
|---|---|
| Program | this repo (`persona.py`) |
| Templates | `templates/` here, plus `~/.local/share/persona/templates/` |
| Characters | `~/.local/share/persona/characters/` (not in the repo) |

Set `PERSONA_HOME` or pass `--dir` to keep characters somewhere else.

## Templates

A game system is a Markdown template, `<id>.cstpl.md`; a character is a
filled-in copy of it, `<name>.cs.md`.

```
<!-- persona: system="Name" id=abbr version=1 -->   template metadata
# [text]                                            character name
## Section                                          one screen (keys 1-9, 0)
<!-- persona: hud=Other Section -->                 what the HUD shows here
### Panel                                           sub-panel
**Key:** [type]                                     field
<!-- persona: repeat hudpool=Rating+Attr -->        growable table + HUD dice pool
| Col | Col |                                       table
- [type]                                            list item
[longtext]                                          free text (alone under its heading)
```

Types: `[text]` `[longtext]` `[tags]` `[int]` `[int:N]` (default)
`[int:LO-HI]` `[num]` `[num:N]` `[enum:a/b/c]` `[boxes:N]`.
A character file may carry more boxes than the template (e.g. from a quality).

Adding fields to a template is safe: existing characters gain them the next
time they're opened.

## Cyberdeck setup (Pi 400)

- **HUD:** `--hud` draws a live summary on an SPI TFT (found by driver name,
  `fb_*`). Which section it shows follows the `hud=` rules in the template.
- **Console placement:** `deck/persona-console.service` moves the text console
  to the HDMI screen when one is connected, otherwise to the TFT, regardless of
  which display registers first. Install instructions are in the file.
- **Boot straight into the sheet:** enable console autologin, then in `~/.bashrc`:

  ```
  [ "$(tty)" = "/dev/tty1" ] && ~/persona/persona.py -c name --theme cyan --hud
  ```
