"""Print the grid's public map (default) or every area's private data (--private) as JSON."""
import json
import sys

from . import grid as G

if __name__ == "__main__":
    g = G.build()
    if "--private" in sys.argv:
        print(json.dumps({G.area_name(a): G.area_private(g, a) for a in range(G.N_AREAS)}))
    else:
        print(json.dumps(G.public_view(g), indent=1))
