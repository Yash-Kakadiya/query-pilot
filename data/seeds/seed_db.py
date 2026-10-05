"""CLI runner for DatabaseSeeder targeting GradeSense_Local."""

import argparse
import logging
import sys
from pathlib import Path
from sqlalchemy import create_engine

# Add src to Python path if run standalone
root_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root_dir / "src"))

from query_pilot.config import get_db_settings
from query_pilot.db.seeder import DatabaseSeeder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Seed GradeSense_Local database.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration, paths, and datasets without executing database operations.",
    )
    args = parser.parse_args()

    # Load database-only configuration (no LLM credentials required)
    db_settings = get_db_settings()

    # Double safeguard: ensure settings target GradeSense_Local
    if db_settings.DB_NAME != "GradeSense_Local":
        logger.error(
            f"REFUSING TO SEED: db_settings.DB_NAME is '{db_settings.DB_NAME}'. "
            "Seeding can only target 'GradeSense_Local'."
        )
        sys.exit(1)

    csv_dir = Path(r"D:\01. Github\gradesense-ml\data\raw")
    supplemental_dir = root_dir / "data" / "seeds"

    if not csv_dir.exists():
        logger.error(f"CSV source directory not found: {csv_dir}")
        sys.exit(1)

    logger.info("Initializing DatabaseSeeder with database-only configuration...")
    seeder = DatabaseSeeder(csv_dir=csv_dir, supplemental_dir=supplemental_dir)

    engine_url = db_settings.get_sqlalchemy_url()

    if args.dry_run:
        logger.info("[DRY RUN] Configuration validated successfully.")
        logger.info(f"[DRY RUN] Target Server: {db_settings.DB_SERVER}")
        logger.info(f"[DRY RUN] Target Database: {db_settings.DB_NAME}")
        logger.info(f"[DRY RUN] Trusted Connection: {db_settings.DB_USE_TRUSTED_CONNECTION}")
        logger.info(f"[DRY RUN] CSV Source: {csv_dir}")
        logger.info(f"[DRY RUN] Supplemental Dir: {supplemental_dir}")
        logger.info("[DRY RUN] Dry-run complete. No database operations performed.")
        return

    engine = create_engine(engine_url)

    logger.info("Starting safe database seeding...")
    results = seeder.seed_all(engine)

    logger.info("Database seeding completed successfully.")
    for tbl, count in results.items():
        logger.info(f"  - {tbl}: {count} records")


if __name__ == "__main__":
    main()
