import os
import sys

# The shared lib/ (toml_min, tooling) sits at the repo root. realpath, because
# the installed plugin directory is a symlink into the repo.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "..", "..", "..", "lib"))
