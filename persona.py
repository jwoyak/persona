#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Persona - a console RPG character sheet
=======================================

A keyboard-driven, terminal character sheet whose data lives in Markdown.

  * A game system is described by a template:   <id>.cstpl.md
  * A character is a filled-in copy of it:       <name>.cs.md

Template grammar (see sra2.cstpl.md for a full example):

  <!-- persona: system="Name" id=abbr version=1 -->  template metadata
  # [text]                                           character name
  ## Section                                         one screen (keys 1-9, 0)
  ### Sub-panel  /  #### Sub-sub-panel               panels inside a screen
  **Key:** [type]                                    key/value field
  | Col | Col |  + |---|---| + rows                  table
  - [type]                                           list item
  [longtext]                                         free text (alone under its heading)
  <!-- persona: repeat -->                           next table/list can grow

  Types: [text] [longtext] [tags] [int] [int:N default] [int:LO-HI]
         [num] [num:N default] [enum:a/b/c] [boxes:N]

  In a fixed (non-repeat) table, a literal first cell (e.g. "Physical") is
  the row's label. In a repeat table, the placeholder row is the prototype.

Usage:
  persona.py                      pick a system and character interactively
  persona.py -g sra2 -n "Shiv"    new character from template sra2
  persona.py -c shiv              open an existing character
  persona.py --list               list templates and characters

Characters live in ~/.local/share/persona/characters/ (override with --dir or
$PERSONA_HOME). Templates are read from that folder's templates/, from the
templates/ folder next to this script, and from the current directory. Requires Python 3.8+; on Windows: pip install windows-curses
"""
from __future__ import annotations

import argparse
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

os.environ.setdefault("ESCDELAY", "25")  # snappy Esc key

try:
    import curses
except ImportError:  # pragma: no cover
    sys.exit("curses is not available. On Windows run: pip install windows-curses")

VERSION = "0.1.0"
SCRIPT_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Markdown grammar
# --------------------------------------------------------------------------
TYPES = "text|longtext|int|num|enum|tags|boxes"
PH_RE = re.compile(r"^\[(%s)(?::([^\]]*))?\]$" % TYPES)
PH_ANY = re.compile(r"\[(%s)(?::([^\]]*))?\]" % TYPES)
HEAD_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)\s*$")
KV_RE = re.compile(r"^\*\*([^*]+?):\*\*\s*(.*?)\s*$")
LIST_RE = re.compile(r"^[-*+](?:[ \t]+(.*))?$")
SEP_RE = re.compile(r"^\|?(\s*:?-+:?\s*\|)*\s*:?-+:?\s*\|?$")
DIR_RE = re.compile(r"^<!--\s*(?:persona|crpgcs):(.*?)-->$")  # crpgcs: = old name
FILLED = set("■xX#▓█●")
BOX_FULL, BOX_EMPTY = "■", "□"


def box_count(v):
    return sum(1 for c in v if c in FILLED)


def box_total(ft, v):
    """Track length: the template's count, or more if the character file has
    extra boxes (e.g. a quality that adds a wound box)."""
    return max(ft.n, sum(1 for c in (v or "") if c in FILLED or c == BOX_EMPTY))


def box_str(m, n):
    return BOX_FULL * m + BOX_EMPTY * (n - m)


def clean(v):
    v = (v or "").strip()
    return "" if PH_RE.match(v) else v


def split_row(text):
    s = text.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [p.strip().replace("\\|", "|") for p in re.split(r"(?<!\\)\|", s)]


def join_row(cells):
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def sep_row(n):
    return "|" + "---|" * n


def _pad(cells, n):
    return (list(cells) + [""] * n)[:n]


def _tokens(s):
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


def _kv_tokens(toks):
    out = {}
    for t in toks:
        k, sep, v = t.partition("=")
        if sep:
            out[k.strip().lower()] = v.strip()
    return out


class FType:
    """A placeholder type such as [int:1-6] or [boxes:3]."""

    def __init__(self, kind, arg=""):
        self.kind = kind
        self.arg = (arg or "").strip()
        self.lo = self.hi = None
        self.default = ""
        self.options = []
        self.n = 0
        a = self.arg
        if kind == "int":
            m = re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", a)
            if m:
                self.lo, self.hi = sorted((int(m[1]), int(m[2])))
            elif re.fullmatch(r"-?\d+", a):
                self.default = a
        elif kind == "num":
            try:
                float(a)
                self.default = a
            except ValueError:
                pass
        elif kind == "enum":
            self.options = [o.strip() for o in a.split("/") if o.strip()]
        elif kind == "boxes":
            self.n = int(a) if a.isdigit() and int(a) > 0 else 1
            self.default = box_str(0, self.n)
        elif kind == "text":
            self.default = a

    @classmethod
    def parse(cls, s):
        m = PH_RE.match((s or "").strip())
        return cls(m[1], m[2]) if m else None


# --------------------------------------------------------------------------
# Document model: a list of mutable Line objects, edited in place
# --------------------------------------------------------------------------
class Line:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


class Doc:
    def __init__(self, text):
        self.lines = [Line(t) for t in text.replace("\r\n", "\n").split("\n")]

    def text(self):
        return "\n".join(l.text for l in self.lines)

    def idx(self, line):
        for i, l in enumerate(self.lines):
            if l is line:
                return i
        raise ValueError("line not in document")

    def insert(self, i, lines):
        self.lines[i:i] = lines

    def remove(self, line):
        del self.lines[self.idx(line)]


class Block:
    def __init__(self, kind, lines):
        self.kind = kind          # kv | table | list | para
        self.lines = lines
        self.repeat = False
        self.key = self.val = None
        self.header = self.sep = None
        self.rows = []
        self.opts = {}


class Ctx:
    """Content under one heading (an H2 section or an H3/H4 sub-panel)."""

    def __init__(self, heading, level, h2, sub):
        self.heading = heading
        self.level = level
        self.h2 = h2
        self.sub = sub
        self.blocks = []
        self.meta = {}

    @property
    def key(self):
        return (self.h2.lower(), self.sub.lower())


class Parsed:
    def __init__(self):
        self.h1 = None
        self.ctxs = {}
        self.order = []
        self.directives = []


def parse(doc):
    P = Parsed()
    L = doc.lines
    root = Ctx(None, 0, "", "")
    P.ctxs[root.key] = root
    P.order.append(root)
    state = {"cur": root, "rep": False, "opts": {}}
    h2 = ""

    def add(b):
        b.repeat = state["rep"]
        b.opts = state["opts"]
        state["rep"] = False
        state["opts"] = {}
        state["cur"].blocks.append(b)

    i = 0
    while i < len(L):
        t = L[i].text
        s = t.strip()
        m = HEAD_RE.match(t)
        if m:
            lvl, title = len(m[1]), m[2].strip()
            if lvl == 1:
                if P.h1 is None:
                    P.h1 = L[i]
            else:
                if lvl == 2:
                    h2, sub = title, ""
                else:
                    sub = title
                c = Ctx(L[i], lvl, h2, sub)
                if c.key not in P.ctxs:
                    P.ctxs[c.key] = c
                    P.order.append(c)
                state["cur"] = c
            state["rep"] = False
            state["opts"] = {}
            i += 1
            continue
        dm = DIR_RE.match(s)
        if dm:
            toks = _tokens(dm[1])
            P.directives.append((L[i], toks))
            opts = _kv_tokens(toks)
            if "hud" in opts:
                state["cur"].meta["hud"] = opts.pop("hud")
            if "repeat" in toks:
                state["rep"] = True
            state["opts"].update(opts)
            i += 1
            continue
        if s.startswith("<!--"):
            while i < len(L) and "-->" not in L[i].text:
                i += 1
            i += 1
            continue
        if s.startswith("|"):
            j = i
            while j < len(L) and L[j].text.strip().startswith("|"):
                j += 1
            tl = list(L[i:j])
            if len(tl) >= 2 and SEP_RE.match(tl[1].text.strip()):
                b = Block("table", tl)
                b.header, b.sep, b.rows = tl[0], tl[1], tl[2:]
            else:
                b = Block("para", tl)
            add(b)
            i = j
            continue
        if s and t[:1] in "-*+" and LIST_RE.match(s):
            j = i
            while j < len(L) and L[j].text[:1] in "-*+" and L[j].text.strip() \
                    and LIST_RE.match(L[j].text.strip()):
                j += 1
            add(Block("list", list(L[i:j])))
            i = j
            continue
        km = KV_RE.match(s)
        if km:
            b = Block("kv", [L[i]])
            b.key, b.val = km[1].strip(), km[2]
            add(b)
            i += 1
            continue
        if not s:
            i += 1
            continue
        j = i
        while j < len(L):
            tt = L[j].text
            ss = tt.strip()
            if (not ss or HEAD_RE.match(tt) or ss.startswith(("|", "<!--"))
                    or KV_RE.match(ss) or (tt[:1] in "-*+" and LIST_RE.match(ss))):
                break
            j += 1
        add(Block("para", list(L[i:j])))
        i = j
    return P


# --------------------------------------------------------------------------
# Template schema
# --------------------------------------------------------------------------
class KVS:
    def __init__(self, key, ft):
        self.key, self.ft = key, ft


class TS:
    def __init__(self, headers, repeat, proto, fixed, pool=None):
        self.headers, self.repeat, self.proto, self.fixed = headers, repeat, proto, fixed
        self.pool = pool  # e.g. "Rating+Attr": HUD shows a computed dice pool column


class LS:
    def __init__(self, ft, repeat):
        self.ft, self.repeat = ft, repeat


class LongS:
    def __init__(self, ft):
        self.ft = ft


class CtxS:
    def __init__(self, h2, sub, level):
        self.h2, self.sub, self.level = h2, sub, level
        self.blocks = []


class Schema:
    def __init__(self, path):
        self.path = Path(path)
        text = self.path.read_text(encoding="utf-8")
        P = parse(Doc(text))
        meta = {}
        for _, toks in P.directives:
            d = _kv_tokens(toks)
            if "id" in d or "system" in d:
                meta = d
                break
        name = self.path.name
        stem = name[: -len(".cstpl.md")] if name.endswith(".cstpl.md") else self.path.stem
        self.id = meta.get("id") or stem
        self.system = meta.get("system") or self.id
        self.version = meta.get("version", "1")
        self.sections = []
        self.hud = {}  # section title (lower) -> section to show on the HUD
        bytitle = {}
        for ctx in P.order:
            if not ctx.h2:
                continue
            if not ctx.sub and "hud" in ctx.meta:
                self.hud[ctx.h2.lower()] = ctx.meta["hud"]
            cs = CtxS(ctx.h2, ctx.sub, ctx.level)
            for b in ctx.blocks:
                if b.kind == "kv":
                    ft = FType.parse(b.val)
                    if ft:
                        cs.blocks.append(KVS(b.key, ft))
                elif b.kind == "table":
                    hdr = split_row(b.header.text)
                    n = len(hdr)
                    rows = [_pad(split_row(r.text), n) for r in b.rows]
                    if b.repeat:
                        first = rows[0] if rows else [""] * n
                        proto = [FType.parse(c) or FType("text", c) for c in first]
                        cs.blocks.append(TS(hdr, True, proto, [], b.opts.get("hudpool")))
                    else:
                        fixed = [[FType.parse(c) or c for c in r] for r in rows]
                        cs.blocks.append(TS(hdr, False, None, fixed, b.opts.get("hudpool")))
                elif b.kind == "list":
                    m = LIST_RE.match(b.lines[0].text.strip())
                    ft = FType.parse((m[1] if m else "") or "") or FType("text")
                    cs.blocks.append(LS(ft, b.repeat))
                elif b.kind == "para" and len(b.lines) == 1:
                    ft = FType.parse(b.lines[0].text)
                    if ft and ft.kind == "longtext":
                        cs.blocks.append(LongS(ft))
            k = ctx.h2.lower()
            if k not in bytitle:
                bytitle[k] = (ctx.h2, [])
                self.sections.append(bytitle[k])
            bytitle[k][1].append(cs)


def instantiate(schema, name):
    """Turn a template into a fresh character file's text."""
    doc = Doc(schema.path.read_text(encoding="utf-8"))
    P = parse(doc)
    drop = set()
    for ctx in P.order:
        for b in ctx.blocks:
            if b.repeat and b.kind == "table":
                drop.update(id(l) for l in b.rows)
            elif b.repeat and b.kind == "list":
                drop.update(id(l) for l in b.lines)
    header = "<!-- persona: template=%s version=%s -->" % (schema.id, schema.version)
    out, meta_done = [], False
    for l in doc.lines:
        if id(l) in drop:
            continue
        t = l.text
        dm = DIR_RE.match(t.strip())
        if dm and not meta_done:
            d = _kv_tokens(_tokens(dm[1]))
            if "id" in d or "system" in d:
                out.append(header)
                meta_done = True
                continue
        if l is P.h1:
            out.append("# " + name)
            continue
        out.append(PH_ANY.sub(lambda m: FType(m[1], m[2]).default, t).rstrip())
    if not meta_done:
        out.insert(0, header)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Binding a character file to its template
