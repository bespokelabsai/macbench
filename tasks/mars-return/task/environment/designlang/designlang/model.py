"""Name resolution, inheritance and instances for a parsed SysML v2 model.

`load([files])` parses the domain library and the team's model together and returns a `Model`. Its
`instances()` are the part usages at package level, each expanded into a tree of `Inst`: every
attribute, part, port and connection it has, from its definition, the definitions that one
specializes, and its own body, with redefinitions applied in that order. Values are converted to SI
(angles to degrees) and checked against their declared quantity types.

Every problem is collected in `Model.errors` with its file and line, so a checker can report them all
at once.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import sysml as S
from .units import QUANTITY_TYPES, SCALAR_TYPES, UnitError, dim_text, parse_unit

LIBRARY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "library")
# Packages of the SysML v2 standard library that models may import. Their types are known by name.
STANDARD_PACKAGES = {"ScalarValues", "ISQ", "SI", "ISQBase", "ISQSpaceTime", "ISQMechanics", "ISQThermodynamics",
                     "SIPrefixes", "MeasurementReferences", "Quantities"}
MEMBER_KINDS = ("usage", "connection", "end", "subject")


def library_files():
    return sorted(os.path.join(LIBRARY_DIR, f) for f in os.listdir(LIBRARY_DIR) if f.endswith(".sysml"))


@dataclass
class Scope:
    """The packages around a node, innermost last."""
    packages: tuple

    def push(self, pkg):
        return Scope(self.packages + (pkg,))


@dataclass
class Inst:
    name: str
    metaclass: str
    path: str                       # e.g. vehicle.s1.fuelTank
    type: object = None             # a definition Node, a standard type name (str), or None
    type_name: str = ""
    count: int = 1
    value: object = None            # SI number, str, bool, or enum literal name
    unit_given: str | None = None
    children: dict = field(default_factory=dict)
    connections: list = field(default_factory=list)
    line: int = 0
    file: str = ""
    doc: str = ""
    optional: bool = False          # an attribute declared [0..1]

    def get(self, name, default=None):
        c = self.children.get(name)
        return default if c is None or c.value is None else c.value

    def parts(self):
        return [c for c in self.children.values() if c.metaclass in ("part", "item") and c.count > 0]


class Model:
    def __init__(self):
        self.errors = []
        self.packages = {}            # qualified name -> (Node, Scope)
        self.defs = {}                # qualified name -> (Node, Scope)
        self.roots = []

    # ---- loading
    def add_text(self, text, path):
        try:
            root = S.parse(text, path)
        except S.SysMLSyntaxError as ex:
            self.errors.append(str(ex))
            return
        self.roots.append(root)
        self._index(root, Scope(()), "")

    def _index(self, node, scope, prefix):
        for c in node.children:
            if c.kind == "package":
                q = f"{prefix}{c.name}"
                if q in self.packages:
                    self.errors.append(f"{c.path}:{c.line}: package {q} is defined twice")
                self.packages[q] = (c, scope.push(c))
                self._index(c, scope.push(c), q + "::")
            elif c.kind == "def":
                q = f"{prefix}{c.name}"
                if q in self.defs:
                    self.errors.append(f"{c.path}:{c.line}: {c.metaclass} def {q} is defined twice")
                self.defs[q] = (c, scope)

    # ---- name resolution
    def _pkg_qname(self, pkg_node):
        for q, (n, _) in self.packages.items():
            if n is pkg_node:
                return q
        return ""

    def resolve(self, name, scope, line=0, path=""):
        """A definition (Node) or a standard library type name (str); None (with an error) if unknown."""
        if "::" in name:
            head, last = name.rsplit("::", 1)
            if head in STANDARD_PACKAGES:
                return self._standard(last, name, line, path)
            if name in self.defs:
                return self.defs[name][0]
            self.errors.append(f"{path}:{line}: unknown type {name}")
            return None
        for pkg in reversed(scope.packages):
            q = self._pkg_qname(pkg)
            if f"{q}::{name}" in self.defs:
                return self.defs[f"{q}::{name}"][0]
        # imports, innermost scope first
        std_imported = False
        for pkg in reversed(scope.packages):
            for imp in pkg.members("import"):
                target = imp.name[:-3] if imp.name.endswith("::*") else imp.name
                if imp.name.endswith("::*"):
                    if target in STANDARD_PACKAGES:
                        std_imported = True
                    elif f"{target}::{name}" in self.defs:
                        return self.defs[f"{target}::{name}"][0]
                    elif target not in self.packages:
                        self.errors.append(f"{imp.path}:{imp.line}: imported package {target} does not exist")
                elif target.rsplit("::", 1)[-1] == name and target in self.defs:
                    return self.defs[target][0]
        if std_imported and (name in QUANTITY_TYPES or name in SCALAR_TYPES):
            return name
        if name in QUANTITY_TYPES or name in SCALAR_TYPES:
            self.errors.append(f"{path}:{line}: {name} is a standard library type; import ScalarValues::* or ISQ::*")
            return name
        self.errors.append(f"{path}:{line}: unknown type {name}")
        return None

    def _standard(self, last, name, line, path):
        if last in QUANTITY_TYPES or last in SCALAR_TYPES:
            return last
        self.errors.append(f"{path}:{line}: {name} is not a standard library type this checker knows")
        return None

    def scope_of_def(self, d):
        for q, (n, sc) in self.defs.items():
            if n is d:
                return sc
        return Scope(())

    def linearize(self, d, seen=None):
        """A definition and every definition it specializes, base first."""
        seen = seen if seen is not None else []
        if d in seen:
            return []
        seen.append(d)
        out = []
        sc = self.scope_of_def(d)
        for sup in d.specializes:
            s = self.resolve(sup, sc, d.line, d.path)
            if isinstance(s, S.Node):
                if s.metaclass != d.metaclass:
                    self.errors.append(f"{d.path}:{d.line}: {d.metaclass} def {d.name} cannot specialize {s.metaclass} def {s.name}")
                for x in self.linearize(s, seen):
                    if x not in out:
                        out.append(x)
        out.append(d)
        return out

    def specializes(self, d, base_name):
        if not isinstance(d, S.Node):
            return False
        return any(x.name == base_name for x in self.linearize(d))

    # ---- instances
    def instances(self):
        out = []
        for root in self.roots:
            self._collect(root, Scope(()), out)
        return out

    def _collect(self, node, scope, out):
        for c in node.children:
            if c.kind == "package":
                self._collect(c, scope.push(c), out)
            elif c.kind == "usage" and c.metaclass == "part":
                out.append(self.instantiate([(c, scope)], c.name, c.name))

    def instantiate(self, layers, name, path):
        node0 = layers[-1][0]
        meta = next((n.metaclass for n, _ in layers if n.metaclass), "")
        inst = Inst(name=name, metaclass=meta or "attribute", path=path, line=node0.line, file=node0.path)
        tnode, tscope = next(((n, sc) for n, sc in reversed(layers) if n.types), (None, None))
        if tnode is not None:
            inst.type_name = tnode.types[0]
            inst.type = self.resolve(tnode.types[0], tscope, tnode.line, tnode.path)
            if len(tnode.types) > 1:
                self.errors.append(f"{tnode.path}:{tnode.line}: {path} has more than one type; the design language uses one")
        for n, _ in layers:
            for d in n.members("doc"):
                inst.doc = d.text
        mult = next((n.multiplicity for n, _ in reversed(layers) if n.multiplicity is not None), None)
        if mult is not None and inst.metaclass not in ("part", "item"):
            inst.optional = mult[0] == 0
        elif mult is not None:
            lo, hi = mult
            if hi is None or lo != hi:
                self.errors.append(f"{node0.path}:{node0.line}: {path}: give an exact count, e.g. [3], not [{lo}..{'*' if hi is None else hi}]")
            inst.count = lo
        # members: from the type's definitions, base first, then each layer
        member_layers = {}
        conns = []
        sources = []
        if isinstance(inst.type, S.Node):
            ok = {"part": {"part", "item"}, "attribute": {"attribute", "enum"}}.get(meta, {meta or inst.type.metaclass})
            if inst.type.metaclass not in ok:
                self.errors.append(f"{node0.path}:{node0.line}: {path} is a {meta} usage typed by {inst.type.metaclass} def {inst.type.name}")
            for d in self.linearize(inst.type):
                sources.append((d, self.scope_of_def(d)))
        sources += [(n, sc) for n, sc in layers]
        for n, sc in sources:
            inner = sc.push(n) if n.kind == "package" else sc
            for c in n.children:
                if c.kind == "connection":
                    conns.append((c, inner))
                elif c.kind in ("usage", "end") and c.name:
                    if c.redefines and c.name not in member_layers:
                        self.errors.append(f"{c.path}:{c.line}: {path} has no feature {c.redefines} to redefine")
                    member_layers.setdefault(c.name, []).append((c, inner))
                elif c.kind == "usage" and not c.name:
                    self.errors.append(f"{c.path}:{c.line}: an unnamed {c.metaclass} in {path}; name it")
        for mname, ml in member_layers.items():
            inst.children[mname] = self.instantiate(ml, mname, f"{path}.{mname}")
        inst.connections = conns
        # value
        vnode = next((n for n, _ in reversed(layers) if n.value is not None), None)
        if vnode is not None:
            self._set_value(inst, vnode.value, vnode)
        return inst

    def _set_value(self, inst, v, node):
        where = f"{node.path}:{v.line or node.line}"
        t = inst.type
        if v.kind == "number":
            if v.unit:
                try:
                    f, d = parse_unit(v.unit)
                except UnitError as ex:
                    self.errors.append(f"{where}: {inst.path}: {ex}")
                    return
                inst.value, inst.unit_given = v.value * f, v.unit
                if isinstance(t, str) and t in QUANTITY_TYPES and QUANTITY_TYPES[t] != d:
                    self.errors.append(f"{where}: {inst.path} is a {t} ({dim_text(QUANTITY_TYPES[t])}) but [{v.unit}] is {dim_text(d)}")
                elif isinstance(t, str) and t in SCALAR_TYPES:
                    self.errors.append(f"{where}: {inst.path} is a {t} and takes no unit")
            else:
                inst.value = v.value
                if isinstance(t, str) and t in QUANTITY_TYPES:
                    self.errors.append(f"{where}: {inst.path} is a {t} and needs a unit, e.g. [{_example_unit(t)}]")
                if t in ("Integer", "Natural", "Positive") and v.value != int(v.value):
                    self.errors.append(f"{where}: {inst.path} must be a whole number")
            if t in ("Boolean", "String"):
                self.errors.append(f"{where}: {inst.path} is a {t}, not a number")
        elif v.kind == "string":
            inst.value = v.value
            if t is not None and t != "String":
                self.errors.append(f"{where}: {inst.path} is a {getattr(t, 'name', t)}, not a string")
        elif v.kind == "bool":
            inst.value = v.value
            if t is not None and t != "Boolean":
                self.errors.append(f"{where}: {inst.path} is a {getattr(t, 'name', t)}, not true or false")
        elif v.kind == "enum":
            enum_name, _, lit = v.value.rpartition("::")
            if isinstance(t, S.Node) and t.metaclass == "enum":
                lits = {x.name for d in self.linearize(t) for x in d.members("enumlit")}
                if enum_name.rsplit("::", 1)[-1] != t.name:
                    self.errors.append(f"{where}: {inst.path} takes a {t.name} literal, not {v.value}")
                elif lit not in lits:
                    self.errors.append(f"{where}: {t.name} has no literal {lit}; choose from {', '.join(sorted(lits))}")
            else:
                self.errors.append(f"{where}: {inst.path} is not an enumeration, so {v.value} is not a value for it")
            inst.value = lit


def _example_unit(t):
    return {"LengthValue": "m", "MassValue": "kg", "PressureValue": "bar", "ForceValue": "N", "AreaValue": "m^2",
            "SpeedValue": "m/s", "AngularMeasureValue": "deg", "AngularVelocityValue": "deg/s", "PowerValue": "W",
            "EnergyValue": "J", "CoefficientOfHeatTransferValue": "W/(m^2*K)"}.get(t, "SI")


def load(paths, with_library=True):
    m = Model()
    files = (library_files() if with_library else []) + [p for p in paths if os.path.abspath(p) not in map(os.path.abspath, library_files())]
    for p in files:
        with open(p) as f:
            m.add_text(f.read(), os.path.relpath(p) if not os.path.isabs(p) or p.startswith(os.getcwd()) else p)
    return m
