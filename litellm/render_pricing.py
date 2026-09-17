"""Fills in model_info pricing from litellm's own bundled cost map, keyed
by each model's `pricing_ref`, so prices stay accurate across image
updates without anyone hand-typing them.
"""
import sys

import litellm
import yaml

config_path = sys.argv[1]

with open(config_path) as f:
    config = yaml.safe_load(f)

for entry in config["model_list"]:
    info = entry.setdefault("model_info", {})
    ref = info.pop("pricing_ref", None)
    if not ref:
        continue
    cost = litellm.model_cost.get(ref)
    if not cost:
        raise SystemExit(f"pricing_ref '{ref}' not found in litellm.model_cost")
    info["input_cost_per_token"] = cost["input_cost_per_token"]
    info["output_cost_per_token"] = cost["output_cost_per_token"]

with open(config_path, "w") as f:
    yaml.safe_dump(config, f, sort_keys=False)
