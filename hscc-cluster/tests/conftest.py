import os, sys

# Put the plugin dir on sys.path so `import clusterlib` resolves without
# importing the plugin package (whose hyphenated dir name isn't importable).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
