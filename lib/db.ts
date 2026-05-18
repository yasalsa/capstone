import { Pool, type QueryResultRow } from "pg";

const GLOBAL_POOL_KEY = Symbol.for("capstone.pg.pool");

type GlobalWithPool = typeof globalThis & {
  [GLOBAL_POOL_KEY]?: Pool;
};

function assertDatabaseUrl(): string {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error(
      "DATABASE_URL is missing. For Vercel, set DATABASE_URL to your Postgres connection string."
    );
  }
  return url;
}

function createPool(): Pool {
  const connectionString = assertDatabaseUrl();
  return new Pool({
    connectionString,
    ssl: connectionString.includes("localhost")
      ? false
      : { rejectUnauthorized: false }
  });
}

function getPool(): Pool {
  const globalObject = globalThis as GlobalWithPool;
  if (!globalObject[GLOBAL_POOL_KEY]) {
    globalObject[GLOBAL_POOL_KEY] = createPool();
  }
  return globalObject[GLOBAL_POOL_KEY] as Pool;
}

export async function dbQuery<T extends QueryResultRow>(
  text: string,
  params: Array<string | number | null> = []
): Promise<T[]> {
  const pool = getPool();
  const result = await pool.query<T>(text, params);
  return result.rows;
}
