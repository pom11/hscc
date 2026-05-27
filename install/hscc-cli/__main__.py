#!/usr/bin/env python3
"""
HSCC CLI — Entry point for hscc command.
"""

import sys
import os

# Add install directory to path
install_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, install_dir)

from hscc_cli import main

if __name__ == "__main__":
    main()
