"""A parser for the subset of the SysML v2 textual notation that the design language uses.

No maintained open-source Python parser reads SysML v2 with its semantics: the OMG pilot
implementation is Java (Xtext), Syside is proprietary, and sysml2py's reader splits text into
statements without parsing values or connections. So this module parses a stated subset of the
grammar (OMG SysML v2.0, formal/25-xx, clause 8 "Textual notation"), and the library and models are
also checked with the pilot implementation (see results/issue-36/README.md).

The subset:
- `package` and `library package`, with `public`, `private` and `protected` visibility.
- `import A::*;` and `import A::B;`.
- `doc /* ... */`, `comment /* ... */`, line and block comments.
- Definitions: `part def`, `attribute def`, `port def`, `connection def`, `interface def`,
  `item def`, `requirement def` and `enum def`, with `:>` or `specializes`.
- Usages: `part`, `attribute`, `port`, `item`, `ref part`, `requirement`, `enum` literals and
  `end` features, typed with `:`, with a multiplicity `[n]` or `[n..m]`, redefinition with `:>>` or
  `redefines`, subsetting with `:>` or `subsets`, and a value with `=` or `default =`.
- `connection NAME : TYPE connect a.b to c.d { ... }` and `connect a.b to c.d;`.
- Values: numbers with an optional unit in brackets (`11.2 [m]`, `0.05 [W/(m^2*K)]`), strings,
  `true`, `false`, enumeration literals (`EngineCycle::fullFlow`) and a leading minus.
- `subject` and `require constraint { ... }` inside requirements are read and kept as text.

Anything else is a syntax error that names the line, so a model never compiles to something the
author did not write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KEYWORDS = {
    "package", "library", "standard", "import", "public", "private", "protected", "doc", "comment",
    "def", "part", "attribute", "port", "connection", "interface", "item", "requirement", "enum",
    "ref", "end", "connect", "to", "specializes", "subsets", "redefines", "default", "true", "false",
    "subject", "require", "constraint", "abstract", "about", "in", "out", "inout",
}
DEF_KINDS = {"part", "attribute", "port", "connection", "interface", "item", "requirement", "enum"}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<doc>/\*.*?\*/)
  | (?P<line>//[^\n]*)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<qname>'(?:[^'\\]|\\.)*')
  | (?P<number>(?:\d+\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<sym>:>>|:>|::|:=|\.\.|[{}()\[\];:=,.*/^~#+\-<>])
  | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
""", re.S | re.X)


class SysMLSyntaxError(Exception):
    def __init__(self, msg, line, path="<model>"):
        super().__init__(f"{path}:{line}: {msg}")
        self.line, self.path, self.msg = line, path, msg


@dataclass
class Token:
    kind: str     # name, number, string, sym, doc, kw, eof
    text: str
    line: int


def tokenize(text: str, path="<model>"):
    out, pos, line = [], 0, 1
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise SysMLSyntaxError(f"unexpected character {text[pos]!r}", line, path)
        kind, s = m.lastgroup, m.group()
        if kind == "doc":
            out.append(Token("doc", s[2:-2], line))
        elif kind == "qname":
            out.append(Token("name", s[1:-1], line))
        elif kind == "name":
            out.append(Token("kw" if s in KEYWORDS else "name", s, line))
        elif kind in ("string", "number", "sym"):
            out.append(Token(kind, s, line))
        line += s.count("\n")
        pos = m.end()
    out.append(Token("eof", "", line))
    return out


# ---- AST ------------------------------------------------------------------------------------------

@dataclass
class Value:
    """A literal value: a number with its unit text (or None), a string, a bool or an enum literal path."""
    kind: str            # number, string, bool, enum
    value: object
    unit: str | None = None
    line: int = 0


@dataclass
class Node:
    kind: str                     # package, def, usage, connection, import, doc, comment, enumlit, end, subject, constraint
    line: int
    path: str
    metaclass: str = ""           # part, attribute, port, ... (for def and usage)
    name: str | None = None
    types: list = field(default_factory=list)          # ':' types of a usage
    specializes: list = field(default_factory=list)    # ':>' of a definition, or subsets of a usage
    redefines: str | None = None
    multiplicity: tuple | None = None                   # (lo, hi); hi None for '*'
    value: Value | None = None
    ends: tuple | None = None                           # connection: (path a, path b)
    children: list = field(default_factory=list)
    text: str = ""                                      # doc, comment, constraint body
    visibility: str = "public"
    library: bool = False
    is_ref: bool = False

    def members(self, kind=None):
        return [c for c in self.children if kind is None or c.kind == kind]


