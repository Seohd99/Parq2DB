"""Write a disposable CI profile; credentials stay in environment variables."""

from pathlib import Path
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dialect", choices=("oracle", "mysql", "postgres"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    settings = {
        "oracle": 'port = 1521\nservice_name = "FREEPDB1"',
        "mysql": 'port = 3306\ndatabase = "pb_test"',
        "postgres": 'port = 5432\ndatabase = "pb_test"\nschema = "public"',
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        f'[db.{args.dialect}]\nhost = "127.0.0.1"\n{settings[args.dialect]}\n'
        'user = "pb_test_user"\npassword_env = "PB_CI_DB_PASSWORD"\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