# --------------------------------------------------------------------------
class Ref:
    """Handle to one editable value."""

    def __init__(self, label, ftype, get, set_):
        self.label, self.ftype = label, ftype
        self._g, self._s = get, set_

    def get(self):
        return clean(self._g())

    def set(self, v):
        self._s(v)


def kv_ref(line, key, ft):
    def g():
        m = KV_RE.match(line.text.strip())
        return m[2] if m else ""

    def s(v):
        line.text = ("**%s:** %s" % (key, v)).rstrip()

    return Ref(key, ft, g, s)


class LongRef(Ref):
    def __init__(self, sheet, heading, ft, label):
        self.sheet, self.heading, self.ftype, self.label = sheet, heading, ft, label

    def _range(self):
        s, e = self.sheet.span(self.heading)
        L = self.sheet.doc.lines
        while s < e and (not L[s].text.strip() or L[s].text.strip().startswith("<!--")):
            s += 1
        while e > s and not L[e - 1].text.strip():
            e -= 1
        return s, e

    def get(self):
        s, e = self._range()
        out = []
        for l in self.sheet.doc.lines[s:e]:
            t = l.text
            out.append(t[1:] if t.startswith("\\#") else t)
        return clean("\n".join(out)) if len(out) == 1 else "\n".join(out).strip()

    def set(self, v):
        s, e = self._range()
        body = []
        if v.strip():
            body = [("\\" + t if HEAD_RE.match(t) else t) for t in v.rstrip().split("\n")]
        self.sheet.doc.lines[s:e] = [Line(t) for t in body]


class TableBind:
    def __init__(self, sheet, ts, blk):
        self.sheet, self.ts, self.repeat = sheet, ts, ts.repeat
        self.header, self.sep, self.rows = blk.header, blk.sep, list(blk.rows)
        hdr = split_row(self.header.text)
        low = [h.lower() for h in hdr]
        sl = [h.lower() for h in ts.headers]
        if low[: len(sl)] != sl:  # template columns changed: realign by name
            extras = [h for h in hdr if h.lower() not in sl]
            newh = list(ts.headers) + extras
            for r in self.rows:
                c = split_row(r.text)
                d = {low[k]: (c[k] if k < len(c) else "") for k in range(len(low))}
                r.text = join_row([d.get(h.lower(), "") for h in newh])
            self.header.text = join_row(newh)
            self.sep.text = sep_row(len(newh))
            sheet.migrated = True
            hdr = newh
        self.headers = hdr
        n, ns = len(hdr), len(ts.headers)
        if ts.repeat:
            self.proto = list(ts.proto) + [None] * (n - ns)
            self._types = None
        else:
            self._types = self._bind_fixed(n, ns)
        for i in range(len(self.rows)):  # draw empty box tracks
            for j, ft in enumerate(self.types(i)):
                if ft and ft.kind == "boxes" and not clean(self.cells(i)[j]):
                    self.ref(i, j).set(ft.default)
                    sheet.migrated = True

    def _bind_fixed(self, n, ns):
        fixed = self.ts.fixed
        conv = lambda fr: [x if isinstance(x, FType) else None for x in fr] + [None] * (n - ns)
        label = lambda fr: fr[0].lower() if fr and isinstance(fr[0], str) else None
        types, matched = [], set()
        for k, r in enumerate(self.rows):
            c = split_row(r.text)
            lab = c[0].lower() if c else ""
            found = None
            for fi, fr in enumerate(fixed):
                if fi in matched:
                    continue
                fl = label(fr)
                if (fl is not None and fl == lab) or (fl is None and fi == k):
                    found = fi
                    break
            if found is None:
                types.append([None] * n)
            else:
                matched.add(found)
                types.append(conv(fixed[found]))
        for fi, fr in enumerate(fixed):
            if fi in matched:
                continue
            line = Line(join_row([c if isinstance(c, str) else c.default for c in fr]
                                 + [""] * (n - ns)))
            anchor = self.rows[-1] if self.rows else self.sep
            self.sheet.doc.insert(self.sheet.doc.idx(anchor) + 1, [line])
            self.rows.append(line)
            types.append(conv(fr))
            self.sheet.migrated = True
        return types

    def types(self, i):
        return self.proto if self.repeat else self._types[i]

    def cells(self, i):
        return _pad(split_row(self.rows[i].text), len(self.headers))

    def ref(self, i, j):
        ts = self.types(i)
        if j >= len(ts) or ts[j] is None:
            return None
        line, n = self.rows[i], len(self.headers)
        first = self.cells(i)[0]
        lab = self.headers[j]
        if j > 0 and clean(first):
            lab = "%s › %s" % (first, lab)

        def g():
            return _pad(split_row(line.text), n)[j]

        def s(v):
            cc = _pad(split_row(line.text), n)
            cc[j] = v
            line.text = join_row(cc)

        return Ref(lab, ts[j], g, s)

    def add(self):
        line = Line(join_row([ft.default if ft else "" for ft in self.proto]))
        anchor = self.rows[-1] if self.rows else self.sep
        self.sheet.doc.insert(self.sheet.doc.idx(anchor) + 1, [line])
        self.rows.append(line)

    def delete(self, i):
        self.sheet.doc.remove(self.rows[i])
        del self.rows[i]

    def move(self, i, d):
        j = i + d
        if 0 <= j < len(self.rows):
            self.rows[i].text, self.rows[j].text = self.rows[j].text, self.rows[i].text
            return j
        return i

    def is_empty(self, i):
        for j, ft in enumerate(self.types(i)):
            v = clean(self.cells(i)[j])
            if ft is None or not v:
                continue
            if ft.kind == "boxes" and box_count(v) == 0:
                continue
            return False
        return True


class ListBind:
    def __init__(self, sheet, ls, heading, items, label):
        self.sheet, self.ft, self.repeat = sheet, ls.ft, ls.repeat
        self.heading, self.items, self.label = heading, items, label

    def ref(self, i):
        line = self.items[i]

        def g():
            m = LIST_RE.match(line.text.strip())
            return (m[1] or "") if m else ""

        def s(v):
            line.text = "- " + v

        return Ref("%s %d" % (self.label, i + 1), self.ft, g, s)

    def add(self):
        line = Line("- " + self.ft.default)
        if self.items:
            self.sheet.doc.insert(self.sheet.doc.idx(self.items[-1]) + 1, [line])
        else:
            self.sheet.insert_block(self.heading, [line])
        self.items.append(line)

    def delete(self, i):
        self.sheet.doc.remove(self.items[i])
        del self.items[i]

    def move(self, i, d):
        j = i + d
        if 0 <= j < len(self.items):
            self.items[i].text, self.items[j].text = self.items[j].text, self.items[i].text
            return j
        return i

    def is_empty(self, i):
        return not self.ref(i).get()


