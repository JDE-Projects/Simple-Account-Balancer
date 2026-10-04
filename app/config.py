"""Application configuration constants."""

GITHUB_OWNER = "JDE-Projects"
GITHUB_REPO = "Simple-Account-Balancer"
ALLOWED_URL_HOST = "jde-projects.com"

DB_FILENAME = "simple_account_balancer.db"
BACKUP_DIRNAME = "backups"
BACKUP_KEEP = 5
BACKUP_KEEP_MIN = 1
BACKUP_KEEP_MAX = 50
PRERESTORE_KEEP = 3
SCHEMA_VERSION = 3
DEFAULT_RANGE_DAYS = 30

# Enforced window minimum, read by create_window's min_size.
MIN_WINDOW_W = 680
MIN_WINDOW_H = 650
