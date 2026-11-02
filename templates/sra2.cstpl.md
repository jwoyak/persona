<!-- persona: system="Shadowrun Anarchy 2.0" id=sra2 version=1 -->
# [text]

## Profile
**Real Name:** [text]
**Metatype:** [enum:Human/Elf/Dwarf/Ork/Troll]
**Awakening:** [enum:Mundane/Awakened/Emerged]
**Tags:** [tags]

### Attributes
**Strength:** [int:1-6]
**Agility:** [int:1-6]
**Willpower:** [int:1-6]
**Logic:** [int:1-6]
**Charisma:** [int:1-6]
**Edge:** [int:1-6]
**Essence:** [num:6]

### Behaviors
<!-- persona: repeat -->
- [text]

### Cues
<!-- persona: repeat -->
- [text]

## Status
<!-- persona: hud=Skills -->
**Anarchy:** [int:0]
**Armor Worn:** [enum:None/Main/Alternate]

### Wounds
| Track | Light | Serious | Incap |
|---|---|---|---|
| Physical | [boxes:2] | [boxes:1] | [boxes:1] |
| Mental | [boxes:2] | [boxes:1] | [boxes:1] |
| Matrix | [boxes:2] | [boxes:1] | [boxes:1] |

### Thresholds
| Armor | Light | Serious | Incap |
|---|---|---|---|
| No armor | [int] | [int] | [int] |
| Main armor | [int] | [int] | [int] |
| Alternate armor | [int] | [int] | [int] |
| Mental | [int] | [int] | [int] |

## Skills
<!-- persona: repeat hudpool=Rating+Attr -->
| Skill | Rating | Specialization | Attr |
|---|---|---|---|
| [text] | [int:0-6] | [text] | [enum:STR/AGI/WIL/LOG/CHA] |

### Knowledge
<!-- persona: repeat -->
- [text]

## Feats
### Traits
<!-- persona: repeat -->
| Trait | Effect |
|---|---|
| [text] | [text] |

### Shadow Amps
<!-- persona: repeat -->
| Amp | Rating | Essence | Effect |
|---|---|---|---|
| [text] | [int] | [num] | [text] |

## Matrix
**Cyberdeck:** [text]
**Attack:** [int:0]
**Firewall:** [int:0]

### Programs
<!-- persona: repeat -->
| Program | Effect |
|---|---|
| [text] | [text] |

## Rigging
**Control Rig:** [text]

### Vehicles & Drones
<!-- persona: repeat -->
| Name | Type | Pilot | Body | Handling | Speed | Armor | Light | Serious | Incap | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| [text] | [text] | [int] | [int] | [int] | [int] | [int] | [boxes:2] | [boxes:1] | [boxes:1] | [text] |

## Gear
**Nuyen:** [int:0]

### Weapons & Spells
<!-- persona: repeat -->
| Name | Type | DV | Melee | Short | Medium | Long | Skill |
|---|---|---|---|---|---|---|---|
| [text] | [text] | [text] | [enum:OK/Dis/—] | [enum:OK/Dis/—] | [enum:OK/Dis/—] | [enum:OK/Dis/—] | [text] |

### Armor
<!-- persona: repeat -->
| Armor | Rating | Notes |
|---|---|---|
| [text] | [int:0-5] | [text] |

### Equipment
<!-- persona: repeat -->
| Item | Notes |
|---|---|
| [text] | [text] |

## Contacts
<!-- persona: repeat -->
| Name | Role | Loyalty | Notes |
|---|---|---|---|
| [text] | [text] | [int] | [text] |

## Notes
[longtext]
