"""Entry point script for the Rover Mission Control application."""

import sys
from mission_control.core.app import MissionControlApp


def main() -> int:
    """Main execution function."""
    app = MissionControlApp(sys.argv)
    if not app.initialize():
        return 1
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
