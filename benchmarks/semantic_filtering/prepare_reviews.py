"""Download UCI Sentiment Labelled Sentences and freeze text-disjoint splits."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

import httpx

URL = "https://archive.ics.uci.edu/static/public/331/sentiment+labelled+sentences.zip"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    with httpx.Client(proxy=args.proxy, timeout=60, follow_redirects=True) as client:
        response = client.get(URL)
        response.raise_for_status()
    unique = {}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in sorted(archive.namelist()):
            if not name.endswith("_labelled.txt") or "__MACOSX" in name:
                continue
            for line in archive.read(name).decode("utf-8").split("\n"):
                if not line.strip():
                    continue
                text, label = line.rsplit("\t", 1)
                identity = hashlib.sha256(text.strip().casefold().encode()).hexdigest()
                entry = {
                    "text": text.strip(),
                    "positive": bool(int(label)),
                    "source": Path(name).name,
                }
                if identity in unique and unique[identity]["positive"] != entry["positive"]:
                    raise ValueError("Conflicting labels for identical text")
                unique[identity] = entry
    ordered = [unique[key] for key in sorted(unique)]
    result = {
        "source": URL,
        "doi": "10.24432/C57604",
        "license": "CC-BY-4.0",
        "archive_sha256": hashlib.sha256(response.content).hexdigest(),
        "splits": {
            "development": ordered[:40],
            "validation": ordered[40:120],
            "test": ordered[120:520],
            "development_v2": ordered[520:920],
            "validation_v2": ordered[920:1320],
            "test_v2": ordered[1320:1720],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print({"unique_texts": len(unique), "splits": {k: len(v) for k, v in result["splits"].items()}})


if __name__ == "__main__":
    main()
