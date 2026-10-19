#!/usr/bin/env python3
"""Entry point kept for backwards compatibility: `python main.py` == `python -m news_agent run`."""

import sys

from news_agent.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
