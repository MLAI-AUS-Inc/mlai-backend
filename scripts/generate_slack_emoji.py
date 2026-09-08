"""Generate the server reaction catalog from the clients' pinned emoji-mart JSON."""

import json
import sys
from pathlib import Path


def generate(source: Path) -> dict:
    data = json.loads(source.read_text())
    shortcodes = {}
    for key, entry in data["emojis"].items():
        names = [key] + [
            alias for alias, target in data["aliases"].items() if target == key
        ]
        for index, skin in enumerate(entry["skins"]):
            tones = [
                int(code, 16) - 0x1F3F9
                for code in skin["unified"].split("-")
                if 0x1F3FB <= int(code, 16) <= 0x1F3FF
            ]
            tone = "-".join(str(value) for value in dict.fromkeys(tones))
            if index and not tone:
                continue
            for name in names:
                shortcodes[name + ("::skin-tone-" + tone if tone else "")] = skin[
                    "native"
                ]
    return {
        "source": "@emoji-mart/data 1.2.1, Unicode 15 (MIT); https://github.com/missive/emoji-mart",
        "shortcodes": shortcodes,
    }


if __name__ == "__main__":
    destination = (
        Path(__file__).resolve().parents[1]
        / "integrations/services/community_bridge/slack_emoji.json"
    )
    destination.write_text(
        json.dumps(
            generate(Path(sys.argv[1])), ensure_ascii=False, separators=(",", ":")
        )
        + "\n"
    )
