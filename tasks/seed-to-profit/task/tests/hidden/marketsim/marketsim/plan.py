"""company/plan.yaml: reading and checking the company's setup.

    company: Example Goods          # any name
    credit_line: 250000             # credit line limit to open, 0 to 500000
    products:                       # 0 to 10 product lines from the catalog
      - id: k1
        channels: [site, marketplace]
        supplier: A                   # optional: A standard, B budget, C premium; orders may name another
"""
from .catalog import PRODUCTS, RULES


class PlanError(Exception):
    pass


def parse(text: str) -> dict:
    import yaml
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise PlanError(f"company/plan.yaml is not valid YAML: {e}") from None
    return check(data or {})


def check(data) -> dict:
    """Return the normalized plan, or raise PlanError with every problem found."""
    problems = []
    if not isinstance(data, dict):
        raise PlanError("company/plan.yaml must be a mapping with keys company, credit_line and products")
    unknown = set(data) - {"company", "credit_line", "products"}
    if unknown:
        problems.append(f"unknown keys: {sorted(unknown)}")
    credit = data.get("credit_line", 0) or 0
    if not isinstance(credit, (int, float)) or isinstance(credit, bool) or not 0 <= credit <= RULES["credit_max"]:
        problems.append(f"credit_line must be a number from 0 to {RULES['credit_max']:.0f}")
        credit = 0
    prods = data.get("products") or []
    if not isinstance(prods, list):
        problems.append("products must be a list")
        prods = []
    if len(prods) > RULES["max_products"]:
        problems.append(f"at most {RULES['max_products']} products, got {len(prods)}")
    out, seen = [], set()
    for i, item in enumerate(prods):
        if not isinstance(item, dict):
            problems.append(f"products[{i}] must be a mapping with id and channels")
            continue
        pid = item.get("id")
        if pid not in PRODUCTS:
            problems.append(f"products[{i}]: unknown product id {pid!r}")
            continue
        if pid in seen:
            problems.append(f"products[{i}]: {pid} is listed twice")
            continue
        seen.add(pid)
        chans = item.get("channels", list(RULES["channels"]))
        if isinstance(chans, str):
            chans = [chans]
        if not isinstance(chans, list) or not chans or any(c not in RULES["channels"] for c in chans):
            problems.append(f"products[{i}]: channels must be a non-empty list of {list(RULES['channels'])}")
            continue
        extra = set(item) - {"id", "channels", "supplier"}
        if extra:
            problems.append(f"products[{i}]: unknown keys {sorted(extra)}")
        sup = item.get("supplier", "A")
        if sup not in ("A", "B", "C"):
            problems.append(f"products[{i}]: supplier must be A, B or C")
            continue
        out.append({"id": pid, "channels": sorted(set(chans)), "supplier": sup})
    if problems:
        raise PlanError("; ".join(problems))
    return {"company": str(data.get("company", ""))[:80], "credit_line": float(credit), "products": out}
