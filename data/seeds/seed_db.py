"""CLI runner for DatabaseSeeder targeting GradeSense_Local."""

import logging
import sys
from pathlib import Path
from sqlalchemy import create_engine

# Add src to Python path if run standalone
root_dir = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root_dir / "src"))

from query_pilot.config import get_settings
from query_pilot.db.seeder import DatabaseSeeder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    settings = get_settings()

    # Double safeguard: ensure settings target GradeSense_Local
    if settings.DB_NAME != "GradeSense_Local":
        logger.error(
            f"REFUSING TO SEED: settings.DB_NAME is '{settings.DB_NAME}'. "
            "Seeding can only target 'GradeSense_Local'."
        )
        sys.exit(1)

    csv_dir = Path(r"D:\01. Github\gradesense-ml\data\raw")
    supplemental_dir = root_dir / "data" / "seeds"

    if not csv_dir.exists():
        logger.error(f"CSV source directory not found: {csv_dir}")
        sys.exit(1)

    logger.info("Initializing DatabaseSeeder...")
    seeder = DatabaseSeeder(csv_dir=csv_dir, supplemental_dir=supplemental_dir)

    engine_url = settings.get_sqlalchemy_url()
    engine = create_engine(engine_url)

    logger.info("Starting safe database seeding...")
    results = seeder.seed_all(engine)

    logger.info("Database seeding completed successfully.")
    for tbl, count in results.items():
        logger.info(f"  - {tbl}: {count} records")


if __name__ == "__main__":
    main()
