import sys, os
import argparse
import logging
from datetime import datetime
from utils.dot_env_secrets import load_env_as_dict

from utils.compare_tool import (
    load_config,
    render_progress,
    run_validation,
    render_summary
)

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------
os.makedirs('./logs/', exist_ok=True)
log_format = '%(asctime)s %(levelname)s %(message)s'

logging.basicConfig(
    filename=f'./logs/data_validation_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log',
    level=logging.INFO,
    format=log_format,
    force=True,   # ensures our config wins, regardless of import order
)

class SuppressNoisyLoggers(logging.Filter):
    """
    Hides log records from known-noisy third-party loggers on the CONSOLE only.
    These records still reach the file handler (attached to root separately,
    with no filter) - so nothing is lost from the audit trail, only the
    live terminal view is cleaned up.
    """
    NOISY_PREFIXES = (
        "datacompy",     # per-column match INFO/WARNING spam + optional-dependency notices
        "urllib3",
        "py.warnings",   # captures actual python warnings.warn() calls routed through logging
    )

    def filter(self, record):
        return not record.name.startswith(self.NOISY_PREFIXES)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(log_format))
console_handler.setLevel(logging.INFO)
console_handler.addFilter(SuppressNoisyLoggers())
logging.getLogger().addHandler(console_handler)

logger = logging.getLogger("data_validator")

def main():
    parser = argparse.ArgumentParser(
        description="Compare source vs target data using datacompy + pandas compare")
    parser.add_argument('--validation', '-v', type=str,
                        help="Run only this validation key", required=True)
    args = parser.parse_args()

    config_path         = f"./config/configuration.yaml"
    config              = load_config(config_path)
    global_output_cfg   = config.get("output", {})
    validations         = config["compare"]

    if args.validation:
        if args.validation not in validations:
            logger.error(f"Validation '{args.validation}' not found in config.")
            sys.exit(1)
        validations = {args.validation: validations[args.validation]}

    statuses = [{"name": k, "type": v.get("type"), "state": "PENDING", "duration_sec": "-"}
                for k, v in validations.items()]
    render_progress(statuses)

    results = []
    for i, (name, cfg) in enumerate(validations.items()):
        db_uri = None
        vtype = cfg["type"]
        if vtype == "rds":
            # Get secret keys & values
            secrets = load_env_as_dict(".env")
            db_uri  = f"{secrets.get(f'pg_connection_string')}/{cfg.get('db_name')}"

        statuses[i]["state"] = "RUNNING..."
        render_progress(statuses)

        try:
            result = None
            if vtype == "rds":
                result = run_validation(name, cfg, global_output_cfg, db_uri)
            else:
                result = run_validation(name, cfg, global_output_cfg)
            statuses[i]["state"] = result["status"]
            statuses[i]["duration_sec"] = result["duration_sec"]
        except Exception as e:
            result = {"name": name, "error": str(e), "duration_sec": "-"}
            statuses[i]["state"] = "ERROR"

        results.append(result)
        render_progress(statuses)

    render_summary(results)

    logger.info("\nDetails:")
    for r in results:
        if r.get("error"):
            logger.error(f"{r['name']} failed: {r['error']}")
        elif r.get("report_dir"):
            logger.info(f"   {r['name']}: {r['report_dir']}")

if __name__ == "__main__":
    main()