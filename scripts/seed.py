import secrets

import bcrypt

from relay.db import pool


def create_api_key(cur, tenant_id: str) -> str:
    prefix = "rly_" + secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)

    secret_hash = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()

    cur.execute(
        """
        INSERT INTO api_keys (tenant_id, key_prefix, key_hash, name)
        VALUES (%s, %s, %s, %s)
        """,
        (tenant_id, prefix, secret_hash, "dev key"),
    )

    return f"{prefix}.{secret}"


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

            tenant_id = row[0]

            cur.execute(
                "SELECT count(*) FROM api_keys WHERE tenant_id = %s",
                (tenant_id,),
            )
            count_row = cur.fetchone()
            existing = count_row[0] if count_row else 0

            if existing:
                full_key = None
                print(f"api key already exists ({existing}) — not creating another")
            else:
                full_key = create_api_key(cur, tenant_id)

    print(f"DEV_TENANT_ID={tenant_id}")
    if full_key:
        print(f"RELAY_API_KEY={full_key}")
        print("^^ save this in .env now — it cannot be recovered")

    pool.close()


if __name__ == "__main__":
    main()
