# Python packages for the image

Each file here is one tool's pinned packages. The Dockerfile installs every `*.txt` file in this folder
into the `marsflight` conda env, in name order. Tudat comes from conda and is installed before these.

To add a tool, add your own file, for example `basilisk.txt`. Do not edit the Dockerfile or another
tool's file. Pin exact versions, and say the license in a comment at the top. The team
image and the hub are built from the same image, so the agents can import these packages too. That is
fine for public tools; never put anything hidden here.
