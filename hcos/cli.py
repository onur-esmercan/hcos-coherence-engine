import json
import argparse
from .coherence import compute_coherence

def main():
    parser = argparse.ArgumentParser(
        description="HCOS Coherence Engine v0.1"
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        help="Path to JSON file with HCOS dimensions",
        required=False,
        default=None,
    )
    args = parser.parse_args()

    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        # fallback: simple prompt mode
        print("No input file given, using default sample values.")
        data = {
            "Flow": 0.6,
            "Body": 0.4,
            "Finance": 0.5,
            "LongTerm": 0.7,
            "Externalization": 0.3,
            "Overload": 0.2,
        }

    result = compute_coherence(data)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
