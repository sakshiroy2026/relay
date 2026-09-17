from relay.db import pool


def main() -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tenants WHERE name = %s", ("dev",))
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
                    ("dev",),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("tenant insert returned no row")
                print("created tenant")
            else:
                print("tenant already exists")

    print(f"DEV_TENANT_ID={row[0]}")


if __name__ == "__main__":
    main()
