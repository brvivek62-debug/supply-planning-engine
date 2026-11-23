import subprocess
import sys

from planner.__main__ import COMMANDS, build_parser


def test_help_lists_all_commands():
    out = subprocess.run([sys.executable, "-m", "planner", "--help"], capture_output=True, text=True, check=True).stdout
    for name in COMMANDS:
        assert name in out


def test_parser_defaults():
    args = build_parser().parse_args(["generate", "--scale", "test"])
    assert args.seed == 20261022 and args.scale == "test"