class CtxView:
    def __init__(self, sub, level, items):
        self.sub, self.level, self.items = sub, level, items


class SecView:
    def __init__(self, title):
        self.title, self.ctxs = title, []


class Sheet:
    def __init__(self, schema, path):
        self.schema = schema
        self.path = Path(path)
        self.load_text(self.path.read_text(encoding="utf-8"))

    def load_text(self, text):
        self.doc = Doc(text)
        self.P = parse(self.doc)
        self.migrated = False
        self._bind()

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(self.doc.text(), encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    def backup(self):
        try:
            shutil.copy2(str(self.path), str(self.path.with_name(self.path.name + ".bak")))
        except OSError:
            pass

    # --- derived values (HUD) -----------------------------------------
    def attr_value(self, abbr):
        """Value of the numeric field whose name starts with abbr (LOG -> Logic)."""
        a = abbr.strip().upper()
        if not a:
            return None
        for sv in self.sections:
            for cv in sv.ctxs:
                for it in cv.items:
                    if (isinstance(it, Ref) and not isinstance(it, LongRef)
                            and it.ftype.kind in ("int", "num") and it.label.upper().startswith(a)):
                        try:
                            return int(float(it.get()))
                        except ValueError:
                            return None
        return None

    def pool(self, spec, t, i):
        """Sum of the named columns; a non-numeric cell (LOG) is looked up as an attribute."""
        hdr = [h.lower() for h in t.headers]
        cells, total = t.cells(i), 0
        for term in spec.split("+"):
            term = term.strip().lower()
            if term not in hdr:
                return ""
            v = clean(cells[hdr.index(term)])
            if not v:
                return ""
            try:
                total += int(float(v))
            except ValueError:
                av = self.attr_value(v)
                if av is None:
                    return ""
                total += av
        return str(total)

    # --- geometry -----------------------------------------------------
    def span(self, heading):
        L = self.doc.lines
        s = self.doc.idx(heading) + 1
        e = s
        while e < len(L) and not HEAD_RE.match(L[e].text):
            e += 1
        return s, e

    def content_end(self, heading):
        s, e = self.span(heading)
        L = self.doc.lines
        while e > s and not L[e - 1].text.strip():
            e -= 1
        return e

    def insert_block(self, heading, new):
        L = self.doc.lines
        i = self.content_end(heading)
        prev = L[i - 1].text.strip() if i > 0 else ""
        if prev and not HEAD_RE.match(L[i - 1].text) and not prev.startswith("<!--"):
            new = [Line("")] + new
        if i < len(L) and L[i].text.strip():
            new = new + [Line("")]
        self.doc.insert(i, new)

    # --- binding ------------------------------------------------------
    def _bind(self):
        P, S = self.P, self.schema
        want = "<!-- persona: template=%s version=%s -->" % (S.id, S.version)
        hdr = None
        for line, toks in P.directives:
            if "template" in _kv_tokens(toks):
                hdr = line
                break
        if hdr is None:
            self.doc.insert(0, [Line(want)])
            self.migrated = True
        elif hdr.text.strip() != want:
            hdr.text = want
            self.migrated = True
        if P.h1 is None:
            P.h1 = Line("# Unnamed")
            self.doc.insert(1, [P.h1, Line("")])
            self.migrated = True
        h1 = P.h1

        def gname():
            m = HEAD_RE.match(h1.text)
            return m[2].strip() if m else ""

        def sname(v):
            h1.text = "# " + (v.strip() or "Unnamed")

        self.name_ref = Ref("Name", FType("text"), gname, sname)
        self.sections = []
        for title, ctxs in S.sections:
            sv = SecView(title)
            for cs in ctxs:
                dctx = self._ctx(cs)
                used, items = set(), []
                for b in cs.blocks:
                    it = self._bind_block(b, dctx, used, cs)
                    if it is not None:
                        items.append(it)
                sv.ctxs.append(CtxView(cs.sub, cs.level, items))
            self.sections.append(sv)

    def _ctx(self, cs):
        key = (cs.h2.lower(), cs.sub.lower())
        if key in self.P.ctxs:
            return self.P.ctxs[key]
        L = self.doc.lines
        if not cs.sub:
            i = len(L)
            while i > 0 and not L[i - 1].text.strip():
                i -= 1
            new = Line("## " + cs.h2)
            self.doc.insert(i, [Line(""), new])
            level = 2
        else:
            parent = self._ctx(CtxS(cs.h2, "", 2))
            lo = self.doc.idx(parent.heading) + 1
            i = lo
            while i < len(L):
                m = HEAD_RE.match(L[i].text)
                if m and len(m[1]) <= 2:
                    break
                i += 1
            while i > lo and not L[i - 1].text.strip():
                i -= 1
            level = max(3, cs.level)
            new = Line("#" * level + " " + cs.sub)
            block = [Line(""), new]
            if i < len(L) and L[i].text.strip():
                block.append(Line(""))
            self.doc.insert(i, block)
        c = Ctx(new, level, cs.h2, cs.sub)
        self.P.ctxs[key] = c
        self.migrated = True
        return c

    def _bind_block(self, b, dctx, used, cs):
        free = [x for x in dctx.blocks if id(x) not in used]
        label = cs.sub or cs.h2
        if isinstance(b, KVS):
            for x in free:
                if x.kind == "kv" and x.key.lower() == b.key.lower():
                    used.add(id(x))
                    return kv_ref(x.lines[0], x.key, b.ft)
            line = Line(("**%s:** %s" % (b.key, b.ft.default)).rstrip())
            kvs = [x for x in dctx.blocks if x.kind == "kv"]
            if kvs:
                self.doc.insert(self.doc.idx(kvs[-1].lines[0]) + 1, [line])
            else:
                self.insert_block(dctx.heading, [line])
            nb = Block("kv", [line])
            nb.key = b.key
            dctx.blocks.append(nb)
            used.add(id(nb))
            self.migrated = True
            return kv_ref(line, b.key, b.ft)
        if isinstance(b, TS):
            sl = {h.lower() for h in b.headers}
            best, score = None, 0
            for x in free:
                if x.kind != "table":
                    continue
                sc = len({h.lower() for h in split_row(x.header.text)} & sl)
                if sc > score:
                    best, score = x, sc
            if best is None:
                lines = [Line(join_row(b.headers)), Line(sep_row(len(b.headers)))]
                if not b.repeat:
                    for r in b.fixed:
                        lines.append(Line(join_row(
                            [c if isinstance(c, str) else c.default for c in r])))
                self.insert_block(dctx.heading, lines)
                best = Block("table", lines)
                best.header, best.sep, best.rows = lines[0], lines[1], lines[2:]
                dctx.blocks.append(best)
                self.migrated = True
            used.add(id(best))
            return TableBind(self, b, best)
        if isinstance(b, LS):
            for x in free:
                if x.kind == "list":
                    used.add(id(x))
                    return ListBind(self, b, dctx.heading, list(x.lines), label)
            return ListBind(self, b, dctx.heading, [], label)
        if isinstance(b, LongS):
            return LongRef(self, dctx.heading, b.ft, label)
        return None


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def default_home():
    """~/.local/share/persona, unless characters still sit in a pre-repo folder."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    new = Path(base) / "persona"
    if not (new / "characters").is_dir():
        for old in (Path.home() / "crpgcs", Path.home() / "persona"):
            if (old / "characters").is_dir() and old.resolve() != SCRIPT_DIR:
                return old
    return new


def slugify(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "character"


def read_header(path):
    info = {"name": ""}
    try:
        with open(path, encoding="utf-8") as f:
            for _, line in zip(range(40), f):
                s = line.strip()
                dm = DIR_RE.match(s)
                if dm:
                    info.update(_kv_tokens(_tokens(dm[1])))
                m = HEAD_RE.match(line.rstrip("\n"))
                if m and len(m[1]) == 1 and not info["name"]:
                    info["name"] = m[2].strip()
    except OSError:
        pass
    return info


def find_templates(home):
    seen, out = set(), []
    for d in (home / "templates", SCRIPT_DIR / "templates", SCRIPT_DIR, Path.cwd()):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.cstpl.md")):
            try:
                s = Schema(p)
            except Exception as e:  # noqa: BLE001
                print("warning: skipping %s: %s" % (p, e), file=sys.stderr)
                continue
            if s.id.lower() not in seen:
                seen.add(s.id.lower())
                out.append(s)
    return out


def match_template(templates, key):
    if not key:
        return None
    k = key.lower()
    for t in templates:
        if t.id.lower() == k or t.path.name.lower() in (k, k + ".cstpl.md"):
            return t
    p = Path(key).expanduser()
    if p.is_file():
        return Schema(p)
    return None


def list_chars(home, tid=None):
    out = []
    for p in sorted((home / "characters").glob("*.cs.md")):
        info = read_header(p)
        if tid is None or info.get("template", "").lower() == tid.lower():
            out.append((p, info.get("name") or p.name, info.get("template", "?")))
    return out


def resolve_char(home, key):
    p = Path(key).expanduser()
    if p.is_file():
        return p
    k = key.lower()
    for path, name, _ in list_chars(home):
        stem = path.name[: -len(".cs.md")].lower()
        if k in (stem, name.lower(), slugify(key).lower()):
            return path
    return None


def create_character(schema, name, home):
    d = home / "characters"
    d.mkdir(parents=True, exist_ok=True)
    base = slugify(name)
    p, n = d / (base + ".cs.md"), 2
    while p.exists():
        p, n = d / ("%s-%d.cs.md" % (base, n)), n + 1
    p.write_text(instantiate(schema, name), encoding="utf-8")
    return p


def editor_cmd():
    e = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if e:
        return shlex.split(e)
    for c in ("nano", "vim", "vi", "notepad"):
        if shutil.which(c):
            return [c]
    return ["vi"]


# --------------------------------------------------------------------------
# Terminal UI
# --------------------------------------------------------------------------
GLYPHS_UNI = dict(h="─", v="│", tl="┌", tr="┐", bl="└", br="┘", full="■", empty="□",
                  mark="▸", more_l="‹", more_r="›", dot="·", logo="▓▒░", ell="…",
                  sub="»", bullet="•", up="▲", dn="▼", enter="⏎", tab="⇥", bar="│")
GLYPHS_ASCII = dict(h="-", v="|", tl="+", tr="+", bl="+", br="+", full="#", empty=".",
                    mark=">", more_l="<", more_r=">", dot=".", logo="##", ell="~",
                    sub=">>", bullet="*", up="^", dn="v", enter="Enter", tab="Tab", bar="|")


class Colors:
    def __init__(self, theme):
        A = curses
        self.norm = A.A_NORMAL
        self.dim = A.A_DIM
        self.head = self.val = self.dmg = self.acc = A.A_BOLD
        self.sel = self.title = A.A_REVERSE
        if not curses.has_colors():
            return
        main = {"green": A.COLOR_GREEN, "amber": A.COLOR_YELLOW, "cyan": A.COLOR_CYAN}[theme]
        head = A.COLOR_MAGENTA if theme == "cyan" else A.COLOR_CYAN
        val = A.COLOR_WHITE if theme == "amber" else A.COLOR_YELLOW
        bg = A.COLOR_BLACK
        A.init_pair(1, main, bg)
        A.init_pair(2, head, bg)
        A.init_pair(3, val, bg)
        A.init_pair(4, A.COLOR_RED, bg)
        A.init_pair(5, A.COLOR_BLACK, main)
        A.init_pair(6, A.COLOR_MAGENTA, bg)
        cp = A.color_pair
        self.norm, self.dim = cp(1), cp(1) | A.A_DIM
        self.head, self.val = cp(2) | A.A_BOLD, cp(3) | A.A_BOLD
        self.dmg, self.acc = cp(4) | A.A_BOLD, cp(6) | A.A_BOLD
        # Highlight: black on white (bright white where the terminal has 16+ colors;
        # the Linux console shows it as light gray). No bold: bold black turns gray.
        white = 15 if curses.COLORS >= 16 else A.COLOR_WHITE
        A.init_pair(7, A.COLOR_BLACK, white)
        self.title = cp(5)
        self.sel = cp(7)


ASCII = {"on": False}
ASCII_MAP = str.maketrans({"›": ">", "‹": "<", "…": "~", "⏎": "Enter", "⇥": "Tab",
                           "│": "|", "■": "#", "□": ".", "·": ".", "▲": "^", "▼": "v",
                           "↑": "^", "↓": "v", "»": ">>", "▸": ">", "•": "*", "—": "-"})


def put(scr, y, x, s, attr=0):
    if ASCII["on"] and s:
        s = s.translate(ASCII_MAP).encode("ascii", "replace").decode("ascii")
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w or x < 0 or not s:
        return
    s = s[: max(0, w - x)]
    try:
        scr.addstr(y, x, s, attr)
    except curses.error:
        pass


def fit(s, n):
    return s if len(s) <= n else (s[: max(0, n - 1)] + "…" if n > 1 else s[:n])


def line_edit(scr, C, label, init="", redraw=None):
    buf, pos = list(init), len(init)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            h, w = scr.getmaxyx()
            y = h - 1
            pre = " %s> " % label
            if len(pre) > w // 2:
                pre = pre[: w // 2 - 2] + "> "
            avail = max(1, w - len(pre) - 1)
            off = max(0, pos - avail + 1)
            put(scr, y, 0, " " * (w - 1), C.norm)
            put(scr, y, 0, pre, C.acc)
            put(scr, y, len(pre), "".join(buf[off:off + avail]), C.val)
            try:
                scr.move(y, min(w - 1, len(pre) + pos - off))
            except curses.error:
                pass
            scr.refresh()
            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return "".join(buf)
            if ch in ("\x1b", "\x03", "\x07"):
                if ch == "\x1b":
                    scr.nodelay(True)
                    try:
                        scr.get_wch()
                    except curses.error:
                        pass
                    scr.nodelay(False)
                return None
            if ch in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif ch in (curses.KEY_DC, "\x04"):
                if pos < len(buf):
                    del buf[pos]
            elif ch == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif ch in (curses.KEY_HOME, "\x01"):
                pos = 0
            elif ch in (curses.KEY_END, "\x05"):
                pos = len(buf)
            elif ch == "\x15":
                buf, pos = [], 0
            elif ch == curses.KEY_RESIZE:
                if redraw:
                    redraw()
            elif isinstance(ch, str) and ch.isprintable():
                buf.insert(pos, ch)
                pos += 1
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def pick(scr, C, g, title, items):
    i, top = 0, 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        put(scr, 0, 0, (" %s PERSONA // %s" % (g["logo"], title)).ljust(w), C.title)
        H = max(1, h - 4)
        if i < top:
            top = i
        if i >= top + H:
            top = i - H + 1
        for k in range(H):
            idx = top + k
            if idx >= len(items):
                break
            sel = idx == i
            put(scr, 2 + k, 1, (g["mark"] + " " if sel else "  ") + items[idx],
                C.sel if sel else C.norm)
        put(scr, h - 1, 0, " ↑↓ select  %s open  q quit" % g["enter"], C.dim)
        scr.refresh()
        try:
            ch = scr.get_wch()
        except curses.error:
            continue
        if ch in (curses.KEY_UP, "k"):
            i = max(0, i - 1)
        elif ch in (curses.KEY_DOWN, "j"):
            i = min(len(items) - 1, i + 1)
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            return i
        elif ch in ("q", "\x1b", "\x03"):
            return None


def ask(scr, C, g, label):
    scr.erase()
    h, w = scr.getmaxyx()
    put(scr, 0, 0, (" %s PERSONA // NEW CHARACTER" % g["logo"]).ljust(w), C.title)
    put(scr, 2, 2, "Enter a street name, then %s." % g["enter"], C.norm)
    v = line_edit(scr, C, label)
    return (v or "").strip()


def splash(scr, C, lines):
    scr.erase()
    h, w = scr.getmaxyx()
    y0 = max(0, (h - len(lines)) // 2 - 1)
    scr.nodelay(True)
    try:
        for k, l in enumerate(lines):
            for x, ch in enumerate(l):
                put(scr, y0 + k, 2 + x, ch, C.head if k == 0 else C.norm)
                scr.refresh()
                if scr.getch() != -1:
                    return
                time.sleep(0.006)
            time.sleep(0.08)
        time.sleep(0.35)
    finally:
        scr.nodelay(False)


class Row:
    __slots__ = ("kind", "obj", "i", "text", "lw", "level")

    def __init__(self, kind, obj=None, i=0, text="", lw=0, level=0):
        self.kind, self.obj, self.i, self.text, self.lw, self.level = kind, obj, i, text, lw, level


SELECTABLE = {"kv", "trow", "litem", "add", "long"}

HELP = [
    ("Tab / Shift-Tab", "next / previous section"),
    ("1-9, 0, F1-F10", "jump to section"),
    ("Alt+1-9", "jump to section"),
    ("Up/Down  j/k", "move between fields"),
    ("PgUp/PgDn", "scroll a page"),
    ("Left/Right", "table column; elsewhere +/-"),
    ("Enter", "edit / cycle / add / open notes"),
    ("+  -  Space", "adjust, mark/heal boxes, cycle"),
    ("x  Del", "clear field"),
    ("a", "add row or item"),
    ("d", "delete row or item"),
    ("J / K", "move row down / up"),
    ("u", "undo"),
    ("e", "edit the whole file in $EDITOR"),
    ("s", "save now (autosave is on)"),
    ("q", "quit"),
]


class App:
    def __init__(self, scr, sheet, C, g):
        self.scr, self.sheet, self.C, self.g = scr, sheet, C, g
        self.sec = self.cur = self.col = self.top = 0
        self.msg = None
        self.undo = []
        self.rows = []
        self.hud = None
        self.rebuild()

    # --- HUD ------------------------------------------------------------
    def hud_model(self):
        """What the second display should show for the current section."""
        secs = self.sheet.sections
        cur = secs[self.sec].title
        by = {s.title.lower(): s for s in secs}
        target = by.get(self.sheet.schema.hud.get(cur.lower(), "").lower())
        if target is None and cur.lower() != "status":
            target = by.get("status")
        if target is None:
            target = secs[(self.sec + 1) % len(secs)]
        items = []
        for cv in target.ctxs:
            if cv.sub:
                items.append(("head", cv.sub))
            for it in cv.items:
                if isinstance(it, LongRef):
                    v = it.get()
                    if v:
                        items.append(("text", tuple(v.split("\n")[:12])))
                elif isinstance(it, Ref):
                    items.append(("kv", it.label, hud_val(it.ftype, it.get())))
                elif isinstance(it, TableBind):
                    if not it.rows:
                        continue
                    hdr, pool, rows = list(it.headers), it.ts.pool, []
                    for i in range(len(it.rows)):
                        cells, types = it.cells(i), it.types(i)
                        row = [hud_val(types[j], clean(cells[j])) for j in range(len(hdr))]
                        if pool:
                            p = self.sheet.pool(pool, it, i)
                            row.append(("t", p) if p else ("e",))
                        rows.append(tuple(row))
                    if pool:
                        hdr.append("Pool")
                    items.append(("table", tuple(hdr), tuple(rows)))
                elif isinstance(it, ListBind):
                    vals = tuple(v for v in (it.ref(i).get() for i in range(len(it.items))) if v)
                    if vals:
                        items.append(("list", vals))
        items = [x for k, x in enumerate(items)  # drop headings with nothing under them
                 if not (x[0] == "head" and (k + 1 == len(items) or items[k + 1][0] == "head"))]
        return (self.sheet.name_ref.get(), target.title, tuple(items))

    def push_hud(self):
        if not self.hud:
            return
        if self.hud.error:
            self.msg = (" HUD stopped: %s" % self.hud.error, self.C.dmg)
            self.hud = None
            return
        self.hud.submit(self.hud_model())

    # --- rows -----------------------------------------------------------
    def build_rows(self):
        h, w = self.scr.getmaxyx()
        cw = max(10, w - 5)
        rows = []
        sv = self.sheet.sections[self.sec]
        for ci, cv in enumerate(sv.ctxs):
            if cv.sub:
                if rows:
                    rows.append(Row("blank"))
                rows.append(Row("head", text=cv.sub, level=cv.level))
            refs = [it for it in cv.items if isinstance(it, Ref) and not isinstance(it, LongRef)]
            lw = max([len(r.label) for r in refs] + [0])
            if self.sec == 0 and ci == 0:
                lw = max(lw, 4)
                rows.append(Row("kv", self.sheet.name_ref, lw=lw))
            for it in cv.items:
                if isinstance(it, LongRef):
                    text = it.get()
                    if not text:
                        lines = ["(empty)  %s opens your editor" % self.g["enter"]]
                    else:
                        lines = []
                        for para in text.split("\n"):
                            lines.extend(textwrap.wrap(para, cw) or [""])
                        limit = max(3, h - 7)
                        if len(lines) > limit:
                            lines = lines[: limit - 1] + [self.g["ell"]]
                    rows.append(Row("long", it, text=lines[0]))
                    rows.extend(Row("longc", it, text=l) for l in lines[1:])
                elif isinstance(it, Ref):
                    rows.append(Row("kv", it, lw=lw))
                elif isinstance(it, TableBind):
                    rows.append(Row("thead", it))
                    rows.extend(Row("trow", it, i) for i in range(len(it.rows)))
                    if it.repeat:
                        rows.append(Row("add", it, text="+ add row"))
                elif isinstance(it, ListBind):
                    rows.extend(Row("litem", it, i) for i in range(len(it.items)))
                    if it.repeat:
                        rows.append(Row("add", it, text="+ add item"))
        return rows

    def rebuild(self):
        self.rows = self.build_rows()
        if not self.rows:
            self.cur = 0
            return
        self.cur = max(0, min(self.cur, len(self.rows) - 1))
        if self.rows[self.cur].kind not in SELECTABLE:
            for rng in (range(self.cur, len(self.rows)), range(self.cur, -1, -1)):
                hit = next((i for i in rng if self.rows[i].kind in SELECTABLE), None)
                if hit is not None:
                    self.cur = hit
                    break
        self.fix_col()

    def row(self):
        return self.rows[self.cur] if 0 <= self.cur < len(self.rows) else None

    def cur_ref(self):
        r = self.row()
        if r is None:
            return None
        if r.kind in ("kv", "long"):
            return r.obj
        if r.kind == "trow":
            return r.obj.ref(r.i, self.col)
        if r.kind == "litem":
            return r.obj.ref(r.i)
        return None

    def fix_col(self):
        r = self.row()
        if r and r.kind == "trow":
            cols = [j for j, ft in enumerate(r.obj.types(r.i)) if ft]
            if cols and self.col not in cols:
                self.col = min(cols, key=lambda j: abs(j - self.col))

    # --- drawing --------------------------------------------------------
    def ensure_visible(self, H):
        if self.cur < self.top:
            self.top = self.cur
        if self.cur == self.top:
            k = 0
            while self.top > 0 and k < 2 and self.rows[self.top - 1].kind in ("head", "thead", "blank"):
                self.top -= 1
                k += 1
        if self.cur >= self.top + H:
            self.top = self.cur - H + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - H)))

    def draw(self):
        s, C, g = self.scr, self.C, self.g
        s.erase()
        h, w = s.getmaxyx()
        if h < 10 or w < 36:
            put(s, 0, 0, "TERMINAL TOO SMALL", C.dmg)
            put(s, 1, 0, "need 36x10, have %dx%d" % (w, h), C.norm)
            s.refresh()
            return
        name = self.sheet.name_ref.get() or "UNNAMED"
        left = " %s %s" % (g["logo"], name.upper())
        right = "%s %d/%d " % (self.sheet.schema.id.upper(), self.sec + 1, len(self.sheet.sections))
        put(s, 0, 0, (left + " " * max(1, w - len(left) - len(right)) + right)[:w], C.title)
        x = 0
        for i, sv in enumerate(self.sheet.sections):
            lab = "%d%s" % ((i + 1) % 10, sv.title[:3].upper())
            if x + len(lab) >= w:
                break
            put(s, 1, x, lab, C.sel if i == self.sec else C.dim)
            x += len(lab) + 1
        title = "[ %s ]" % self.sheet.sections[self.sec].title.upper()
        put(s, 2, 0, g["tl"] + g["h"] * (w - 2) + g["tr"], C.dim)
        put(s, 2, 2, title, C.acc)
        for y in range(3, h - 2):
            put(s, y, 0, g["v"], C.dim)
            put(s, y, w - 1, g["v"], C.dim)
        put(s, h - 2, 0, g["bl"] + g["h"] * (w - 2) + g["br"], C.dim)
        H = h - 5
        self.ensure_visible(H)
        for k in range(H):
            idx = self.top + k
            if idx >= len(self.rows):
                break
            self.draw_row(3 + k, self.rows[idx], idx == self.cur, w)
        if self.top > 0:
            put(s, 3, w - 1, g["up"], C.acc)
        if self.top + H < len(self.rows):
            put(s, h - 3, w - 1, g["dn"], C.acc)
        if self.msg:
            put(s, h - 1, 0, self.msg[0][: w - 1], self.msg[1])
        else:
            put(s, h - 1, 0, self.hints()[: w - 1], C.dim)
        s.noutrefresh()
        curses.doupdate()

    def put_segs(self, y, x, segs, sel, pad=0):
        total = 0
        for t, a in segs:
            put(self.scr, y, x + total, t, self.C.sel if sel else a)
            total += len(t)
        want = max(pad, 3 if sel else 0)
        if total < want:
            put(self.scr, y, x + total, " " * (want - total),
                self.C.sel if sel else self.C.norm)

    def val_segs(self, ft, v, width):
        C, g = self.C, self.g
        if ft is None:
            return [(fit(v, width), C.norm)]
        if ft.kind == "boxes":
            n = box_total(ft, v)
            m = min(box_count(v), n)
            return [(g["full"] * m, C.dmg), (g["empty"] * (n - m), C.dim)]
        if not v:
            return [(g["dot"], C.dim)]
        return [(fit(v, max(1, width)), C.val)]

    def tlayout(self, t, cw):
        n = len(t.headers)
        widths = []
        for j in range(n):
            m = len(t.headers[j])
            for i in range(len(t.rows)):
                ft = t.types(i)[j]
                if ft and ft.kind == "boxes":
                    m = max(m, box_total(ft, t.cells(i)[j]))
                else:
                    m = max(m, len(clean(t.cells(i)[j])))
            widths.append(max(1, min(m, 24)))
        start = 0
        r = self.row()
        if r and r.kind == "trow" and r.obj is t:
            j = min(self.col, n - 1)
            while start < j and sum(widths[start:j + 1]) + 2 * (j - start) > cw - 1:
                start += 1
        return widths, start

    def draw_row(self, y, r, sel, w):
        s, C, g = self.scr, self.C, self.g
        x0, cw = 3, w - 4
        if sel:
            put(s, y, 1, g["mark"], C.acc)
        k = r.kind
        if k == "head":
            ind = 2 * max(0, r.level - 3)
            put(s, y, x0 + ind, "%s %s" % (g["sub"], r.text.upper()), C.head)
        elif k == "kv":
            ref = r.obj
            lab = (ref.label + ":").ljust(r.lw + 2)
            put(s, y, x0, lab[:cw], C.norm)
            self.put_segs(y, x0 + len(lab), self.val_segs(ref.ftype, ref.get(), cw - len(lab)), sel)
        elif k in ("thead", "trow"):
            t = r.obj
            widths, start = self.tlayout(t, cw)
            if start > 0:
                put(s, y, x0 - 1, g["more_l"], C.acc)
            x = x0
            for j in range(start, len(t.headers)):
                wj = widths[j]
                if x + wj > x0 + cw - 1:
                    put(s, y, x0 + cw - 1, g["more_r"], C.acc)
                    break
                if k == "thead":
                    put(s, y, x, fit(t.headers[j], wj), C.head)
                else:
                    ft = t.types(r.i)[j]
                    v = clean(t.cells(r.i)[j])
                    self.put_segs(y, x, self.val_segs(ft, v, wj), sel and j == self.col, pad=wj)
                x += wj + 2
        elif k == "litem":
            ref = r.obj.ref(r.i)
            put(s, y, x0, g["bullet"], C.dim)
            self.put_segs(y, x0 + 2, self.val_segs(ref.ftype, ref.get(), cw - 2), sel)
        elif k == "add":
            put(s, y, x0, r.text, C.sel if sel else C.dim)
        elif k in ("long", "longc"):
            cr = self.row()
            hl = cr is not None and cr.kind == "long" and cr.obj is r.obj
            empty = not r.obj.get()
            attr = C.dim if empty else C.val
            put(s, y, x0, r.text[:cw], C.sel if hl else attr)

    def hints(self):
        g = self.g
        r = self.row()
        tail = "  %s 1-9 section  ? help  q quit" % g["tab"]
        if r is None:
            return tail
        ref = self.cur_ref()
        sp = ""
        if r.kind == "add":
            sp = "%s add" % g["enter"]
        elif r.kind == "long":
            sp = "%s open editor" % g["enter"]
        elif ref:
            sp = {"int": "+/- adjust %s type", "num": "+/- adjust %s type",
                  "boxes": "+/- mark/heal", "enum": "%s/+/- cycle"}.get(
                ref.ftype.kind, "%s edit")
            if "%s" in sp:
                sp = sp % g["enter"]
        if r.kind in ("trow", "litem") and r.obj.repeat:
            sp += "  a add  d del"
        return " " + sp + "  " + g["bar"] + tail

    def help(self):
        s, C = self.scr, self.C
        s.erase()
        h, w = s.getmaxyx()
        put(s, 0, 0, (" %s PERSONA v%s // KEYS" % (self.g["logo"], VERSION)).ljust(w), C.title)
        for k, (key, what) in enumerate(HELP):
            if 2 + k >= h - 1:
                break
            put(s, 2 + k, 2, key.ljust(17), C.head)
            put(s, 2 + k, 20, what, C.norm)
        put(s, h - 1, 0, " any key to return", C.dim)
        s.refresh()
        s.get_wch()

    # --- mutation -------------------------------------------------------
    def save(self, quiet=True):
        try:
            self.sheet.save()
        except OSError as e:
            self.msg = ("SAVE FAILED: %s" % e, self.C.dmg)
            return False
        if not quiet:
            self.msg = (" saved %s" % self.sheet.path.name, self.C.acc)
        return True

    def mutate(self, fn):
        before = self.sheet.doc.text()
        fn()
        if self.sheet.doc.text() != before:
            self.undo.append(before)
            del self.undo[:-200]
            self.save()
        self.rebuild()

    def do_undo(self):
        if not self.undo:
            self.msg = (" nothing to undo", self.C.dim)
            return
        self.sheet.load_text(self.undo.pop())
        self.save()
        self.rebuild()
        self.msg = (" undone", self.C.acc)

    def adjust(self, d):
        ref = self.cur_ref()
        if ref is None or isinstance(ref, LongRef):
            return
        ft, v, C, g = ref.ftype, ref.get(), self.C, self.g
        k = ft.kind
        if k == "int":
            try:
                n = int(v) + d
            except ValueError:  # empty: + starts at 1 (or the range minimum)
                base = int(ft.default) if ft.default else 0
                n = max(1, ft.lo or 0) if d > 0 and not ft.default else base
            lo = ft.lo if ft.lo is not None else 0
            n = max(lo, n)
            if ft.hi is not None:
                n = min(ft.hi, n)
            new = str(n)
        elif k == "num":
            try:
                x = float(v) + d * 0.1
            except ValueError:
                x = float(ft.default) if ft.default else 0.0
            new = "%g" % max(0.0, round(x, 2))
        elif k == "boxes":
            n = box_total(ft, v)
            m = max(0, min(n, box_count(v) + d))
            new = box_str(m, n)
            if d > 0:
                txt = " %s %s %d/%d" % (g["up"], ref.label.upper(), m, n)
                if m == n:
                    txt = " !! %s FULL !!" % ref.label.upper()
                self.msg = (txt, C.dmg)
            else:
                self.msg = (" %s patched %s %d/%d" % (g["dn"], ref.label, m, n), C.acc)
        elif k == "enum":
            if not ft.options:
                return
            i = ft.options.index(v) if v in ft.options else -1
            if i == -1 and d < 0:
                i = 0
            new = ft.options[(i + d) % len(ft.options)]
        else:
            self.msg = (" %s to edit text" % g["enter"], C.dim)
            return
        self.mutate(lambda: ref.set(new))

    def edit_ref(self, ref):
        val = line_edit(self.scr, self.C, ref.label, ref.get(), redraw=self.redraw)
        if val is None:
            return False
        val = val.strip()
        ft = ref.ftype
        if val and ft.kind == "int":
            try:
                n = int(val)
            except ValueError:
                self.msg = (" not a whole number: %s" % val, self.C.dmg)
                return False
            if ft.lo is not None and not ft.lo <= n <= ft.hi:
                n = max(ft.lo, min(ft.hi, n))
                self.msg = (" clamped to %d-%d" % (ft.lo, ft.hi), self.C.dim)
            val = str(n)
        elif val and ft.kind == "num":
            try:
                val = "%g" % float(val)
            except ValueError:
                self.msg = (" not a number: %s" % val, self.C.dmg)
                return False
        elif ft.kind == "boxes":
            try:
                n = box_total(ft, ref.get())
                val = box_str(max(0, min(n, int(val or 0))), n)
            except ValueError:
                return False
        self.mutate(lambda: ref.set(val))
        return True

    def redraw(self):
        self.rebuild()
        self.draw()

    def shell_out(self, cmd):
        curses.def_prog_mode()
        curses.endwin()
        err = None
        try:
            subprocess.call(cmd)
        except OSError as e:
            err = e
        finally:
            curses.reset_prog_mode()
            self.scr.clear()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self.scr.refresh()
        if err:
            self.msg = (" editor failed: %s" % err, self.C.dmg)

    def edit_long(self, ref):
        fd, tmp = tempfile.mkstemp(suffix=".md", prefix="persona-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ref.get() + "\n")
        try:
            self.shell_out(editor_cmd() + [tmp])
            new = Path(tmp).read_text(encoding="utf-8").rstrip()
        finally:
            os.unlink(tmp)
        if new != ref.get():
            self.mutate(lambda: ref.set(new))

    def edit_file(self):
        before = self.sheet.doc.text()
        self.save()
        self.shell_out(editor_cmd() + [str(self.sheet.path)])
        try:
            text = self.sheet.path.read_text(encoding="utf-8")
        except OSError as e:
            self.msg = (" reload failed: %s" % e, self.C.dmg)
            return
        if text != before:
            self.undo.append(before)
            self.sheet.load_text(text)
            if self.sheet.migrated:
                self.save()
            self.rebuild()
            self.msg = (" reloaded from disk", self.C.acc)

    def add_item(self, obj):
        if not getattr(obj, "repeat", False):
            return
        self.mutate(obj.add)
        last = (len(obj.rows) if isinstance(obj, TableBind) else len(obj.items)) - 1
        for i, r in enumerate(self.rows):
            if r.obj is obj and r.kind in ("trow", "litem") and r.i == last:
                self.cur = i
                break
        if isinstance(obj, TableBind):
            self.col = 0
            self.fix_col()
        ref = self.cur_ref()
        self.draw()
        if ref and ref.ftype.kind in ("text", "tags", "int", "num"):
            if not self.edit_ref(ref) and obj.is_empty(last):
                obj.delete(last)
                self.undo.pop() if self.undo else None
                self.save()
                self.rebuild()

    def delete_item(self):
        r = self.row()
        if not r or r.kind not in ("trow", "litem") or not r.obj.repeat:
            return
        self.msg = (" delete this %s? y/N" % ("row" if r.kind == "trow" else "item"), self.C.dmg)
        self.draw()
        k = self.scr.get_wch()
        self.msg = None
        if k in ("y", "Y"):
            self.mutate(lambda: r.obj.delete(r.i))

    def move_item(self, d):
        r = self.row()
        if not r or r.kind not in ("trow", "litem") or not r.obj.repeat:
            return
        res = {}
        self.mutate(lambda: res.setdefault("j", r.obj.move(r.i, d)))
        self.cur += res.get("j", r.i) - r.i
        self.rebuild()

    def clear(self):
        ref = self.cur_ref()
        if ref is None or isinstance(ref, LongRef):
            return
        ft = ref.ftype
        new = box_str(0, box_total(ft, ref.get())) if ft.kind == "boxes" else ""
        self.mutate(lambda: ref.set(new))

    # --- input ----------------------------------------------------------
    def move(self, d):
        i = self.cur
        while True:
            i += d
            if i < 0 or i >= len(self.rows):
                return False
            if self.rows[i].kind in SELECTABLE:
                break
        self.cur = i
        self.fix_col()
        return True

    def col_move(self, d):
        r = self.row()
        cols = [j for j, ft in enumerate(r.obj.types(r.i)) if ft]
        if not cols:
            return
        if self.col not in cols:
            self.fix_col()
            return
        p = cols.index(self.col) + d
        if 0 <= p < len(cols):
            self.col = cols[p]

    def goto_sec(self, i, wrap=False):
        n = len(self.sheet.sections)
        if wrap:
            i %= n
        if 0 <= i < n and i != self.sec:
            self.sec, self.cur, self.top, self.col = i, 0, 0, 0
            self.rebuild()

    def getkey(self):
        try:
            ch = self.scr.get_wch()
        except curses.error:
            return None
        if ch == "\x1b":
            self.scr.nodelay(True)
            try:
                nxt = self.scr.get_wch()
            except curses.error:
                nxt = None
            self.scr.nodelay(False)
            if isinstance(nxt, str) and nxt.isdigit():
                return "ALT" + nxt
            return "ESC"
        return ch

    def enter(self):
        r = self.row()
        if r is None:
            return
        if r.kind == "add":
            return self.add_item(r.obj)
        ref = self.cur_ref()
        if ref is None:
            return
        if isinstance(ref, LongRef):
            return self.edit_long(ref)
        if ref.ftype.kind in ("enum", "boxes"):
            return self.adjust(+1)
        self.edit_ref(ref)

    def run(self):
        K = curses
        while True:
            self.draw()
            self.push_hud()
            k = self.getkey()
            if k is None:
                continue
            self.msg = None
            r = self.row()
            if k in ("q", "Q", "\x11", "\x03"):
                return
            if k == K.KEY_RESIZE:
                self.rebuild()
            elif k == "\t":
                self.goto_sec(self.sec + 1, wrap=True)
            elif k == K.KEY_BTAB:
                self.goto_sec(self.sec - 1, wrap=True)
            elif isinstance(k, str) and len(k) == 1 and k.isdigit():
                self.goto_sec((int(k) - 1) % 10)
            elif isinstance(k, str) and k.startswith("ALT"):
                self.goto_sec((int(k[3]) - 1) % 10)
            elif isinstance(k, int) and K.KEY_F0 < k <= K.KEY_F0 + 10:
                self.goto_sec(k - K.KEY_F0 - 1)
            elif k in (K.KEY_UP, "k"):
                self.move(-1)
            elif k in (K.KEY_DOWN, "j"):
                self.move(+1)
            elif k in (K.KEY_PPAGE, K.KEY_NPAGE):
                d = -1 if k == K.KEY_PPAGE else 1
                for _ in range(max(1, self.scr.getmaxyx()[0] - 6)):
                    if not self.move(d):
                        break
            elif k in (K.KEY_HOME, "g"):
                self.cur = 0
                self.rebuild()
            elif k in (K.KEY_END, "G"):
                self.cur = len(self.rows) - 1
                self.rebuild()
            elif k in (K.KEY_LEFT, "h", K.KEY_RIGHT, "l"):
                d = -1 if k in (K.KEY_LEFT, "h") else 1
                if r and r.kind == "trow":
                    self.col_move(d)
                else:
                    self.adjust(d)
            elif k in ("\n", "\r", K.KEY_ENTER):
                self.enter()
            elif k in ("+", "=", " "):
                if r and r.kind == "add" and k == " ":
                    self.add_item(r.obj)
                else:
                    self.adjust(+1)
            elif k in ("-", "_"):
                self.adjust(-1)
            elif k in ("x", K.KEY_DC):
                self.clear()
            elif k == "a":
                if r and r.kind in ("trow", "litem", "add"):
                    self.add_item(r.obj)
            elif k == "d":
                self.delete_item()
            elif k in ("J", K.KEY_SF):
                self.move_item(+1)
            elif k in ("K", K.KEY_SR):
                self.move_item(-1)
            elif k == "u":
                self.do_undo()
            elif k == "e":
                self.edit_file()
            elif k in ("s", "\x13"):
                self.save(quiet=False)
            elif k == "?":
                self.help()


# --------------------------------------------------------------------------
# HUD: a read-only second display drawn straight onto a framebuffer
# --------------------------------------------------------------------------
HUD_THEMES = {
    "green": dict(main=(0, 230, 90), head=(0, 200, 220), val=(255, 215, 0), dim=(0, 150, 70)),
    "amber": dict(main=(255, 176, 0), head=(0, 200, 220), val=(240, 240, 240), dim=(160, 110, 0)),
    "cyan": dict(main=(0, 210, 240), head=(220, 60, 220), val=(255, 215, 0), dim=(0, 140, 165)),
}
HUD_DMG = (235, 40, 40)
FBIOGET_CON2FBMAP = 0x460F
FBIOPUT_CON2FBMAP = 0x4610


def fb_list():
    out = []
    try:
        with open("/proc/fb") as f:
            for line in f:
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    out.append((int(parts[0]), parts[1].strip()))
    except OSError:
        pass
    return out


def fb_find(spec):
    """'auto' = first SPI panel (fbtft drivers are named fb_*); else a /dev/fbN,
    an index, or a driver name like fb_ili9486 or vc4drmfb."""
    fbs = fb_list()
    if spec in (None, "", "auto"):
        return next((i for i, n in fbs if n.startswith("fb_")), None)
    m = re.fullmatch(r"(?:/dev/fb)?(\d+)", spec)
    if m:
        return int(m[1])
    return next((i for i, n in fbs if n == spec or spec in n), None)


def console_fb(con=1):
    """Which framebuffer a text console is drawn on (None if unknown)."""
    try:
        import fcntl
        import struct
    except ImportError:
        return None
    for i, _ in fb_list():
        try:
            with open("/dev/fb%d" % i, "rb") as f:
                buf = bytearray(struct.pack("II", con, 0))
                fcntl.ioctl(f, FBIOGET_CON2FBMAP, buf, True)
                return struct.unpack("II", buf)[1]
        except OSError:
            continue
    return None


def hdmi_connected():
    import glob
    for p in glob.glob("/sys/class/drm/card*-HDMI-A-*/status"):
        try:
            with open(p) as f:
                if f.read().strip() == "connected":
                    return True
        except OSError:
            pass
    return False


def map_console(spec, wait):
    """Move every text console to one framebuffer, then exit (needs root).
    auto: the HDMI screen if a monitor is plugged in, otherwise the SPI TFT."""
    import fcntl
    import struct
    auto = spec in (None, "", "auto")
    deadline = time.time() + wait
    while True:  # displays register in either order; wait for both
        fbs = fb_list()
        if auto and any(n.startswith("fb_") for _, n in fbs) and any("drm" in n for _, n in fbs):
            break
        if not auto and fb_find(spec) is not None:
            break
        if time.time() > deadline:
            break
        time.sleep(0.5)
    fbs = fb_list()
    if auto:
        drm = [i for i, n in fbs if "drm" in n]
        tft = [i for i, n in fbs if n.startswith("fb_")]
        target = drm[0] if (drm and hdmi_connected()) else (tft or drm or [None])[0]
    else:
        target = fb_find(spec)
    if target is None:
        print("persona: no matching framebuffer", file=sys.stderr)
        return 1
    with open("/dev/fb%d" % target, "rb") as f:
        for con in range(1, 64):
            try:
                fcntl.ioctl(f, FBIOPUT_CON2FBMAP, struct.pack("II", con, target))
            except OSError:
                pass
    print("persona: console -> fb%d (%s)" % (target, dict(fbs).get(target, "?")))
    return 0


def hud_val(ft, v):
    if ft is None:
        return ("l", v)
    if ft.kind == "boxes":
        n = box_total(ft, v)
        return ("b", n, min(box_count(v), n))
    return ("t", v) if v else ("e",)


class Hud:
    """Renders a summary of one section and writes it to a framebuffer from a
    background thread, so the main screen never waits on the SPI panel."""

    FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono%s.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationMono-%s.ttf")

    def __init__(self, spec, theme, font_size=15):
        import threading
        from PIL import Image, ImageChops, ImageDraw, ImageFont
        self.Image, self.ImageChops, self.ImageDraw, self.ImageFont = Image, ImageChops, ImageDraw, ImageFont
        test = os.environ.get("PERSONA_HUD_GEOMETRY")  # WxHxBPP: write to a plain file (testing)
        if test:
            w, h, bpp = map(int, test.split("x"))
            stride, self.path = w * bpp // 8, spec
        else:
            idx = fb_find(spec)
            if idx is None:
                raise RuntimeError("no HUD display found (%s)" % (spec or "auto"))
            if console_fb(1) == idx:
                raise RuntimeError("the console is on fb%d, the HUD display" % idx)
            base = "/sys/class/graphics/fb%d/" % idx
            with open(base + "virtual_size") as f:
                w, h = (int(x) for x in f.read().strip().split(","))
            with open(base + "bits_per_pixel") as f:
                bpp = int(f.read())
            try:
                with open(base + "stride") as f:
                    stride = int(f.read())
            except (OSError, ValueError):
                stride = w * bpp // 8
            self.path = "/dev/fb%d" % idx
        self.w, self.h, self.bpp, self.stride = w, h, bpp, stride
        self.fh = open(self.path, "r+b")
        self.c = HUD_THEMES.get(theme, HUD_THEMES["green"])
        self.font = self._font(("", "Regular"), font_size)
        self.bold = self._font(("-Bold", "Bold"), font_size)
        self.cw = self.font.getlength("M")
        asc, desc = self.font.getmetrics()
        self.lh = asc + desc + 2
        self.cols = int(self.w // self.cw)
        self.box = max(6, int(self.lh * 0.62))
        self.lut = ([v & 0xF8 for v in range(256)], [v >> 5 for v in range(256)],
                    [(v & 0x1C) << 3 for v in range(256)], [v >> 3 for v in range(256)])
        self.cond = threading.Condition()
        self.pending = self.last = self.error = None
        self.stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _font(self, variants, size):
        for pat, var in zip(self.FONTS, variants):
            if os.path.exists(pat % var):
                return self.ImageFont.truetype(pat % var, size)
        try:
            return self.ImageFont.load_default(size)
        except TypeError:
            return self.ImageFont.load_default()

    # --- threading ------------------------------------------------------
    def submit(self, model):
        with self.cond:
            if model == self.last or model == self.pending:
                return
            self.pending = model
            self.cond.notify()

    def _loop(self):
        while True:
            with self.cond:
                while self.pending is None and not self.stop:
                    self.cond.wait()
                if self.pending is None:
                    return
                model, self.pending = self.pending, None
            try:
                self._write(self.render(model))
                self.last = model
            except Exception as e:  # noqa: BLE001 - report, never crash the sheet
                self.error = str(e)

    def close(self, text="// JACKED OUT"):
        with self.cond:
            self.stop = True
            self.cond.notify()
        self.thread.join(timeout=3)
        try:
            self._write(self.render(("", "", (("banner", text),))))
            self.fh.close()
        except Exception:  # noqa: BLE001
            pass

    # --- output ---------------------------------------------------------
    def _write(self, img):
        if self.bpp == 16:  # RGB565, little-endian
            r, g, b = img.split()
            a, bb, cc, d = self.lut
            hi = self.ImageChops.add(r.point(a), g.point(bb))
            lo = self.ImageChops.add(g.point(cc), b.point(d))
            data = self.Image.merge("LA", (lo, hi)).tobytes()
        elif self.bpp == 32:
            data = img.convert("RGBA").tobytes("raw", "BGRA")
        elif self.bpp == 24:
            data = img.tobytes("raw", "BGR")
        else:
            raise RuntimeError("unsupported %d bpp display" % self.bpp)
        row = self.w * self.bpp // 8
        if self.stride == row:
            self.fh.seek(0)
            self.fh.write(data)
        else:
            for y in range(self.h):
                self.fh.seek(y * self.stride)
                self.fh.write(data[y * row:(y + 1) * row])
        self.fh.flush()

    # --- drawing --------------------------------------------------------
    def _t(self, d, x, y, s, fill, font=None):
        d.text((x, y), s, font=font or self.font, fill=fill)

    def _fit(self, s, n):
        return s if len(s) <= n else s[: max(0, n - 1)] + "…"

    def _chars(self, val):
        if val[0] == "b":
            return -(-val[1] * (self.box + 3) // int(self.cw))
        return 1 if val[0] == "e" else len(val[1])

    def _val(self, d, x, y, val, maxch, color=None):
        k = val[0]
        if k == "b":
            n, m, s = val[1], val[2], self.box
            top = y + (self.lh - s) // 2 - 1
            for i in range(n):
                bx = x + i * (s + 3)
                if i < m:
                    d.rectangle([bx, top, bx + s, top + s], fill=HUD_DMG)
                else:
                    d.rectangle([bx, top, bx + s, top + s], outline=self.c["dim"])
        elif k == "e":
            self._t(d, x, y, "·", self.c["dim"])
        else:
            col = color or (self.c["main"] if k == "l" else self.c["val"])
            self._t(d, x, y, self._fit(val[1], maxch), col)

    def render(self, model):
        name, title, items = model
        c, lh, cw = self.c, self.lh, self.cw
        img = self.Image.new("RGB", (self.w, self.h), (0, 0, 0))
        d = self.ImageDraw.Draw(img)
        y = 0
        if name or title:
            d.rectangle([0, 0, self.w, lh + 1], fill=c["main"])
            self._t(d, 4, 1, name.upper(), (0, 0, 0), self.bold)
            right = "// " + title.upper()
            self._t(d, self.w - self.bold.getlength(right) - 4, 1, right, (0, 0, 0), self.bold)
            y = lh + 4
        i = 0
        while i < len(items):
            if y + lh > self.h:
                break
            it = items[i]
            k = it[0]
            if k == "banner":
                tw = self.bold.getlength(it[1])
                self._t(d, (self.w - tw) / 2, (self.h - lh) / 2, it[1], c["dim"], self.bold)
            elif k == "head":
                self._t(d, 2, y, "» " + it[1].upper(), c["head"], self.bold)
                y += lh
            elif k == "kv":
                run = [it]
                while i + 1 < len(items) and items[i + 1][0] == "kv":
                    i += 1
                    run.append(items[i])
                y = self._kvs(d, y, run)
            elif k == "table":
                y = self._table(d, y, it[1], it[2])
            elif k in ("list", "text"):
                lines = textwrap.wrap("  ·  ".join(it[1]), self.cols - 2) if k == "list" else \
                    [w for ln in it[1] for w in (textwrap.wrap(ln, self.cols - 2) or [""])]
                for ln in lines:
                    if y + lh > self.h:
                        break
                    self._t(d, 4, y, ln, c["val"])
                    y += lh
            i += 1
        if i < len(items):
            self._t(d, self.w - cw - 2, self.h - lh, "…", c["dim"])
        return img

    def _kvs(self, d, y, run):
        ncol = 2 if len(run) >= 4 else 1
        colw = self.w // ncol
        for start in range(0, len(run), ncol):
            if y + self.lh > self.h:
                break
            for k, it in enumerate(run[start:start + ncol]):
                group = run[k::ncol]
                lw = max(len(g[1]) for g in group) + 2
                x = 4 + k * colw
                self._t(d, x, y, self._fit(it[1] + ":", lw), self.c["main"])
                self._val(d, x + lw * self.cw, y, it[2], int(colw / self.cw) - lw - 1)
            y += self.lh
        return y

    def _table(self, d, y, headers, rows):
        n = len(headers)
        widths = [max([len(headers[j])] + [self._chars(r[j]) for r in rows]) for j in range(n)]
        avail = self.cols - 1
        while sum(widths) + 2 * (n - 1) > avail:
            text_cols = [j for j in range(n) if widths[j] > 3 and all(r[j][0] != "b" for r in rows)]
            if not text_cols:
                break
            widths[max(text_cols, key=lambda j: widths[j])] -= 1
        xs, x = [], 4
        for w in widths:
            xs.append(x)
            x += (w + 2) * self.cw
        for j in range(n):
            self._t(d, xs[j], y, self._fit(headers[j], widths[j]), self.c["dim"])
        y += self.lh
        for r in rows:
            if y + self.lh > self.h:
                break
            for j in range(n):
                self._val(d, xs[j], y, r[j], widths[j])
            y += self.lh
        return y


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run_curses(scr, args, home, templates, schema, charpath, ascii_mode):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.raw()
    scr.keypad(True)
    C = Colors(args.theme)
    scr.bkgd(" ", C.norm)
    g = GLYPHS_ASCII if ascii_mode else GLYPHS_UNI
    ASCII["on"] = ascii_mode
    if schema is None:
        if len(templates) == 1:
            schema = templates[0]
        else:
            i = pick(scr, C, g, "SELECT SYSTEM",
                     ["%-8s %s" % (t.id, t.system) for t in templates])
            if i is None:
                return None
            schema = templates[i]
    if charpath is None:
        name = None
        if args.new is not None:
            name = args.new or ask(scr, C, g, "Street name")
        else:
            chars = list_chars(home, schema.id)
            items = ["+ NEW CHARACTER"] + ["%s  (%s)" % (n, p.name) for p, n, _ in chars]
            i = pick(scr, C, g, schema.system.upper(), items)
            if i is None:
                return None
            if i == 0:
                name = ask(scr, C, g, "Street name")
            else:
                charpath = chars[i - 1][0]
        if charpath is None:
            if not name:
                return None
            charpath = create_character(schema, name, home)
    sheet = Sheet(schema, charpath)
    sheet.backup()
    if sheet.migrated:
        sheet.save()
    if not args.no_splash:
        splash(scr, C, [
            "PERSONA v%s // console rpg character sheet" % VERSION,
            "",
            "> mount template %s ........ OK" % schema.id,
            "> decrypt %s ........ OK" % charpath.name,
            "> sync biomonitor ........ OK",
            "> JACKING IN %s" % ("_" if ascii_mode else "▌"),
        ])
    app = App(scr, sheet, C, g)
    hud = None
    if args.hud:
        try:
            hud = Hud(args.hud, args.theme, args.hud_font)
        except ImportError:
            app.msg = (" HUD needs Pillow: sudo apt install python3-pil", C.dmg)
        except Exception as e:  # noqa: BLE001
            app.msg = (" HUD off: %s" % e, C.dmg)
    app.hud = hud
    try:
        app.run()
    finally:
        if hud:
            hud.close()
    return "saved: %s" % charpath


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="persona", description="Persona - a Markdown-backed, keyboard-driven RPG character sheet.")
    ap.add_argument("-g", "--game", metavar="ID", help="template id or file, e.g. sra2")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("-c", "--char", metavar="NAME", help="character name, file stem or path")
    grp.add_argument("-n", "--new", nargs="?", const="", metavar="NAME", help="create a new character")
    ap.add_argument("--dir", metavar="PATH", help="data directory (default ~/.local/share/persona or $PERSONA_HOME)")
    ap.add_argument("--ascii", action="store_true", help="plain ASCII glyphs")
    ap.add_argument("--theme", choices=("green", "amber", "cyan"), default="green")
    ap.add_argument("--no-splash", action="store_true", help="skip the boot animation")
    ap.add_argument("--list", action="store_true", help="list templates and characters, then exit")
    ap.add_argument("--hud", nargs="?", const="auto", metavar="FB",
                    help="live summary on a second display (default: the SPI TFT; or /dev/fbN)")
    ap.add_argument("--hud-font", type=int, default=15, metavar="PX", help="HUD font size")
    ap.add_argument("--map-console", nargs="?", const="auto", metavar="FB",
                    help="(root) move text consoles to a display and exit; auto = HDMI if "
                         "connected, else the TFT")
    ap.add_argument("--wait", type=float, default=15, metavar="SEC",
                    help="with --map-console: how long to wait for displays")
    ap.add_argument("--version", action="version", version="persona " + VERSION)
    args = ap.parse_args(argv)
    if args.map_console is not None:
        return map_console(args.map_console, args.wait)

    locale.setlocale(locale.LC_ALL, "")
    enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
    ascii_mode = args.ascii or enc not in ("utf8", "cp65001")

    home = args.dir or os.environ.get("PERSONA_HOME") or os.environ.get("CRPGCS_HOME")
    home = Path(home).expanduser() if home else default_home()
    (home / "templates").mkdir(parents=True, exist_ok=True)
    (home / "characters").mkdir(parents=True, exist_ok=True)
    templates = find_templates(home)

    if args.list:
        print("templates:")
        for t in templates:
            print("  %-8s %-30s %s" % (t.id, t.system, t.path))
        print("characters (%s):" % (home / "characters"))
        for p, n, tid in list_chars(home):
            print("  %-20s %-8s %s" % (n, tid, p.name))
        return 0
    if not templates:
        print("No templates found. Put a *.cstpl.md file in %s or next to this script."
              % (home / "templates"), file=sys.stderr)
        return 1

    schema = charpath = None
    if args.game:
        schema = match_template(templates, args.game)
        if schema is None:
            print("Unknown template: %s (try --list)" % args.game, file=sys.stderr)
            return 1
    if args.char:
        charpath = resolve_char(home, args.char)
        if charpath is None:
            print("Character not found: %s (try --list)" % args.char, file=sys.stderr)
            return 1
        if schema is None:
            tid = read_header(charpath).get("template")
            schema = match_template(templates, tid)
            if schema is None:
                print("Template '%s' for %s not found." % (tid, charpath.name), file=sys.stderr)
                return 1
    try:
        res = curses.wrapper(run_curses, args, home, templates, schema, charpath, ascii_mode)
    except KeyboardInterrupt:
        res = None
    if res:
        print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