class Parser:
    def __init__(self, text, path="<model>"):
        self.path = path
        self.t = tokenize(text, path)
        self.i = 0

    # helpers
    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def next(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def at(self, text, k=0):
        return self.peek(k).text == text and self.peek(k).kind in ("kw", "sym")

    def expect(self, text):
        tok = self.next()
        if tok.text != text or tok.kind not in ("kw", "sym"):
            raise SysMLSyntaxError(f"expected {text!r}, found {tok.text or 'end of file'!r}", tok.line, self.path)
        return tok

    def err(self, msg, tok=None):
        tok = tok or self.peek()
        raise SysMLSyntaxError(msg, tok.line, self.path)

    def name(self):
        tok = self.next()
        if tok.kind != "name":
            raise SysMLSyntaxError(f"expected a name, found {tok.text or 'end of file'!r}", tok.line, self.path)
        return tok.text

    def qualified(self):
        parts = [self.name()]
        while self.at("::") and self.peek(1).kind == "name":
            self.next()
            parts.append(self.name())
        return "::".join(parts)

    def feature_path(self):
        parts = [self.name()]
        while self.at(".") or self.at("::"):
            self.next()
            parts.append(self.name())
        return ".".join(parts)

    # grammar
    def parse(self):
        root = Node("package", 0, self.path, name="<root>")
        while self.peek().kind != "eof":
            root.children.append(self.element())
        return root

    def body(self, node):
        if self.at(";"):
            self.next()
            return
        if not self.at("{"):
            self.err(f"expected ';' or '{{' after {node.name or node.metaclass or node.kind}, found {self.peek().text!r}")
        self.next()
        while not self.at("}"):
            if self.peek().kind == "eof":
                self.err("missing '}'")
            node.children.append(self.element())
        self.next()

    def element(self):
        tok = self.peek()
        vis = "public"
        if tok.text in ("public", "private", "protected") and tok.kind == "kw":
            vis = self.next().text
            tok = self.peek()
        if tok.kind == "doc":     # a bare block comment documents nothing; keep it as a comment
            self.next()
            return Node("comment", tok.line, self.path, text=tok.text.strip(), visibility=vis)
        if tok.kind != "kw" and not (tok.kind == "sym" and tok.text in (":>>", ";")):
            self.err(f"expected a declaration, found {tok.text!r}")
        t = tok.text
        if t == ";":
            self.next()
            return Node("comment", tok.line, self.path)
        if t in ("library", "standard", "package"):
            lib = False
            while self.peek().text in ("library", "standard"):
                self.next()
                lib = True
            self.expect("package")
            n = Node("package", tok.line, self.path, name=self.qualified(), visibility=vis, library=lib)
            self.body(n)
            return n
        if t == "import":
            self.next()
            parts = [self.name()]
            star = False
            while self.at("::"):
                self.next()
                if self.at("*"):
                    self.next()
                    star = True
                    break
                parts.append(self.name())
            self.expect(";")
            return Node("import", tok.line, self.path, name="::".join(parts) + ("::*" if star else ""), visibility=vis)
        if t in ("doc", "comment"):
            self.next()
            if self.peek().kind == "name":      # a named comment
                self.next()
            if self.at("about"):
                self.next()
                self.feature_path()
                while self.at(","):
                    self.next()
                    self.feature_path()
            d = self.next()
            if d.kind != "doc":
                self.err(f"{t} needs a /* ... */ body", d)
            return Node(t, tok.line, self.path, text=_clean_doc(d.text), visibility=vis)
        if t == "abstract":
            self.next()
            return self.element()
        if t == "connect":
            return self.connection(tok, vis, named=False)
        if t == "connection" and not self.at("def", 1):
            return self.connection(tok, vis, named=True)
        if t == "interface" and not self.at("def", 1):
            return self.connection(tok, vis, named=True)
        if t == "subject":
            self.next()
            n = Node("subject", tok.line, self.path, visibility=vis)
            self.usage_header(n)
            self.body(n)
            return n
        if t == "require":
            self.next()
            self.expect("constraint")
            return self.constraint(tok, vis)
        if t == "constraint":
            self.next()
            return self.constraint(tok, vis)
        if t == "end":
            self.next()
            meta = ""
            if self.peek().text in DEF_KINDS and self.peek().kind == "kw":
                meta = self.next().text
            n = Node("end", tok.line, self.path, metaclass=meta, visibility=vis)
            self.usage_header(n)
            self.body(n)
            return n
        if t == "ref":
            self.next()
            meta = "part"
            if self.peek().text in DEF_KINDS:
                meta = self.next().text
            n = Node("usage", tok.line, self.path, metaclass=meta, visibility=vis, is_ref=True)
            self.usage_header(n)
            self.body(n)
            return n
        if t in DEF_KINDS:
            self.next()
            if self.at("def"):
                self.next()
                n = Node("def", tok.line, self.path, metaclass=t, name=self.name(), visibility=vis)
                if self.at(":>") or self.at("specializes"):
                    self.next()
                    n.specializes.append(self.qualified())
                    while self.at(","):
                        self.next()
                        n.specializes.append(self.qualified())
                self.body(n)
                return n
            if t == "enum":
                n = Node("enumlit", tok.line, self.path, name=self.name(), visibility=vis)
                self.body(n)
                return n
            n = Node("usage", tok.line, self.path, metaclass=t, visibility=vis)
            self.usage_header(n)
            self.body(n)
            return n
        if t == ":>>" or t == "redefines":
            n = Node("usage", tok.line, self.path, metaclass="", visibility=vis)
            self.usage_header(n)
            self.body(n)
            return n
        self.err(f"{t!r} is not in the subset of SysML v2 this parser reads")

    def usage_header(self, n):
        if self.at("<"):             # a short name, e.g. attribute <bar> bar : PressureUnit
            self.next()
            self.name()
            self.expect(">")
        if self.peek().kind == "name":
            n.name = self.name()
        while True:
            if self.at(":") :
                self.next()
                n.types.append(self.qualified())
                while self.at(","):
                    self.next()
                    n.types.append(self.qualified())
            elif self.at(":>>") or self.at("redefines"):
                self.next()
                n.redefines = self.feature_path()
                if n.name is None:
                    n.name = n.redefines.split(".")[-1]
            elif self.at(":>") or self.at("subsets"):
                self.next()
                n.specializes.append(self.feature_path())
            elif self.at("["):
                n.multiplicity = self.multiplicity()
            else:
                break
        if self.at("default"):
            self.next()
            if self.at("=") or self.at(":="):
                self.next()
            n.value = self.value()
        elif self.at("=") or self.at(":="):
            self.next()
            n.value = self.value()

    def multiplicity(self):
        self.expect("[")
        lo = self.next()
        if lo.kind != "number" and lo.text != "*":
            self.err("a multiplicity is a whole number or '*'", lo)
        hi = lo
        if self.at(".."):
            self.next()
            hi = self.next()
        self.expect("]")
        lo_v = 0 if lo.text == "*" else int(float(lo.text))
        hi_v = None if hi.text == "*" else int(float(hi.text))
        return (lo_v, hi_v)

    def value(self):
        tok = self.peek()
        sign = 1.0
        if self.at("-"):
            self.next()
            sign = -1.0
            tok = self.peek()
        if tok.kind == "number":
            self.next()
            v = Value("number", sign * float(tok.text), line=tok.line)
            if self.at("["):
                self.next()
                depth, parts = 1, []
                while True:
                    u = self.next()
                    if u.kind == "eof":
                        self.err("missing ']' after a unit", u)
                    if u.text == "[":
                        depth += 1
                    if u.text == "]":
                        depth -= 1
                        if depth == 0:
                            break
                    parts.append(u.text)
                v.unit = "".join(parts)
            return v
        if sign < 0:
            self.err("a minus sign must come before a number")
        if tok.kind == "string":
            self.next()
            return Value("string", bytes(tok.text[1:-1], "utf-8").decode("unicode_escape"), line=tok.line)
        if tok.text in ("true", "false") and tok.kind == "kw":
            self.next()
            return Value("bool", tok.text == "true", line=tok.line)
        if tok.kind == "name":
            return Value("enum", self.qualified(), line=tok.line)
        self.err(f"expected a value, found {tok.text!r}")

    def connection(self, tok, vis, named):
        n = Node("connection", tok.line, self.path, metaclass=tok.text if named else "connection", visibility=vis)
        if named:
            self.next()
            self.usage_header(n)
        self.expect("connect")
        a = self.feature_path()
        self.expect("to")
        b = self.feature_path()
        n.ends = (a, b)
        self.body(n)
        return n

    def constraint(self, tok, vis):
        n = Node("constraint", tok.line, self.path, visibility=vis)
        if self.peek().kind == "name":
            n.name = self.name()
        if self.at(";"):
            self.next()
            return n
        self.expect("{")
        depth, parts = 1, []
        while depth:
            u = self.next()
            if u.kind == "eof":
                self.err("missing '}' in a constraint", u)
            depth += (u.text == "{") - (u.text == "}")
            if depth:
                parts.append(u.text)
        n.text = " ".join(parts)
        return n


def _clean_doc(s):
    lines = [ln.strip().lstrip("*").strip() for ln in s.strip().splitlines()]
    return " ".join(ln for ln in lines if ln)


def parse(text, path="<model>"):
    return Parser(text, path).parse()


def parse_file(path):
    with open(path) as f:
        return parse(f.read(), str(path))
