"""The face, in a browser tab.

tkinter draws arcs and rectangles and nothing else, which is why the HUD
looks like software from 1998 however carefully it is arranged. A browser
already has gradients, blur, shadows, real fonts and smooth animation, and
every machine has one.

So this serves a small page from a local HTTP server. No framework, no build
step, no dependencies -- one HTML file and Python's standard library, which
matters because the whole thing has to install on a Chromebook with pip
sometimes unavailable.
"""

from jarvis.web.server import WebFace, find_free_port

__all__ = ["WebFace", "find_free_port"]
